-- Keep guest authentication independent from account linkage. Linked identities
-- share only a lifetime free pool; account sessions and Plus quota remain private.
-- Do not rewrite the deployed migrations: backfill existing claimed guests here.
create table ai_private.free_pools (
  id uuid primary key,
  used integer not null default 0 check(used >= 0)
);
alter table ai_private.free_pools enable row level security;
revoke all on ai_private.free_pools from public,anon,authenticated,service_role;
alter table ai_private.principals add column free_pool_id uuid not null default gen_random_uuid();
update ai_private.principals g set free_pool_id=a.free_pool_id
  from ai_private.principals a where g.claimed_by=a.user_id;
insert into ai_private.free_pools(id,used)
  select p.free_pool_id,coalesce(max(b.used),0)
  from ai_private.principals p left join ai_private.buckets b on b.principal=p.id and b.period='free'
  group by p.free_pool_id;
alter table ai_private.principals add foreign key(free_pool_id) references ai_private.free_pools(id)
  deferrable initially deferred;
create index ai_principals_free_pool on ai_private.principals(free_pool_id);

create function ai_private.create_free_pool() returns trigger
language plpgsql set search_path='' as $$
begin
  insert into ai_private.free_pools(id) values(new.free_pool_id) on conflict do nothing;
  return new;
end $$;
create trigger create_free_pool after insert on ai_private.principals
  for each row execute function ai_private.create_free_pool();

-- Buckets retain per-identity receipts for refunds and rollback. Only their
-- deltas affect the shared counter; deleting an account never refills its pool.
create function ai_private.update_free_pool() returns trigger
language plpgsql set search_path='' as $$
declare previous_used integer := 0;
begin
  if new.period <> 'free' then return new; end if;
  if tg_op='UPDATE' then previous_used := old.used; end if;
  update ai_private.free_pools set used=greatest(0,used+new.used-previous_used)
    where id=(select free_pool_id from ai_private.principals where id=new.principal);
  return new;
end $$;
create trigger update_free_pool after insert or update of used on ai_private.buckets
  for each row execute function ai_private.update_free_pool();
revoke all on function ai_private.create_free_pool() from public,anon,authenticated,service_role;
revoke all on function ai_private.update_free_pool() from public,anon,authenticated,service_role;

create or replace function ai_private.quota_status(actor text, dev_allowed boolean, member_limit integer) returns jsonb
language plpgsql set search_path='' as $$
declare p ai_private.principals; period_key text; quota_limit integer; used_count integer;
  resets timestamptz; instant timestamptz := clock_timestamp();
  ent_plan text; ent_status text; ent_tz text;
begin
  select * into strict p from ai_private.principals where id=actor;
  period_key := 'free'; quota_limit := p.free_limit;
  -- Formal Plus membership: entitlement-driven daily pool in the account
  -- timezone. The legacy development flag is retired and never grants quota.
  if p.user_id is not null then
    select plan, status, coalesce(account_timezone,'Asia/Shanghai')
      into ent_plan, ent_status, ent_tz
      from billing_private.account_entitlements
      where user_id = substring(actor from 9)::uuid;
    if ent_plan = 'plus' and ent_status in ('active','grace') then
      period_key := 'member:' || ((instant at time zone ent_tz)::date)::text;
      quota_limit := coalesce(member_limit,30);
      resets := ((instant at time zone ent_tz)::date + 1)::timestamp at time zone ent_tz;
    end if;
  end if;
  if period_key='free' then
    select used into used_count from ai_private.free_pools where id=p.free_pool_id;
  else
    select used into used_count from ai_private.buckets where principal=actor and period=period_key;
  end if;
  return jsonb_build_object('supportCode',p.support_code,'limit',quota_limit,
    'used',coalesce(used_count,0),'remaining',greatest(0,quota_limit-coalesce(used_count,0)),
    'enabled',false,
    'resetsAt',case when resets is null then null else
      to_char(resets at time zone ent_tz,'YYYY-MM-DD"T"HH24:MI:SS') || '+08:00' end,
    'period',period_key);
end $$;

