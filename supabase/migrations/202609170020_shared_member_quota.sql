-- E1: the daily Plus allowance is one counter per purchase chain, shared by that
-- chain's active member devices. Decision record:
-- docs/ai-quota-shared-scope-analysis-20260917.md section 5.0 (E1 share per
-- purchase chain, explicitly not per Supabase account; E4 the free tier is not
-- shared). Design: the same document, sections 3.1-3.5.
--
-- Mechanism. Membership already resolves by principal identity
-- (202609170017), but the member meter was read and written at (actor, period),
-- so two devices on one chain each got their own 30/day. The meter now lives at
-- the chain owner's principal -- billing_private.store_purchases.principal,
-- which 202609170017 fixes at first INSERT and deliberately never rewrites --
-- for every principal that is an active member of that chain
-- (purchase_devices.revoked_at is null). The existing three-active-device cap
-- therefore doubles as the quota-sharing boundary: no device-group concept, no
-- new table, no new column and no backfill are introduced, and no client change
-- is needed because the shipped membership page already renders whatever
-- aiQuota the billing RPC reports.
--
-- Best-effort control, stated plainly (202609170017's D2 note applies
-- verbatim): a device_id is client-asserted and a guest JWS is a copyable
-- bearer credential, so this is a product control, not enforcement. Sharing
-- converts entitlement theft into an availability attack on the group: a forged
-- or borrowed device_id on a bound chain can drain the whole group's daily 30
-- and lock the legitimate devices out. That is inherent to the decided
-- semantics, not a defect of this mechanism.
--
-- The stored projection is trusted as read. plus_source takes plan/status from
-- account_entitlements and does not re-check store_status/expires_at at read
-- time, because decision E5 is still open in the analysis. The accepted D5 gap
-- therefore keeps a stale Plus projection alive for every device on the chain,
-- and sharing widens its blast radius from one device to the chain's whole
-- daily 30. Closing it is a separate decision for the product owner.
--
-- One ledger, one number. ai_private.quota_status resolves both the member
-- period and the meter scope, and every consumer already calls it:
-- ai_quota_service status/reserve, billing_service entitlement/apple_verify and
-- therefore GET /billing/entitlement, and GET /api/account/quota. That is why
-- public.billing_service is deliberately NOT re-emitted here: 202609170019
-- removed its hand-read of free_limit and free_pools and made aiQuota a
-- projection of ai_private.quota_status, so the member-display fix (F2) is
-- preserved by construction rather than by re-emitting 250 lines. F2's
-- contract is unchanged: a member principal still gets a non-null resetsAt from
-- its entitlement timezone, a free principal still gets the lifetime pool with
-- resetsAt null. GET /api/account/quota is deliberately unchanged as well: an
-- account principal owns no chain membership (nothing can bind it to one), so
-- it keeps reading its merged free pool and keeps returning its supportCode --
-- see the "Membership quota scope" section of docs/account-api.md.
--
-- Locking, and why reserve changes. The pre-existing locks serialize one actor:
-- the actor's free-pool row and then the actor's principal row
-- (202609140016:199-201). They do not cover two different devices on one chain,
-- so both could read `remaining = 1` and both consume the last request. reserve
-- therefore reclaims expired holds on the shared meter, locks that row and
-- re-reads quota_status before its exhaustion check. The free branch is
-- untouched: its shared pool row lock is already correct, so its behaviour is
-- byte-identical.
--
-- Rollback is NOT lossless, and this file has no *_rollback.sql companion (it
-- changes no schema and no data, so the inverse is restoring the previous
-- function bodies from git, the rule 202609170019's header records). Restoring
-- them returns future requests to per-device metering and leaves the shared
-- rows in place. What it therefore does NOT restore: the per-device split of a
-- chain's usage, which is not recoverable from a row written under the chain
-- owner -- the owner's device keeps showing the group's used count and its
-- peers show 0, so each device appears to gain up to 30/day, and usage the
-- group already consumed is not undone. It also does not restore the previous
-- lock behaviour, does not touch 202609170017's purchase_devices, the
-- free-limit unification or the free-pool mechanism, and does not make
-- public.billing_service hand-read the ledger again (that fix stays). A
-- cutover rollback to the legacy SQLite authority (scripts/rollback-account-ai-
-- cutover.sh) reverts Plus quota to per-device metering too, because
-- quota_store.py is intentionally unchanged: it has no entitlement concept to
-- key a purchase chain on.
--
-- The reverse export does not silently drop post-cutover member usage. The
-- shared row sits at the chain owner, which is a guest principal, so
-- ai_quota_export_legacy emits it with the group's used count exactly as it
-- emitted the owner's own row before. What it cannot do is attribute a peer's
-- share back to the peer, which is the accepted E3 gap. The guard block below
-- is what keeps the switch itself honest: it refuses to apply while any member
-- row would be re-keyed onto a chain owner, so no usage is silently stranded.
--
-- Free tier (E4, explicitly out of scope). No free ledger, claim-guest or
-- free_pools behaviour changes here. A free principal reads and writes its own
-- pool; plus_source returns no row for it, so every free branch is unchanged.
--
-- 202609140016 and 202609130015. Production's applied migration set ends at
-- 202609140016; the migration that was never applied there is
-- 202609130015_free_quota_30, which is why live free_limit was still 50 for
-- about 150 rows plus one row at 3 (202609170017:31-40). 202609170017 re-issues
-- those three statements idempotently and must be applied first: this file is
-- written against its result (plus_source reads purchase_devices) and does not
-- repeat them, because duplicating them invites drift. Existing free rows are
-- not rewritten here; the guard block reports the live free_limit distribution
-- so the deploy log shows whether the unification took effect. Applying this
-- file without 202609170017 fails loudly at create time instead of drifting.
--
-- Deploy order (docs/account-api.md, membership-device-principal-implementation
-- -design "deployment order is not reversible"): apply migrations to hosted
-- Supabase first -- 202609170017, then 202609170018/019 (already merged), then
-- this file, in one maintenance window -- then scripts/billing-deploy.sh, then
-- watch DEVICE_REQUIRED / DEVICE_LIMIT_REACHED / ACCOUNT_TOKEN_UNKNOWN, and add
-- AI_DAILY_QUOTA_EXHAUSTED because a shared counter exhausts earlier for
-- multi-device members. The iOS rollout is unchanged and not gated by this
-- change.
--
-- Re-runnable: every object is create or replace or a guarded do block, and no
-- table and no row is rewritten.

-- ---------------------------------------------------------------------------
-- The member scope for one acting principal: which principal's bucket meters
-- its Plus allowance and in which timezone that allowance resets. Returns no
-- row for a principal without an active Plus projection, which is exactly the
-- pre-change free condition (202609170017:185). The chain is chosen with the
-- ordering billing_private.aggregate_entitlement already uses, so the meter
-- follows the same chain that produced the stored projection. When no chain
-- resolves the scope stays the actor, which keeps a projection that has no
-- purchase behind it (development/hand-seeded data, and the pre-M4 tests)
-- metered exactly where it was. A chain whose owner row is gone does not
-- resolve either: ai_private.buckets.principal has a foreign key to
-- ai_private.principals, so metering there would fail every member's reserve
-- instead of merely losing the sharing. That state is not reachable on the
-- normal path (a guest principal survives account deletion) but is reachable
-- for a legacy account-owned chain whose account was deleted.
-- ---------------------------------------------------------------------------
create or replace function billing_private.plus_source(p_actor text)
returns table(account_timezone text, scope text)
language sql security definer set search_path='' as $$
  select coalesce(ae.account_timezone,'Asia/Shanghai'),
         coalesce((
           select sp.principal
           from billing_private.purchase_devices pd
           join billing_private.store_purchases sp on sp.id = pd.purchase_id
           where pd.principal = p_actor and pd.revoked_at is null
             and sp.principal is not null
             and exists (select 1 from ai_private.principals pr where pr.id = sp.principal)
           order by (case sp.store_status when 'active' then 0 when 'billing_retry' then 1 else 2 end),
                    sp.expires_at desc nulls last, sp.id
           limit 1), p_actor)
  from billing_private.account_entitlements ae
  where ae.principal = p_actor and ae.plan = 'plus' and ae.status in ('active','grace');
$$;
revoke all on function billing_private.plus_source(text) from public,anon,authenticated,service_role;

-- ---------------------------------------------------------------------------
-- quota_status now reads the chain's meter. Body re-emitted from 202609170017
-- with only the membership lookup and the bucket predicate changed; every other
-- line and the returned keys are unchanged.
-- ---------------------------------------------------------------------------
create or replace function ai_private.quota_status(actor text, dev_allowed boolean, member_limit integer) returns jsonb
language plpgsql set search_path='' as $$
declare p ai_private.principals; period_key text; quota_limit integer; used_count integer;
  resets timestamptz; instant timestamptz := clock_timestamp();
  ent_tz text; meter_scope text;
begin
  select * into strict p from ai_private.principals where id=actor;
  period_key := 'free'; quota_limit := p.free_limit;
  -- Formal Plus membership: entitlement-driven daily pool in the account
  -- timezone. The legacy development flag is retired and never grants quota.
  -- The meter is the chain's, not the actor's: membership resolves through
  -- billing_private.plus_source, which also returns the principal that owns the
  -- purchase chain this actor is an active member of, and every device on that
  -- chain reads and writes that one counter (E1). plus_source returns no row
  -- when the actor has no active Plus projection, which leaves the free period
  -- and every free path untouched (E4).
  select ps.account_timezone, ps.scope into ent_tz, meter_scope
    from billing_private.plus_source(actor) ps;
  if meter_scope is not null then
    period_key := 'member:' || ((instant at time zone ent_tz)::date)::text;
    quota_limit := coalesce(member_limit,30);
    resets := ((instant at time zone ent_tz)::date + 1)::timestamp at time zone ent_tz;
  end if;
  if meter_scope is null then
    select used into used_count from ai_private.free_pools where id=p.free_pool_id;
  else
    select used into used_count from ai_private.buckets where principal=meter_scope and period=period_key;
  end if;
  return jsonb_build_object('supportCode',p.support_code,'limit',quota_limit,
    'used',coalesce(used_count,0),'remaining',greatest(0,quota_limit-coalesce(used_count,0)),
    'enabled',false,
    'resetsAt',case when resets is null then null else
      to_char(resets at time zone ent_tz,'YYYY-MM-DD"T"HH24:MI:SS') || '+08:00' end,
    'period',period_key);
end $$;
revoke all on function ai_private.quota_status(text,boolean,integer) from public,anon,authenticated,service_role;

create or replace function public.ai_quota_service(p_action text,p_data jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
  actor text := p_data->>'principal'; p ai_private.principals; guest ai_private.principals;
  q jsonb; r ai_private.requests; b_id bigint; item jsonb; new_actor text; meter_scope text;
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
    -- E1: reset the meter this device actually consumes, which for a Plus
    -- device is its chain's shared row, not only its own buckets.
    select ps.scope into meter_scope from billing_private.plus_source(actor) ps;
    update ai_private.buckets set used=0 where principal=coalesce(meter_scope,actor) or
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
    -- E1: one member meter is shared by every active device on the purchase
    -- chain, and the locks above are actor-scoped, so they do not serialize two
    -- devices on it. Reclaim expired holds on the shared row first (the only
    -- other chance to reclaim them is a quota call by their own free-pool
    -- group, and a device that is gone never makes one), then lock the row and
    -- re-read the quota, so the exhaustion check below decides on the locked
    -- value. Without the lock both devices observe `remaining = 1` and both
    -- consume it; without the re-read the check would still use the pre-lock
    -- value. The insert makes the row exist for `for update` to lock.
    -- The lock order stays principal -> free pool -> requests -> bucket, the
    -- same order the free path already uses, so this adds no new cycle.
    select ps.scope into meter_scope from billing_private.plus_source(actor) ps;
    if meter_scope is not null then
      with expired as (
        update ai_private.requests set state='refunded'
        where bucket_id=(select b.id from ai_private.buckets b
                         where b.principal=meter_scope and b.period=q->>'period')
          and state='reserved' and expires_at <= clock_timestamp()
        returning bucket_id
      ), totals as (select bucket_id,count(*)::integer n from expired group by bucket_id)
      update ai_private.buckets b set used=greatest(0,b.used-t.n) from totals t where b.id=t.bucket_id;
      insert into ai_private.buckets(principal,period,used) values(meter_scope,q->>'period',0)
        on conflict(principal,period) do nothing;
      perform 1 from ai_private.buckets where principal=meter_scope and period=q->>'period' for update;
      q := ai_private.quota_status(actor,dev_allowed,coalesce((p_data->>'memberLimit')::integer,30));
    end if;
    if (q->>'remaining')::integer=0 then
      return q || jsonb_build_object('code',case when q->>'period'='free' then 'AI_QUOTA_EXHAUSTED' else 'AI_DAILY_QUOTA_EXHAUSTED' end);
    end if;
    insert into ai_private.buckets(principal,period,used) values(coalesce(meter_scope,actor),q->>'period',1)
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


-- ---------------------------------------------------------------------------
-- Pre-conditions, reported in the deploy log. A member row that exists before
-- this change was metered per device; after it, the same device is metered on
-- its chain owner. Rows belonging to a device whose best chain is owned by
-- somebody else would therefore be re-keyed, so this fails closed instead of
-- silently stranding usage at a principal nothing reads any more. The check
-- picks the chain exactly as plus_source does. It stays silent on a re-run:
-- after the switch the shared row itself lives at the chain owner.
-- ---------------------------------------------------------------------------
do $$ declare member_rows bigint; rekeyed bigint; purchases bigint; plus_rows bigint;
  free_30 bigint; free_other bigint; begin
  select count(*) into member_rows from ai_private.buckets where period like 'member:%';
  select count(*) into rekeyed from ai_private.buckets b
    where b.period like 'member:%' and exists (
      select 1 from billing_private.purchase_devices pd
      join billing_private.store_purchases sp on sp.id = pd.purchase_id
      where pd.principal = b.principal and pd.revoked_at is null
        and sp.principal is not null and sp.principal is distinct from b.principal
        and exists (select 1 from ai_private.principals pr where pr.id = sp.principal)
      order by (case sp.store_status when 'active' then 0 when 'billing_retry' then 1 else 2 end),
               sp.expires_at desc nulls last, sp.id
      limit 1);
  select count(*) into purchases from billing_private.store_purchases;
  select count(*) into plus_rows from billing_private.account_entitlements where plan = 'plus';
  select count(*) filter (where free_limit = 30), count(*) filter (where free_limit is distinct from 30)
    into free_30, free_other from ai_private.principals;
  raise notice 'shared member quota: % member bucket rows (% at a non-owner chain member), % purchase chains, % plus entitlements, free_limit % at 30 / % other',
    member_rows, rekeyed, purchases, plus_rows, free_30, free_other;
  if rekeyed > 0 then
    raise exception 'shared member quota: % member bucket row(s) belong to a non-owner chain member and would be re-keyed onto the chain owner; re-key or remove them before applying (see this file''s header)', rekeyed;
  end if;
end $$;
