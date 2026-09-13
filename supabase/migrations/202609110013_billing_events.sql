-- Billing event ingestion and reconciliation (Phase A5, account/cloud design
-- §9.3/§9.5): deduplicated notification events, retry-safe processing state
-- and the reconcile listing, all behind the service_role-only entry point.

-- Apple purchase verification and entitlement aggregation (Phase A4,
-- account/cloud design §9.2/§9.3/§9.4). Extends billing_service with the
-- apple_verify action: the trusted API verifies the JWS and re-queries Apple
-- first, then this RPC performs the unique binding, claim confirmation and
-- account-level entitlement aggregation inside one entry point.

-- Re-aggregate the account-level projection from all bound purchase chains,
-- under the account row lock. Any active source wins over grace; grace beats
-- expired; a revoked-only account is revoked. Every call bumps
-- entitlement_revision so all devices learn the change.
create or replace function public.billing_service(p_action text, p_data jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
  actor text := p_data->>'principal'; uid uuid; sid uuid;
  row billing_private.account_entitlements; claim billing_private.billing_claims;
  purchase billing_private.store_purchases;
  token uuid; inserted integer; pool_limit integer; used_count integer;
  stored_hash text;
  key_hash text;
begin
  if p_action in ('claim_register','claim_get','apple_verify','entitlement') then
    if actor !~ '^account:[0-9a-fA-F-]{36}$' then
      return jsonb_build_object('code','ACCOUNT_REQUIRED');
    end if;
    uid := substring(actor from 9)::uuid;
    sid := (p_data->>'sessionID')::uuid;
    -- Worker-side processing has no user session; user liveness still applies.
    if coalesce(p_data->>'requireSession','true')::boolean then
      if not exists(select 1 from auth.users u join auth.sessions s on s.user_id=u.id
        where u.id=uid and s.id=sid and u.email_confirmed_at is not null and not coalesce(u.is_anonymous,false)) then
        return jsonb_build_object('code','ACCOUNT_UNAVAILABLE');
      end if;
    end if;
    if exists(select 1 from sync_private.accounts where user_id=uid and deletion_pending) then
      return jsonb_build_object('code','ACCOUNT_UNAVAILABLE');
    end if;
  end if;

  if p_action='account_by_token' then
    select coalesce(user_id::text,'') into stored_hash
      from billing_private.account_entitlements
      where purchase_account_token=(p_data->>'appAccountToken')::uuid;
    if stored_hash = '' then return jsonb_build_object('code','ACCOUNT_TOKEN_UNKNOWN'); end if;
    return jsonb_build_object('userID',stored_hash);
  end if;

  if p_action='claim_register' then
    row := billing_private.ensure_account(uid);
    token := row.purchase_account_token;
    insert into billing_private.billing_claims(claim_id,user_id,provider,product_id,
        expected_account_identifier_hash,request_hash)
      values((p_data->>'claimId')::uuid,uid,p_data->>'provider',p_data->>'productId',
        encode(sha256(convert_to(token::text,'UTF8')),'hex'),
        encode(sha256(convert_to((p_data->>'claimId') || uid::text || (p_data->>'provider')
          || (p_data->>'productId'),'UTF8')),'hex'))
      on conflict (claim_id) do nothing;
    get diagnostics inserted = row_count;
    select * into claim from billing_private.billing_claims where claim_id=(p_data->>'claimId')::uuid;
    if inserted = 0 and (claim.user_id is distinct from uid
        or claim.provider is distinct from p_data->>'provider'
        or claim.product_id is distinct from p_data->>'productId') then
      return jsonb_build_object('code','CLAIM_CONFLICT');
    end if;
    return jsonb_build_object('claimId',claim.claim_id,'appAccountToken',token,
      'expiresAt',billing_private.iso(claim.created_at + interval '1 hour'));
  end if;

  if p_action='claim_get' then
    select * into claim from billing_private.billing_claims where claim_id=(p_data->>'claimId')::uuid;
    if not found then return jsonb_build_object('code','CLAIM_NOT_FOUND'); end if;
    if claim.user_id is distinct from uid then
      return jsonb_build_object('code','CLAIM_CONFLICT');
    end if;
    return jsonb_build_object('claimId',claim.claim_id,'status',claim.status,
      'provider',claim.provider,'productId',claim.product_id,
      'purchaseKeyHash',claim.purchase_key_hash,
      'entitlementRevision',claim.result_entitlement_revision);
  end if;

  if p_action='apple_verify' then
    row := billing_private.ensure_account(uid);
    token := row.purchase_account_token;
    if coalesce(p_data->>'appAccountToken','') <> token::text then
      return jsonb_build_object('code','ACCOUNT_MISMATCH');
    end if;
    key_hash := encode(sha256(convert_to('apple|' || (p_data->>'originalTransactionId'),'UTF8')),'hex');
    select * into purchase from billing_private.store_purchases
      where provider='apple' and purchase_key_hash=key_hash for update;
    if found then
      if purchase.user_id is not null and purchase.user_id is distinct from uid then
        return jsonb_build_object('code','TRANSACTION_ALREADY_BOUND');
      end if;
      update billing_private.store_purchases set user_id=uid,product_id=p_data->>'productId',
        store_status=p_data->>'storeStatus',expires_at=(p_data->>'expiresAt')::timestamptz,
        environment=p_data->>'environment',last_store_event_at=now(),updated_at=now()
        where id=purchase.id;
    else
      insert into billing_private.store_purchases(provider,environment,purchase_key_hash,
          store_reference_ciphertext,user_id,product_id,store_status,expires_at,last_store_event_at)
        values('apple',p_data->>'environment',key_hash,p_data->>'storeReferenceCiphertext',uid,
          p_data->>'productId',p_data->>'storeStatus',(p_data->>'expiresAt')::timestamptz,now());
    end if;
    if p_data->>'claimId' is not null then
      update billing_private.billing_claims set status='verified',purchase_key_hash=key_hash,
        result_entitlement_revision=row.entitlement_revision+1,completed_at=now()
        where claim_id=(p_data->>'claimId')::uuid and user_id=uid and status='pending';
    end if;
    perform billing_private.aggregate_entitlement(uid);
    select * into row from billing_private.account_entitlements where user_id=uid;
    select coalesce(free_limit,50) into pool_limit from ai_private.principals where id=actor;
    select coalesce(used,0) into used_count from ai_private.buckets where principal=actor and period='free';
    return jsonb_build_object('plan',row.plan,'status',row.status,
      'validUntil',billing_private.iso(row.valid_until),
      'serviceEndAt',billing_private.iso(row.service_end_at),
      'entitlementRevision',row.entitlement_revision,
      'aiQuota',jsonb_build_object('limit',coalesce(pool_limit,50),
        'used',coalesce(used_count,0),
        'remaining',greatest(0,coalesce(pool_limit,50)-coalesce(used_count,0)),
        'resetsAt',jsonb 'null'),
      'billingSources',(select coalesce(jsonb_agg(jsonb_build_object('provider',sp.provider,
          'productId',sp.product_id,'expiresAt',billing_private.iso(sp.expires_at))),'[]'::jsonb)
        from billing_private.store_purchases sp
        where sp.user_id=uid and sp.store_status in ('active','billing_retry')));
  end if;

  if p_action='entitlement' then
    row := billing_private.ensure_account(uid);
    -- Free-pool remainder lives in the shared AI ledger for the account principal.
    select coalesce(free_limit,50) into pool_limit from ai_private.principals where id=actor;
    select coalesce(used,0) into used_count from ai_private.buckets where principal=actor and period='free';
    return jsonb_build_object('plan',row.plan,'status',row.status,
      'validUntil',billing_private.iso(row.valid_until),
      'serviceEndAt',billing_private.iso(row.service_end_at),
      'entitlementRevision',row.entitlement_revision,
      'aiQuota',jsonb_build_object('limit',coalesce(pool_limit,50),
        'used',coalesce(used_count,0),
        'remaining',greatest(0,coalesce(pool_limit,50)-coalesce(used_count,0)),
        'resetsAt',jsonb 'null'),
      'billingSources',(select coalesce(jsonb_agg(jsonb_build_object('provider',sp.provider,
          'productId',sp.product_id,'expiresAt',billing_private.iso(sp.expires_at))),'[]'::jsonb)
        from billing_private.store_purchases sp
        where sp.user_id=uid and sp.store_status in ('active','billing_retry')));
  end if;

  if p_action='event_receive' then
    if length(coalesce(p_data->>'payloadHash','')) < 32
       or length(coalesce(p_data->>'eventId','')) < 1
       or length(coalesce(p_data->>'replayMaterialCiphertext','')) < 1 then
      raise exception 'BILLING_INVALID_EVENT';
    end if;
    insert into billing_private.billing_events(provider,environment,event_id,payload_hash,
        replay_material_ciphertext)
      values(p_data->>'provider',p_data->>'environment',p_data->>'eventId',
        p_data->>'payloadHash',p_data->>'replayMaterialCiphertext')
      on conflict (provider,environment,event_id) do nothing;
    get diagnostics inserted = row_count;
    if inserted = 0 then
      select payload_hash into stored_hash from billing_private.billing_events
        where provider=p_data->>'provider' and environment=p_data->>'environment'
          and event_id=p_data->>'eventId';
      if stored_hash is distinct from p_data->>'payloadHash' then
        return jsonb_build_object('code','EVENT_CONFLICT');
      end if;
      return jsonb_build_object('received',false);
    end if;
    return jsonb_build_object('received',true);
  end if;

  if p_action='event_mark' then
    update billing_private.billing_events set
      status=coalesce(p_data->>'status',status),
      attempts=case when p_data->>'status' in ('failed','processed')
        then attempts+1 else attempts end,
      last_error_code=p_data->>'lastErrorCode',
      processed_at=case when p_data->>'status'='processed' then now() else processed_at end
      where provider=p_data->>'provider' and environment=p_data->>'environment'
        and event_id=p_data->>'eventId';
    get diagnostics inserted = row_count;
    return jsonb_build_object('updated',inserted);
  end if;

  if p_action='event_pending' then
    return jsonb_build_object('events',
      (select coalesce(jsonb_agg(jsonb_build_object('provider',provider,'environment',environment,
        'eventId',event_id,'attempts',attempts,
        'replayMaterialCiphertext',replay_material_ciphertext)),'[]'::jsonb)
       from (select * from billing_private.billing_events
             where status in ('received','failed')
               and attempts < coalesce((p_data->>'maxAttempts')::integer,8)
             order by received_at
             limit coalesce((p_data->>'limit')::integer,20)) pending));
  end if;

  if p_action='reconcile_list' then
    return jsonb_build_object('chains',
      (select coalesce(jsonb_agg(jsonb_build_object('userId',sp.user_id,
        'storeReferenceCiphertext',sp.store_reference_ciphertext,'productId',sp.product_id,
        'environment',sp.environment,'purchaseAccountToken',ae.purchase_account_token)),'[]'::jsonb)
       from billing_private.store_purchases sp
       join billing_private.account_entitlements ae on ae.user_id=sp.user_id
       where sp.user_id is not null and sp.store_status in ('active','billing_retry')));
  end if;

  raise exception 'BILLING_UNKNOWN_ACTION';
end $$;
revoke all on function public.billing_service(text,jsonb) from public,anon,authenticated;
grant execute on function public.billing_service(text,jsonb) to service_role;
revoke all on all functions in schema billing_private from public,anon,authenticated,service_role;
