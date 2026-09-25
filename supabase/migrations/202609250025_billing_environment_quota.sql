-- Forward-only follow-up to 024. Pause both API/worker writers during rollout.
-- Legacy member counters came from the sandbox-only deployment. Refuse to guess
-- if an owner also has production purchases or a destination bucket exists.
begin;
lock table ai_private.buckets in access exclusive mode;
do $$ begin
  if to_regprocedure('public.billing_service_unscoped(text,jsonb)') is null
     or to_regprocedure('public.ai_quota_service_unscoped(text,jsonb)') is null then
    raise exception 'BILLING_ENVIRONMENT_MIGRATION_024_REQUIRED';
  end if;
  if exists (
    select 1 from ai_private.buckets b
    join billing_private.store_purchases sp on sp.principal=b.principal
    where b.period ~ '^member:[0-9]{4}-[0-9]{2}-[0-9]{2}$'
      and sp.environment='production'
  ) then
    raise exception 'BILLING_LEGACY_QUOTA_ENVIRONMENT_AMBIGUOUS';
  end if;
  if exists (
    select 1 from ai_private.buckets old join ai_private.buckets scoped
      on scoped.principal=old.principal
      and scoped.period=replace(old.period,'member:','member:sandbox:')
    where old.period ~ '^member:[0-9]{4}-[0-9]{2}-[0-9]{2}$'
  ) then
    raise exception 'BILLING_LEGACY_QUOTA_DESTINATION_EXISTS';
  end if;
end $$;
-- Keep the primary key and outstanding request references, including refunds.
update ai_private.buckets set period=replace(period,'member:','member:sandbox:')
where period ~ '^member:[0-9]{4}-[0-9]{2}-[0-9]{2}$';

create or replace function ai_private.quota_status(actor text, dev_allowed boolean, member_limit integer) returns jsonb
language plpgsql set search_path='' as $$
declare p ai_private.principals; period_key text; quota_limit integer; used_count integer;
  resets timestamptz; instant timestamptz := clock_timestamp();
  ent_tz text; meter_scope text; reset_offset integer;
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
    period_key := 'member:' || coalesce(nullif(current_setting('app.billing_environment',true),''),'sandbox')
      || ':' || ((instant at time zone ent_tz)::date)::text;
    quota_limit := coalesce(member_limit,30);
    resets := ((instant at time zone ent_tz)::date + 1)::timestamp at time zone ent_tz;
    -- F3: ent_tz's real UTC offset at that reset instant, in seconds. `resets`
    -- is read here and never rewritten, so the reset moment is unchanged.
    reset_offset := extract(epoch from ((resets at time zone ent_tz)::timestamp at time zone 'UTC') - resets)::int;
  end if;
  if meter_scope is null then
    select used into used_count from ai_private.free_pools where id=p.free_pool_id;
  else
    select used into used_count from ai_private.buckets where principal=meter_scope and period=period_key;
  end if;
  return jsonb_build_object('supportCode',p.support_code,'limit',quota_limit,
    'used',coalesce(used_count,0),'remaining',greatest(0,quota_limit-coalesce(used_count,0)),
    'enabled',false,
    -- F3: the wall clock in ent_tz, labelled with ent_tz's real UTC offset at
    -- that instant. A zone whose offset differs from +08:00, and a zone whose
    -- offset differs between winter and summer, both now label correctly.
    'resetsAt',case when resets is null then null else
      to_char(resets at time zone ent_tz,'YYYY-MM-DD"T"HH24:MI:SS')
      || case when reset_offset < 0 then '-' else '+' end
      || lpad((abs(reset_offset) / 3600)::int::text,2,'0')
      || ':' || lpad((abs(reset_offset) % 3600 / 60)::int::text,2,'0') end,
    'period',period_key);
end $$;
revoke all on function ai_private.quota_status(text,boolean,integer) from public,anon,authenticated,service_role;

-- Deployment capability probe; no user data and no access for public clients.
create or replace function public.billing_environment_schema() returns integer
language sql set search_path='' as $$ select 25 $$;
revoke all on function public.billing_environment_schema() from public,anon,authenticated;
grant execute on function public.billing_environment_schema() to service_role;
commit;
