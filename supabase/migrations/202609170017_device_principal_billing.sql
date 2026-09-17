-- Membership binds to a device principal instead of a Time Fragment account
-- (product decision D1: reuse the existing guest_* shape; no device: prefix).
-- The billing tables and the billing RPC move from auth.users ids to principal
-- strings. Membership is deliberately independent of accounts.
--
-- Best-effort cap (D2, stated plainly): the principal is derived server-side
-- from a client-asserted device_id, so the three-device limit prevents misuse
-- and account-sharing mistakes. It is NOT cryptographic enforcement: anyone
-- who claims another device's device_id obtains that device's token and its
-- entitlement without spending a slot. Do not describe it as unforgeable.
--
-- The cap is per purchase chain (D4), keyed on
-- purchase_key_hash = sha256('apple|' || originalTransactionId). That key is a
-- server-side convention for "one purchase chain", not an Apple guarantee:
-- whether a resubscribe yields a new originalTransactionId is unverified
-- (design M0.5, pre-launch), so this migration assumes nothing about it either
-- way.
--
-- Revoked devices may rejoin (D3: on conflict do update set revoked_at=null).
-- That is why every call which leaves the principal non-revoked -- the first
-- join AND the revive -- must pass the quota check in billing_service below.
-- Without the revive branch a revoke/revive cycle grows the active set without
-- bound, silently defeating the cap.
--
-- Known gap (D5, accepted, pre-launch must-fix): only the acting principal is
-- re-aggregated. A worker updating a chain does not rebuild the entitlement
-- projection of that chain's other member devices, and the entitlement action
-- returns the stored projection without recomputing, so a refund or expiry
-- would leave devices 2..3 on plus until they verify again.
--
-- A4 unification: production's applied migration set ends at 202609140016 and
-- 202609130015_free_quota_30 was never applied there, so live
-- ai_private.principals.free_limit is still 50 (150 rows) plus one row at 3, and
-- ai_private.enforce_free_limit_30 does not exist. The product decision is to
-- unify on 30, following the repository, so the 015 statements are re-issued
-- below in idempotent form (create or replace / drop trigger if exists) and are
-- safe on a database which already applied 015. This REVERSES the implementation
-- design's §6.2 warning about copying a literal 50: the live value really is 50,
-- so leaving 015 unapplied would keep the wrong quota even after the
-- coalesce(free_limit,50) fallbacks are removed from the billing RPC.

-- ---------------------------------------------------------------------------
-- A4: apply 202609130015_free_quota_30 where it was skipped. Idempotent.
-- ---------------------------------------------------------------------------
alter table ai_private.principals alter column free_limit set default 30;

update ai_private.principals set free_limit = 30 where free_limit is distinct from 30;

create or replace function ai_private.enforce_free_limit_30() returns trigger
language plpgsql set search_path='' as $$
begin
  new.free_limit := 30;
  return new;
end $$;

drop trigger if exists enforce_free_limit_30 on ai_private.principals;
create trigger enforce_free_limit_30
before insert or update of free_limit on ai_private.principals
for each row execute function ai_private.enforce_free_limit_30();

revoke all on function ai_private.enforce_free_limit_30() from public,anon,authenticated,service_role;

-- Assumption A2 (verified on production 2026-09-17: zero account-owned chains,
-- zero plus entitlements, zero claims). Recorded, not enforced: the backfill
-- below is order-preserving, so the migration stays correct if it stops being
-- true. The R2 "existing paid accounts" acceptance branch is deliberately not
-- implemented.
do $$ declare chains bigint; plus_rows bigint; begin
  select count(*) into chains from billing_private.store_purchases where user_id is not null;
  select count(*) into plus_rows from billing_private.account_entitlements where plan = 'plus';
  raise notice 'device-principal migration: % account-owned chains, % plus entitlements backfilled', chains, plus_rows;
end $$;

