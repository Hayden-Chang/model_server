-- F2: `GET /billing/entitlement` and its apple_verify twin must report the AI
-- quota from the same ledger the AI path charges. Both actions hand-read
-- `ai_private.principals.free_limit` plus the shared `ai_private.free_pools`
-- remainder and returned `'resetsAt', jsonb 'null'`, so a signed-out device that
-- holds Plus rendered its lifetime free remainder as "今日剩余 X/30" instead of
-- its member daily pool, and the number could disagree with what
-- /api/plan/parse actually enforced. The client design (subscription design §5
-- 会员 AI 额度契约衔接, quoted in
-- docs/ai-quota-shared-scope-analysis-20260917.md) requires one ledger:
-- 会员中心「今日剩余 X/30」与 AI 请求前的服务端额度检查消费同一份账本.
--
-- `ai_private.quota_status(actor,false,memberLimit)` resolves membership by
-- principal identity and picks the free pool or the member daily bucket
-- itself, so both branches now return its `limit`/`used`/`remaining`/`resetsAt`
-- directly. Only those four keys are re-projected: the action's public shape
-- (`AiQuotaStatus`) is unchanged. For a free principal the answer is
-- byte-identical to the previous hand-read; for a Plus principal it switches
-- from the lifetime pool to the member period and gains a real `resetsAt`.
--
-- `memberLimit` defaults to 30 inside quota_status, mirroring the
-- `coalesce((p_data->>'memberLimit')::integer,30)` convention every other
-- ai_quota_service call site uses; the account backend does not send one for
-- these actions, exactly as before.
--
-- No rollback file: this migration only replaces a function body and changes no
-- schema or data, so the inverse is re-applying the previous definition from
-- git (`git show fc6a038:supabase/migrations/202609170017_device_principal_billing.sql`),
-- the same restore-from-git rule 202609170017_device_principal_billing_rollback.sql
-- documents for its own replaced bodies. A `*_rollback.sql` file placed here
-- would also have to be registered in `recoveryScripts` (supabase/tests/database.mjs)
-- or the harness would apply it as a forward migration.

