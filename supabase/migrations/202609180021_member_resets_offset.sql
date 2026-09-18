-- F3: the member `resetsAt` ISO 8601 label now carries the entitlement's real
-- UTC offset instead of a hardcoded `+08:00`.
--
-- The defect. 202609170020 (and every definition before it, back to
-- 202609110014) rendered the label as
--   to_char(resets at time zone ent_tz,'YYYY-MM-DD"T"HH24:MI:SS') || '+08:00'
-- The reset *instant* was already correct -- it is the entitlement timezone's
-- next local midnight -- but the offset text was a literal. `account_timezone`
-- defaults to `Asia/Shanghai` (202609110014:7-9) and nothing in this repository
-- ever writes another value, so production numbers are right today by accident.
-- The moment one entitlement row carries a different zone, the label claims
-- +08:00 while the instant sits elsewhere: a client that parses `resetsAt`
-- (iOS `MembershipEntitlementModels.swift` does) computes the wrong reset
-- moment, in the worst case showing a full day of remaining allowance that the
-- ledger has already expired. `Asia/Shanghai` has no DST, so the shipped label
-- is self-consistent; a zone with DST is not even self-consistent across the
-- year, which is why the fix must read the offset at the reset instant.
--
-- Why this cannot reuse `to_char(..., 'OF')`. PostgreSQL's `to_char` offset codes
-- are rendered against the *session* `TimeZone` GUC, not against the entitlement
-- zone, for every form of the input. Measured on PostgreSQL 17.7 (the pinned test
-- runtime), session zone fixed at `Asia/Shanghai` and `z` in turn
-- `Asia/Shanghai`, `Asia/Kolkata`, `Asia/Kathmandu`, `Australia/Eucla`,
-- `Etc/GMT+12`:
--   to_char((resets at time zone z) at time zone z, 'OF') -> '+08'   for ALL of them
-- Changing only the session moves it: with the same input, `set time zone 'UTC'`
-- gives '+00' and `set time zone 'America/New_York'` gives '-04'. The entitlement
-- zone is therefore not what 'OF' reads, so 'OF' is unusable here; it is also
-- unusable on its own terms, because it omits the minutes when they are zero
-- ('+08', not '+08:00') and so cannot produce the `±HH:MM` form the field's
-- ISO 8601 shape requires. The offset is therefore derived from the instant
-- itself:
--   (resets at time zone ent_tz)                       -- local wall clock, no zone
--   ((...)::timestamp at time zone 'UTC')              -- that wall clock READ AS UTC
--   that value - resets                                -- = the zone's real offset
-- `extract(epoch from ...)` reduces it to whole seconds, and the label is built
-- from the sign and the absolute hour/minute split, so '+08:00', '-05:00',
-- '+05:30', '+05:45', '+08:45' and '+12:45' all come out as the required
-- `±HH:MM`. This is session-independent: the same expression returns '+05:30'
-- for `Asia/Kolkata` under session zones UTC, America/New_York, Asia/Kolkata,
-- Pacific/Kiritimati and Etc/GMT+12.
--
-- What changes, exactly. Only the `resetsAt` label. `period_key`, `quota_limit`,
-- `resets`, `used`, `remaining`, `supportCode` and `enabled` are byte-identical,
-- because the instant is still `((instant at time zone ent_tz)::date + 1)::timestamp
-- at time zone ent_tz` -- the new expression reads that same `resets` variable
-- and never rewrites it. Verified against Node's `Intl` zone database over 12
-- zones x 6 instants (DST transitions in both hemispheres, whole-hour zones,
-- half-hour offsets, the 45-minute zones `Asia/Kathmandu`/`Pacific/Chatham`/
-- `Australia/Eucla`, and the `Etc/GMT+12` sign inversion): 72/72 labels match
-- both the wall clock and the parsed instant. The free path is untouched: `resets` stays null unless
-- `plus_source` resolved a chain scope, so a free principal still reports
-- `resetsAt: null` (E4), and `billing_service` reaches the same value by
-- construction because its `aiQuota` is a projection of this function
-- (202609170019:184-192). No public signature, column, table, index or row
-- changes, and no client change is needed: the field keeps its ISO 8601 shape,
-- which is what the shipped client already parses.
--
-- 202609170099 is deliberately NOT touched. It is the manual *reverse* migration
-- for the device-principal rollout, its version sorts after every forward
-- migration, and it must stay byte-reproducible so a rollback replays exactly
-- what it replayed before; its copy of the old literal is historical. The fix
-- belongs to the forward definition only, which is what this file re-emits.
--
-- Rollback. Like 202609170019 and 202609170020, this file changes no schema and
-- no data, so it has no *_rollback.sql companion; the inverse is restoring the
-- previous `ai_private.quota_status` body from git (the rule 202609170020:58-61
-- records). Restoring it reinstates the hardcoded `+08:00` label and therefore
-- the defect. It requires no data repair: the ledger, the meter rows and the
-- reset instants this function computes are unaffected by this change, and no
-- stored value is derived from the label.
--
-- Deploy order. Apply after 202609170020, which is what created
-- `billing_private.plus_source`; this body calls it and fails loudly at create
-- time if it is missing. No maintenance window is needed: it is one
-- `create or replace` of a function no client calls directly, and its only
-- observable effect is a corrected offset label.
--
-- Re-runnable: one `create or replace` plus the privilege re-issue, no table and
-- no row is rewritten. `revoke all` is repeated so a re-run cannot widen access.

-- ---------------------------------------------------------------------------
-- Body re-emitted from 202609170020 with only the `resetsAt` expression
-- changed; every other line, the declared variables, the resolved scope and the
-- returned keys are unchanged.
-- ---------------------------------------------------------------------------
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
    period_key := 'member:' || ((instant at time zone ent_tz)::date)::text;
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

-- ---------------------------------------------------------------------------
-- Pre-conditions, reported in the deploy log: what this change can and cannot
-- see. `resetsAt` is only rendered for a member, so a database with no Plus
-- entitlement exercises nothing here; the count makes that explicit instead of
-- leaving a silent no-op in the log. A member row is reported with the zone its
-- label will now be derived from, so the log shows whether any live row leaves
-- the `Asia/Shanghai` default. Nothing is rejected: unlike 202609170020 this
-- change rewrites no row and re-keys no meter, so there is no state it could
-- strand.
-- ---------------------------------------------------------------------------
do $$ declare plus_rows bigint; non_default_zones bigint; distinct_zones bigint; begin
  select count(*), count(*) filter (where coalesce(account_timezone,'Asia/Shanghai') <> 'Asia/Shanghai'),
         count(distinct account_timezone)
    into plus_rows, non_default_zones, distinct_zones
    from billing_private.account_entitlements where plan = 'plus' and status in ('active','grace');
  raise notice 'member resets offset: % active plus entitlement(s) in % distinct timezone(s), % outside the Asia/Shanghai default',
    plus_rows, distinct_zones, non_default_zones;
end $$;