-- ---------------------------------------------------------------------------
-- Billing tables key on principal strings. account-owned rows backfill as
-- 'account:' || user_id::text, the naming ai_private.principals already uses for
-- account principals (202609090006:21), so the cast is lossless and reversible.
-- ---------------------------------------------------------------------------
alter table billing_private.store_purchases drop constraint if exists store_purchases_user_id_fkey;
alter table billing_private.store_purchases rename column user_id to principal;
alter table billing_private.store_purchases alter column principal type text
  using case when principal is null then null else 'account:' || principal::text end;

alter table billing_private.billing_claims drop constraint if exists billing_claims_user_id_fkey;
alter table billing_private.billing_claims rename column user_id to principal;
alter table billing_private.billing_claims alter column principal type text
  using 'account:' || principal::text;
alter index billing_private.billing_claims_user rename to billing_claims_principal;

alter table billing_private.account_entitlements drop constraint if exists account_entitlements_user_id_fkey;
alter table billing_private.account_entitlements rename column user_id to principal;
alter table billing_private.account_entitlements alter column principal type text
  using case when principal is null then null else 'account:' || principal::text end;

-- Chain membership. No ^guest_ check on principal on purpose: backfilled
-- account-owned chains keep their 'account:<uuid>' rows, and the admission gate
-- in billing_service is what restricts NEW principals to the guest_* shape.
create table billing_private.purchase_devices (
  purchase_id bigint not null references billing_private.store_purchases(id) on delete cascade,
  principal   text   not null check(length(principal) between 8 and 64),
  bound_at    timestamptz not null default now(),
  revoked_at  timestamptz,
  primary key (purchase_id, principal)
);
create index purchase_devices_principal on billing_private.purchase_devices(principal);
-- 202609110010's 'revoke all on all tables in schema' is a one-shot statement and
-- does not cover tables created later, so revoke on this one explicitly.
alter table billing_private.purchase_devices enable row level security;
revoke all on billing_private.purchase_devices from public,anon,authenticated,service_role;

-- Order-preserving backfill: every chain which still has an owner becomes a
-- device row for that owner, so the device-joined aggregation below yields
-- exactly the entitlement the principal-keyed query yielded before. Chains whose
-- owner was deleted (principal NULL) stay ownerless, matching the previous
-- reconcile_list filter. bound_at uses the default now(): the real join time is
-- unknown, and sp.updated_at is the last renewal, not the join.
insert into billing_private.purchase_devices(purchase_id, principal)
  select sp.id, sp.principal from billing_private.store_purchases sp
  where sp.principal is not null
  on conflict (purchase_id, principal) do nothing;

-- Postgres cannot change a function's parameter types in place, so the uuid
-- overloads are dropped rather than replaced.
drop function billing_private.ensure_account(uuid);
drop function billing_private.aggregate_entitlement(uuid);

-- The 015 body also bootstrapped an ai_private.principals row with its own SQL
-- support-code derivation. That bootstrap is dropped here: /api/auth/guest
-- already creates the principal row (and the gate below requires it), keeping it
-- would require reverse-deriving a user_id from the principal prefix (the
-- account: branch the product decisions forbid), and its support-code derivation
-- duplicates account_backend.support_code().
create function billing_private.ensure_account(target_principal text)
returns billing_private.account_entitlements
language sql security definer set search_path='' as $$
  insert into billing_private.account_entitlements(principal, purchase_account_token)
    values(target_principal, gen_random_uuid())
    on conflict (principal) do nothing;
  select * from billing_private.account_entitlements where principal = target_principal;
$$;

-- Membership is derived from the chains this principal is an active member of.
-- A revoked device loses its plus projection the next time this runs.
create function billing_private.aggregate_entitlement(target_principal text) returns void
language plpgsql security definer set search_path='' as $$
declare best record;
begin
  perform 1 from billing_private.account_entitlements where principal=target_principal for update;
  select * into best from billing_private.store_purchases sp
    where exists (select 1 from billing_private.purchase_devices pd
                  where pd.purchase_id = sp.id and pd.principal = target_principal
                    and pd.revoked_at is null)
    order by (case sp.store_status when 'active' then 0 when 'billing_retry' then 1 else 2 end),
             sp.expires_at desc nulls last
    limit 1;
  update billing_private.account_entitlements set
    plan = case when best.store_status in ('active','billing_retry') then 'plus' else 'free' end,
    status = case best.store_status when 'active' then 'active'
      when 'billing_retry' then 'grace' when 'revoked' then 'revoked' else 'expired' end,
    valid_until = best.expires_at,
    entitlement_revision = entitlement_revision + 1,
    updated_at = now()
    where principal = target_principal;