create or replace function public.ai_quota_service(p_action text,p_data jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
  actor text := p_data->>'principal'; p ai_private.principals; guest ai_private.principals;
  q jsonb; r ai_private.requests; b_id bigint; item jsonb; new_actor text;
  dev_allowed boolean := coalesce((p_data->>'developmentAllowed')::boolean,false);
  request_key text := p_data->>'requestID'; attempt_id uuid := (p_data->>'attempt')::uuid;
  uid uuid; sid uuid; total integer; pool_id uuid; guest_pool_id uuid;
begin
  if p_action in ('import','finish_import') then
    perform 1 from ai_private.runtime for update;
    if p_action='finish_import' then
      update ai_private.runtime set legacy_import_complete=true where singleton;
      return jsonb_build_object('ok',true);
    end if;
    if (select legacy_import_complete from ai_private.runtime) then
      raise exception 'AI_IMPORT_ALREADY_CLOSED';
    end if;
    if actor !~ '^guest_[a-f0-9]{24}$' or actor is null then raise exception 'AI_INVALID_PRINCIPAL'; end if;
    insert into ai_private.principals(id,support_code,free_limit,development_enabled,import_hash)
    values(actor,p_data->>'supportCode',(p_data->>'limit')::integer,
      (p_data->>'developmentEnabled')::boolean,p_data->>'importHash') on conflict do nothing;
    get diagnostics total=row_count;
    if total=0 then
      if (select import_hash from ai_private.principals where id=actor) is distinct from p_data->>'importHash' then
        raise exception 'AI_IMPORT_CHANGED';
      end if;
      return jsonb_build_object('ok',true);
    end if;
    for item in select value from jsonb_array_elements(p_data->'buckets') loop
      insert into ai_private.buckets(principal,period,used)
        values(actor,item->>'period',(item->>'used')::integer);
    end loop;
    for item in select value from jsonb_array_elements(p_data->'completedRequests') loop
      insert into ai_private.requests(principal,request_id,attempt,state,expires_at)
        values(actor,item#>>'{}',gen_random_uuid(),'consumed',clock_timestamp()) on conflict do nothing;
    end loop;
    return jsonb_build_object('ok',true);
  end if;

  -- Claims change group membership; serialize them against short ledger RPCs,
  -- never against the model call itself. Other calls lock only their free pool.
  if p_action in ('claim','admin_reset_all') then
    perform 1 from ai_private.runtime for update;
  else
    perform 1 from ai_private.runtime for share;
  end if;
  if not (select legacy_import_complete from ai_private.runtime) then raise exception 'AI_IMPORT_REQUIRED'; end if;
  if p_action='ready' then return jsonb_build_object('ok',true); end if;
  if p_action in ('admin_status','admin_reset') then
    select id into actor from ai_private.principals where support_code=p_data->>'supportCode';
    if actor is null then return jsonb_build_object('code','SUPPORT_CODE_NOT_FOUND'); end if;
    -- A claimed installation keeps its legacy support code for support, but the
    -- live quota now belongs to the claiming account.
    select * into p from ai_private.principals where id=actor;
    if not found then return jsonb_build_object('code','SUPPORT_CODE_NOT_FOUND'); end if;
    if p.claimed and p.claimed_by is not null then actor := 'account:'||p.claimed_by::text; end if;
  elsif p_action='admin_reset_all' then
    -- Lock all actors in the same order as claims; outstanding attempts must settle first.
    perform 1 from ai_private.principals order by id for update;
    for new_actor in select id from ai_private.principals loop
      perform ai_private.expire_reservations(new_actor);
    end loop;
    if exists(select 1 from ai_private.requests where state='reserved') then
      return jsonb_build_object('code','AI_REQUEST_IN_PROGRESS');
    end if;
    update ai_private.buckets set used=0 where used > 0;
    update ai_private.free_pools set used=0 where used > 0;
    select count(*) into total from ai_private.principals;
    return jsonb_build_object('refreshedInstallations',total);
  end if;

  if p_action not in ('admin_status','admin_reset') then
    if actor ~ '^account:' then
      uid := substring(actor from 9)::uuid; sid := (p_data->>'sessionID')::uuid;
      if not exists(select 1 from auth.users u join auth.sessions s on s.user_id=u.id
        where u.id=uid and s.id=sid and u.email_confirmed_at is not null and not coalesce(u.is_anonymous,false))
        or exists(select 1 from sync_private.accounts where user_id=uid and deletion_pending) then
        return jsonb_build_object('code','ACCOUNT_UNAVAILABLE');
      end if;
    elsif actor !~ '^guest_[a-f0-9]{24}$' or actor is null then
      raise exception 'AI_INVALID_PRINCIPAL';
    end if;
    insert into ai_private.principals(id,user_id,support_code,free_limit)
      values(actor,uid,p_data->>'supportCode',case when uid is null then (p_data->>'freeLimit')::integer else 50 end)
      on conflict do nothing;
  end if;

  -- Link free pools without granting account authentication to the guest.
  if p_action='claim' then
    if uid is null or p_data->>'guest' is null or (p_data->>'guest') !~ '^guest_[a-f0-9]{24}$' then raise exception 'AI_INVALID_CLAIM'; end if;
    insert into ai_private.principals(id,support_code,free_limit)
      values(p_data->>'guest',p_data->>'guestSupportCode',(p_data->>'freeLimit')::integer) on conflict do nothing;
    perform 1 from ai_private.principals where id in (actor,p_data->>'guest') order by id for update;
    select * into guest from ai_private.principals where id=p_data->>'guest';
    if not found then return jsonb_build_object('code','AI_GUEST_NOT_FOUND'); end if;
    if guest.claimed then
      if guest.claimed_by=uid then return ai_private.quota_status(actor,false,coalesce((p_data->>'memberLimit')::integer,30)); end if;
      return jsonb_build_object('code','AI_GUEST_ALREADY_CLAIMED');
    end if;
    select free_pool_id into pool_id from ai_private.principals where id=actor;
    guest_pool_id := guest.free_pool_id;
    perform 1 from ai_private.free_pools where id in (pool_id,guest_pool_id) order by id for update;
    for new_actor in select id from ai_private.principals where free_pool_id in (pool_id,guest_pool_id) loop
      perform ai_private.expire_reservations(new_actor);
    end loop;
    if exists(select 1 from ai_private.requests req join ai_private.principals owners on owners.id=req.principal
      where owners.free_pool_id in (pool_id,guest_pool_id) and req.state='reserved') then
      return jsonb_build_object('code','AI_REQUEST_IN_PROGRESS');
    end if;
    update ai_private.free_pools set used=greatest(used,
      (select used from ai_private.free_pools where id=guest_pool_id)) where id=pool_id;
    update ai_private.principals set free_pool_id=pool_id where free_pool_id=guest_pool_id;
    delete from ai_private.free_pools where id=guest_pool_id and id<>pool_id;
    update ai_private.principals set claimed=true,claimed_by=uid,development_enabled=false where id=guest.id;
    return ai_private.quota_status(actor,false,coalesce((p_data->>'memberLimit')::integer,30));
  end if;

  select free_pool_id into pool_id from ai_private.principals where id=actor;
  perform 1 from ai_private.free_pools where id=pool_id for update;
  select * into p from ai_private.principals where id=actor for update;
  if not found then return jsonb_build_object('code','ACCOUNT_SERVICE_UNAVAILABLE'); end if;
  for new_actor in select id from ai_private.principals where free_pool_id=pool_id loop
    perform ai_private.expire_reservations(new_actor);
  end loop;
  if p_action='membership' then
    if not dev_allowed or p.user_id is not null then return jsonb_build_object('code','DEVELOPMENT_MEMBERSHIP_DISABLED'); end if;
    update ai_private.principals set development_enabled=(p_data->>'enabled')::boolean where id=actor;
  end if;
  q := ai_private.quota_status(actor,dev_allowed,coalesce((p_data->>'memberLimit')::integer,30));
  if p_action in ('status','membership','admin_status') then return q; end if;
  if p_action='admin_reset' then
    if exists(select 1 from ai_private.requests req join ai_private.principals owners on owners.id=req.principal
      where owners.free_pool_id=pool_id and req.state='reserved') then
      return jsonb_build_object('code','AI_REQUEST_IN_PROGRESS');
    end if;
    update ai_private.buckets set used=0 where principal=actor or
      (period='free' and principal in (select id from ai_private.principals where free_pool_id=pool_id));
    update ai_private.free_pools set used=0 where id=pool_id;
    return ai_private.quota_status(actor,dev_allowed,coalesce((p_data->>'memberLimit')::integer,30));
  end if;
  if p_action='reserve' then
    if request_key is null or length(request_key) not between 1 and 200 or attempt_id is null
      or coalesce(p_data->>'bodyHash','') !~ '^[a-f0-9]{64}$' then raise exception 'AI_INVALID_REQUEST'; end if;
    select requests.* into r from ai_private.requests requests
      join ai_private.principals owners on owners.id=requests.principal
      where owners.free_pool_id=pool_id and requests.request_id=request_key
        and (requests.principal=actor or requests.state in ('reserved','consumed'))
      order by case requests.state when 'consumed' then 0 when 'reserved' then 1 else 2 end
      limit 1;
    if found then
      if r.body_hash is not null and r.body_hash <> p_data->>'bodyHash' then
        return jsonb_build_object('code','AI_REQUEST_ID_CONFLICT');
      end if;
      if r.state='consumed' then return jsonb_build_object('code','AI_REQUEST_ALREADY_COMPLETED'); end if;
      if r.state='reserved' then
        if r.principal=actor and r.attempt=attempt_id then return q || jsonb_build_object('attempt',attempt_id); end if;
        return jsonb_build_object('code','AI_REQUEST_IN_PROGRESS');
      end if;
    end if;
    if (q->>'remaining')::integer=0 then
      return q || jsonb_build_object('code',case when q->>'period'='free' then 'AI_QUOTA_EXHAUSTED' else 'AI_DAILY_QUOTA_EXHAUSTED' end);
    end if;
    insert into ai_private.buckets(principal,period,used) values(actor,q->>'period',1)
      on conflict(principal,period) do update set used=ai_private.buckets.used+1 returning id into b_id;
    insert into ai_private.requests(principal,request_id,body_hash,bucket_id,attempt,state,expires_at)
      values(actor,request_key,p_data->>'bodyHash',b_id,attempt_id,'reserved',clock_timestamp()+interval '10 minutes')
      on conflict(principal,request_id) do update set bucket_id=excluded.bucket_id,attempt=excluded.attempt,
        state='reserved',expires_at=excluded.expires_at,body_hash=excluded.body_hash;
    return ai_private.quota_status(actor,dev_allowed,coalesce((p_data->>'memberLimit')::integer,30)) || jsonb_build_object('attempt',attempt_id);
  end if;
  if p_action='finish' then
    select * into r from ai_private.requests where principal=actor and request_id=request_key;
    if not found or r.attempt is distinct from attempt_id then return jsonb_build_object('code','AI_RESERVATION_EXPIRED'); end if;
    new_actor := case when (p_data->>'consume')::boolean then 'consumed' else 'refunded' end;
    if r.state=new_actor then return jsonb_build_object('ok',true); end if;
    if r.state <> 'reserved' then return jsonb_build_object('code','AI_RESERVATION_EXPIRED'); end if;
    update ai_private.requests set state=new_actor where principal=actor and request_id=request_key;
    if new_actor='refunded' then update ai_private.buckets set used=greatest(0,used-1) where id=r.bucket_id; end if;
    return jsonb_build_object('ok',true);
  end if;
  raise exception 'AI_UNKNOWN_ACTION';
end $$;
revoke all on function public.ai_quota_service(text,jsonb) from public,anon,authenticated;
grant execute on function public.ai_quota_service(text,jsonb) to service_role;


create or replace function ai_private.quota_status(actor text, dev_allowed boolean) returns jsonb
language sql set search_path='' as $$
  select ai_private.quota_status(actor,dev_allowed,30);
$$;

-- Billing reads the same shared free balance.
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
    select f.used into used_count from ai_private.free_pools f
      join ai_private.principals p on p.free_pool_id=f.id where p.id=actor;
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
    select f.used into used_count from ai_private.free_pools f
      join ai_private.principals p on p.free_pool_id=f.id where p.id=actor;
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

create or replace function public.ai_quota_export_legacy() returns jsonb
language sql security definer set search_path='' as $$
  select jsonb_build_object(
    'exportedAt', to_char(clock_timestamp() at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
    'principals', coalesce(jsonb_agg(entry order by id),'[]'::jsonb)
  )
  from (
    select p.id, jsonb_build_object(
      'principal', p.id,
      'supportCode', p.support_code,
      'developmentEnabled', p.development_enabled,
      'buckets', coalesce((
        select jsonb_agg(jsonb_build_object(
          'period', b.period,
          'used', greatest(0, b.used - coalesce((
            select count(*) from ai_private.requests r
            join ai_private.buckets rb on rb.id=r.bucket_id
            join ai_private.principals rp on rp.id=r.principal
            where r.state='reserved' and rb.period=b.period
              and (case when b.period='free' then rp.free_pool_id=p.free_pool_id else rp.id=p.id end)),0)),
          'limit', case when b.period='free' then p.free_limit else 50 end
        ) order by b.period)
        from (select 'free'::text period,f.used from ai_private.free_pools f where f.id=p.free_pool_id
          union all select period,used from ai_private.buckets where principal=p.id and period<>'free') b),'[]'::jsonb),
      'completedRequests', coalesce((
        select jsonb_agg(distinct r.request_id order by r.request_id)
        from ai_private.requests r join ai_private.principals rp on rp.id=r.principal
        where rp.free_pool_id=p.free_pool_id and r.state='consumed'),'[]'::jsonb)
    ) as entry
    from ai_private.principals p
    where p.user_id is null
  ) s;
$$;
revoke all on function public.ai_quota_export_legacy() from public,anon,authenticated;
grant execute on function public.ai_quota_export_legacy() to service_role;