-- ---------------------------------------------------------------------------
-- Body re-emitted in full from 202609170017 with only the two `aiQuota`
-- constructions changed; every other action is byte-identical.
-- ---------------------------------------------------------------------------
create or replace function public.billing_service(p_action text, p_data jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
  actor text := p_data->>'principal';
  row billing_private.account_entitlements; claim billing_private.billing_claims;
  purchase billing_private.store_purchases; link billing_private.purchase_devices;
  token uuid; claim_key uuid; new_purchase_id bigint;
  inserted integer; quota jsonb;
  stored_hash text; key_hash text; active integer; bind_device boolean;
begin
  if p_action in ('claim_register','claim_get','apple_verify','entitlement') then
    -- Admission gate (D1): the principal is the device principal, i.e. the
    -- existing guest_* shape. This checks the shape and that /api/auth/guest
    -- created the ai_private.principals row; it does not authenticate device
    -- ownership (D2). It replaces the account session, auth.users and
    -- deletion_pending checks: an account deletion now cascades its
    -- account:<uuid> principal row away, so those calls fail closed here
    -- instead and can never resurrect a deleted account's entitlement row.
    if actor !~ '^guest_[a-f0-9]{24}$' then
      return jsonb_build_object('code','DEVICE_REQUIRED');
    end if;
    if not exists (select 1 from ai_private.principals where id = actor) then
      return jsonb_build_object('code','DEVICE_REQUIRED');
    end if;
  end if;

  if p_action='account_by_token' then
    -- The worker resolves the chain owner from the receipt's appAccountToken.
    -- Guard the cast: an absent or malformed token must yield a clean code, not
    -- an invalid input syntax exception.
    if coalesce(p_data->>'appAccountToken','') !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' then
      return jsonb_build_object('code','ACCOUNT_TOKEN_UNKNOWN');
    end if;
    select ae.principal into stored_hash from billing_private.account_entitlements ae
      where ae.purchase_account_token=(p_data->>'appAccountToken')::uuid;
    if stored_hash is null then return jsonb_build_object('code','ACCOUNT_TOKEN_UNKNOWN'); end if;
    return jsonb_build_object('principal',stored_hash);
  end if;

  if p_action='claim_register' then
    row := billing_private.ensure_account(actor);
    token := row.purchase_account_token;
    -- The client omits claimId on the first purchase and on the retry after a
    -- cancel, so the server mints one. Inserting a NULL primary key would fail
    -- the whole purchase path (design M0.4).
    claim_key := coalesce((p_data->>'claimId')::uuid, gen_random_uuid());
    insert into billing_private.billing_claims(claim_id,principal,provider,product_id,
        expected_account_identifier_hash,request_hash)
      values(claim_key,actor,p_data->>'provider',p_data->>'productId',
        encode(sha256(convert_to(token::text,'UTF8')),'hex'),
        encode(sha256(convert_to(claim_key::text || actor || (p_data->>'provider')
          || (p_data->>'productId'),'UTF8')),'hex'))
      on conflict (claim_id) do nothing;
    get diagnostics inserted = row_count;
    select * into claim from billing_private.billing_claims where claim_id=claim_key;
    if inserted = 0 and (claim.principal is distinct from actor
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
    if claim.principal is distinct from actor then
      return jsonb_build_object('code','CLAIM_CONFLICT');
    end if;
    return jsonb_build_object('claimId',claim.claim_id,'status',claim.status,
      'provider',claim.provider,'productId',claim.product_id,
      'purchaseKeyHash',claim.purchase_key_hash,
      'entitlementRevision',claim.result_entitlement_revision);
  end if;

  if p_action='apple_verify' then
    row := billing_private.ensure_account(actor);
    -- Resolve the receipt's appAccountToken to a known purchase principal. The
    -- token identifies the buying principal, not the requesting device, so a new
    -- device restoring the purchase presents the original device's token and
    -- this is a lookup, not an equality check against the actor's own token.
    -- Guard the cast first: StoreKit omits appAccountToken when the purchase
    -- carried none, and '' or a stray string would raise instead of returning.
    if coalesce(p_data->>'appAccountToken','') !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' then
      return jsonb_build_object('code','ACCOUNT_TOKEN_UNKNOWN');
    end if;
    select ae.principal into stored_hash from billing_private.account_entitlements ae
      where ae.purchase_account_token=(p_data->>'appAccountToken')::uuid;
    if stored_hash is null then return jsonb_build_object('code','ACCOUNT_TOKEN_UNKNOWN'); end if;
    -- Service-to-service calls (billing_worker notifications, reconcile) pass
    -- bindDevice=false: they update the chain but must never touch chain
    -- membership, or they would clear a support revocation.
    bind_device := coalesce((p_data->>'bindDevice')::boolean,true);
    key_hash := encode(sha256(convert_to('apple|' || (p_data->>'originalTransactionId'),'UTF8')),'hex');
    select * into purchase from billing_private.store_purchases
      where provider='apple' and purchase_key_hash=key_hash for update;
    if found then
      -- Join / restore path. The row lock above serializes the quota check and
      -- the write against concurrent joins on the same chain.
      if bind_device then
        select * into link from billing_private.purchase_devices
          where purchase_id=purchase.id and principal=actor;
        -- The check must cover every call which leaves actor non-revoked, not
        -- only the first join: D3 lets a revoked device back in, so a naive
        -- 'if not found then' would let A(revoked)/B/C plus a new D reach 4
        -- active devices and grow by one on every revoke/revive cycle.
        if link.principal is null or link.revoked_at is not null then
          select count(*) into active from billing_private.purchase_devices
            where purchase_id=purchase.id and revoked_at is null;
          if active >= 3 then return jsonb_build_object('code','DEVICE_LIMIT_REACHED'); end if;
        end if;
        insert into billing_private.purchase_devices(purchase_id,principal) values(purchase.id,actor)
          on conflict (purchase_id,principal) do update set revoked_at=null;
      end if;
      -- The chain's principal is NOT rewritten here: ownership is fixed by the
      -- first INSERT, otherwise a later joiner would take the chain over and
      -- reconcile would re-verify it under the wrong principal.
      update billing_private.store_purchases set product_id=p_data->>'productId',
        store_status=p_data->>'storeStatus',expires_at=(p_data->>'expiresAt')::timestamptz,
        environment=p_data->>'environment',last_store_event_at=now(),updated_at=now()
        where id=purchase.id;
    else
      -- First purchase on this chain. Unrelated to the quota: a new chain has no
      -- members yet. Concurrent first verifies still race on the unique
      -- (provider,environment,purchase_key_hash) constraint exactly as before;
      -- M4 neither widens nor fixes that.
      insert into billing_private.store_purchases(provider,environment,purchase_key_hash,
          store_reference_ciphertext,principal,product_id,store_status,expires_at,last_store_event_at)
        values('apple',p_data->>'environment',key_hash,p_data->>'storeReferenceCiphertext',actor,
          p_data->>'productId',p_data->>'storeStatus',(p_data->>'expiresAt')::timestamptz,now())
        returning id into new_purchase_id;
      if bind_device then
        insert into billing_private.purchase_devices(purchase_id,principal)
          values(new_purchase_id,actor);
      end if;
    end if;
    if p_data->>'claimId' is not null then
      update billing_private.billing_claims set status='verified',purchase_key_hash=key_hash,
        result_entitlement_revision=row.entitlement_revision+1,completed_at=now()
        where claim_id=(p_data->>'claimId')::uuid and principal=actor and status='pending';
    end if;
    perform billing_private.aggregate_entitlement(actor);
    select * into row from billing_private.account_entitlements where principal=actor;
    -- The admission gate guarantees the principal row exists, which is what
    -- quota_status requires (`select ... into strict`). The stored projection
    -- was just re-aggregated above, so this reads the fresh plan.
    quota := ai_private.quota_status(actor,false,coalesce((p_data->>'memberLimit')::integer,30));
    return jsonb_build_object('plan',row.plan,'status',row.status,
      'validUntil',billing_private.iso(row.valid_until),
      'serviceEndAt',billing_private.iso(row.service_end_at),
      'entitlementRevision',row.entitlement_revision,
      'aiQuota',jsonb_build_object('limit',(quota->>'limit')::integer,
        'used',(quota->>'used')::integer,
        'remaining',(quota->>'remaining')::integer,
        'resetsAt',quota->'resetsAt'),
      'billingSources',(select coalesce(jsonb_agg(jsonb_build_object('provider',sp.provider,
          'productId',sp.product_id,'expiresAt',billing_private.iso(sp.expires_at))),'[]'::jsonb)
        from billing_private.store_purchases sp
        join billing_private.purchase_devices pd on pd.purchase_id=sp.id
        where pd.principal=actor and pd.revoked_at is null
          and sp.store_status in ('active','billing_retry')));
  end if;

  if p_action='entitlement' then
    row := billing_private.ensure_account(actor);
    -- This action does not re-aggregate: it returns the stored projection, which
    -- is the accepted D5 gap. aiQuota is not part of that projection: it is read
    -- live from the ledger the AI path charges.
    quota := ai_private.quota_status(actor,false,coalesce((p_data->>'memberLimit')::integer,30));
    return jsonb_build_object('plan',row.plan,'status',row.status,
      'validUntil',billing_private.iso(row.valid_until),
      'serviceEndAt',billing_private.iso(row.service_end_at),
      'entitlementRevision',row.entitlement_revision,
      'aiQuota',jsonb_build_object('limit',(quota->>'limit')::integer,
        'used',(quota->>'used')::integer,
        'remaining',(quota->>'remaining')::integer,
        'resetsAt',quota->'resetsAt'),
      'billingSources',(select coalesce(jsonb_agg(jsonb_build_object('provider',sp.provider,
          'productId',sp.product_id,'expiresAt',billing_private.iso(sp.expires_at))),'[]'::jsonb)
        from billing_private.store_purchases sp
        join billing_private.purchase_devices pd on pd.purchase_id=sp.id
        where pd.principal=actor and pd.revoked_at is null
          and sp.store_status in ('active','billing_retry')));
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
    -- The inner join is kept: without an entitlement row the token cannot
    -- resolve to a principal, so listing the chain would only create events that
    -- are guaranteed to fail.
    return jsonb_build_object('chains',
      (select coalesce(jsonb_agg(jsonb_build_object('principal',sp.principal,
        'storeReferenceCiphertext',sp.store_reference_ciphertext,'productId',sp.product_id,
        'environment',sp.environment,'purchaseAccountToken',ae.purchase_account_token)),'[]'::jsonb)
       from billing_private.store_purchases sp
       join billing_private.account_entitlements ae on ae.principal=sp.principal
       where sp.principal is not null and sp.store_status in ('active','billing_retry')));
  end if;

  raise exception 'BILLING_UNKNOWN_ACTION';
end $$;
revoke all on function public.billing_service(text,jsonb) from public,anon,authenticated;
grant execute on function public.billing_service(text,jsonb) to service_role;