end $$;

-- Membership resolves by principal identity, so it now works for device
-- principals too. Before M4 this branch was gated on p.user_id is not null,
-- which skipped every guest_* principal and silently left a paying device on the
-- free tier. The previous user_id = substring(actor from 9)::uuid lookup is
-- replaced by the identity lookup; every other branch is unchanged.
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
  select plan, status, coalesce(account_timezone,'Asia/Shanghai')
    into ent_plan, ent_status, ent_tz
    from billing_private.account_entitlements
    where principal = actor;
  if ent_plan = 'plus' and ent_status in ('active','grace') then
    period_key := 'member:' || ((instant at time zone ent_tz)::date)::text;
    quota_limit := coalesce(member_limit,30);
    resets := ((instant at time zone ent_tz)::date + 1)::timestamp at time zone ent_tz;
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

-- Billing reads the same shared free balance.
create or replace function public.billing_service(p_action text, p_data jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
  actor text := p_data->>'principal';
  row billing_private.account_entitlements; claim billing_private.billing_claims;
  purchase billing_private.store_purchases; link billing_private.purchase_devices;
  token uuid; claim_key uuid; new_purchase_id bigint;
  inserted integer; pool_limit integer; used_count integer;
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
    -- The admission gate guarantees the principal row exists and free_limit is
    -- not null, so no coalesce fallback is needed (and none may reintroduce a
    -- literal 50).
    select free_limit into pool_limit from ai_private.principals where id=actor;
    select f.used into used_count from ai_private.free_pools f
      join ai_private.principals p on p.free_pool_id=f.id where p.id=actor;
    return jsonb_build_object('plan',row.plan,'status',row.status,
      'validUntil',billing_private.iso(row.valid_until),
      'serviceEndAt',billing_private.iso(row.service_end_at),
      'entitlementRevision',row.entitlement_revision,
      'aiQuota',jsonb_build_object('limit',pool_limit,
        'used',coalesce(used_count,0),
        'remaining',greatest(0,pool_limit-coalesce(used_count,0)),
        'resetsAt',jsonb 'null'),
      'billingSources',(select coalesce(jsonb_agg(jsonb_build_object('provider',sp.provider,
          'productId',sp.product_id,'expiresAt',billing_private.iso(sp.expires_at))),'[]'::jsonb)
        from billing_private.store_purchases sp
        join billing_private.purchase_devices pd on pd.purchase_id=sp.id
        where pd.principal=actor and pd.revoked_at is null
          and sp.store_status in ('active','billing_retry')));
  end if;

  if p_action='entitlement' then
    row := billing_private.ensure_account(actor);
    -- Free-pool remainder lives in the shared AI ledger for the device principal.
    -- This action does not re-aggregate: it returns the stored projection, which
    -- is the accepted D5 gap.
    select free_limit into pool_limit from ai_private.principals where id=actor;
    select f.used into used_count from ai_private.free_pools f
      join ai_private.principals p on p.free_pool_id=f.id where p.id=actor;
    return jsonb_build_object('plan',row.plan,'status',row.status,
      'validUntil',billing_private.iso(row.valid_until),
      'serviceEndAt',billing_private.iso(row.service_end_at),
      'entitlementRevision',row.entitlement_revision,
      'aiQuota',jsonb_build_object('limit',pool_limit,
        'used',coalesce(used_count,0),
        'remaining',greatest(0,pool_limit-coalesce(used_count,0)),
        'resetsAt',jsonb 'null'),
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
revoke all on all functions in schema billing_private from public,anon,authenticated,service_role;
