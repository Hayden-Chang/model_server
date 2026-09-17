import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// Generates supabase/migrations/202609170099_billing_device_principal_down.sql, the
// manual reverse of the device-principal billing migration (017) plus the
// function-level parts of 019/020 and the 018 contract re-registration. The
// pre-017 function bodies are extracted from the applied migrations, never
// retyped, so the reverse cannot drift from the state it restores. `--check`
// fails when the committed file is stale, exactly like build-contract.mjs.
const root = new URL('../', import.meta.url);
const output = '202609170099_billing_device_principal_down.sql';
const source = name => readFileSync(new URL('migrations/' + name, root), 'utf8');

// One applied migration's complete `create [...] function ...` statement, up to
// and including its closing `$$;`. Applied migrations are frozen history
// (supabase/README.md: never rewrite an applied migration), so a missing or
// ambiguous marker means this generator and its inputs disagree: fail loudly
// instead of emitting a reverse that silently drops a body.
function statement(file, marker) {
  const sql = source(file);
  const start = sql.indexOf(marker);
  if (start < 0) throw new Error('function not found in ' + file + ': ' + marker);
  if (sql.indexOf(marker, start + 1) >= 0) throw new Error('ambiguous function in ' + file + ': ' + marker);
  const end = sql.indexOf('$$;', start);
  if (end < 0) throw new Error('unterminated function in ' + file + ': ' + marker);
  return sql.slice(start, end + 3);
}

// The two `insert into sync_private.contracts values (...)` statements of a
// generated contract migration. 202609110009_contract_v2.sql holds the payloads
// production has registered today; 202609170018_contract_v2_puzzle_optional.sql
// is the amendment this generator reverses.
function contracts(file) {
  const lines = source(file).split('\n')
    .filter(line => line.startsWith('insert into sync_private.contracts values ('));
  if (lines.length !== 2) throw new Error('expected 2 contract rows in ' + file + ', found ' + lines.length);
  return lines;
}
function payload(line) {
  const start = line.indexOf('$contract$');
  const end = line.lastIndexOf('$contract$');
  if (start < 0 || end <= start) throw new Error('unreadable contract payload');
  return JSON.parse(line.slice(start + '$contract$'.length, end));
}
// Reversing 018 only means anything if the two revisions really differ, and the
// payloads must come from 009: 202609110009_contract_v2_reregister.sql already
// carries 018's amendment, so pointing at it would reverse nothing.
function upsert(line) {
  const rewritten = line.replace(/\$contract\$::jsonb\);\s*$/,
    "$contract$::jsonb) on conflict (name) do update set schema = excluded.schema;");
  if (rewritten === line) throw new Error('contract statement is not in the expected generated shape');
  return rewritten;
}
const pre018 = contracts('202609110009_contract_v2.sql');
const post018 = contracts('202609170018_contract_v2_puzzle_optional.sql');
if (payload(pre018[0]).$id !== 'urn:daymosaic:cloud-state:2' || payload(pre018[1]).$id !== 'urn:daymosaic:operation:2'
  || !payload(pre018[0]).required.includes('puzzle')) {
  throw new Error('202609110009_contract_v2.sql is no longer the puzzle-required v2 revision');
}
if (payload(post018[0]).required.includes('puzzle')) {
  throw new Error('202609170018 no longer makes puzzle optional; this reverse would be a no-op');
}
const reregister = contracts('202609110009_contract_v2_reregister.sql');
if (payload(reregister[0]).required.includes('puzzle')) {
  throw new Error('202609110009_contract_v2_reregister.sql now holds the pre-018 payload; update the header note');
}

const ensureAccount = statement('202609130015_free_quota_30.sql',
  'create or replace function billing_private.ensure_account(target_user uuid)');
const aggregateEntitlement = statement('202609110012_billing_verify.sql',
  'create function billing_private.aggregate_entitlement(target_user uuid) returns void');
const quotaStatus = statement('202609140016_guest_free_pool.sql',
  'create or replace function ai_private.quota_status(actor text, dev_allowed boolean, member_limit integer) returns jsonb');
const quotaStatusShort = statement('202609140016_guest_free_pool.sql',
  'create or replace function ai_private.quota_status(actor text, dev_allowed boolean) returns jsonb');
const aiQuotaService = statement('202609140016_guest_free_pool.sql',
  'create or replace function public.ai_quota_service(p_action text,p_data jsonb) returns jsonb');
const billingService = statement('202609140016_guest_free_pool.sql',
  'create or replace function public.billing_service(p_action text, p_data jsonb) returns jsonb');

const sql = `-- ===========================================================================
-- MANUAL RECOVERY SCRIPT — NOT A FORWARD MIGRATION.
-- ===========================================================================
-- Reverses 202609170017_device_principal_billing.sql and the function-level parts
-- of 202609170019_billing_ai_quota_source.sql and
-- 202609170020_shared_member_quota.sql, and re-registers the pre-202609170018
-- schema v2 contracts. When it commits, the database is back on the shape
-- production runs today (migrations 001..016), which is the shape the pre-M4
-- containers expect.
--
-- It is not part of the forward migration history: supabase/tests/database.mjs
-- lists it in recoveryScripts so no test sweep applies it, and it must never be
-- handed to \`supabase db push\`. The supported way to run it is the wrapper, which
-- runs the guard below as a read-only preflight before it stops containers or
-- touches git:
--
--   scripts/billing-rollback-device-principal.sh <rollback-backups/billing-TS>
--
-- or by hand, as the single transaction this file defines:
--
--   psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f supabase/migrations/${output}
--
-- GENERATED FILE — DO NOT EDIT. Generated by
--   node supabase/scripts/build-rollback-functions.mjs
-- from applied migrations only:
--   202609170017_device_principal_billing.sql  the DDL inverted below
--   202609140016_guest_free_pool.sql           ai_private.quota_status,
--                                              public.ai_quota_service,
--                                              public.billing_service
--   202609130015_free_quota_30.sql             billing_private.ensure_account(uuid)
--   202609110012_billing_verify.sql            billing_private.aggregate_entitlement(uuid)
--   202609110009_contract_v2.sql               pre-018 contract payloads
-- node supabase/scripts/build-rollback-functions.mjs --check runs in
-- supabase/tests/protocol.test.mjs (npm run test:sync) and fails when this file
-- differs from the generator output, so hand-editing it breaks the gate instead of
-- silently drifting away from the migrations it inverts.
--
-- ATOMICITY. The file opens \`begin;\`, puts the guard first and ends with \`commit;\`.
-- A refusal or any error rolls the whole file back: a partially reversed database
-- is impossible. psql must be run with -v ON_ERROR_STOP=1; without it psql reports
-- success even when the transaction failed.
--
-- THE GUARD (fail-closed, read-only). The inverse mapping is exact only while every
-- billing row is account-owned. Pre-017 these tables key on auth.users(id), and the
-- only lossless inverse of 'account:' || user_id::text is
-- substring(principal from 9)::uuid. A device principal ('guest_<24hex>', the shape
-- M4 writes) has no user_id to recover, and a billing_private.purchase_devices row
-- has no pre-017 equivalent at all. Before the first ALTER the guard refuses when:
--   * any of billing_private.store_purchases / billing_claims /
--     account_entitlements holds a principal that is not 'account:' || <uuid>
--     (NULLs stay allowed where the pre-017 column was nullable: an ownerless
--     chain stays ownerless);
--   * billing_private.purchase_devices holds any row whose principal is not
--     'account:' || <uuid>;
--   * a recovered uuid no longer exists in auth.users, because the restored
--     foreign keys (store_purchases_user_id_fkey on delete set null,
--     billing_claims_user_id_fkey and account_entitlements_user_id_fkey on delete
--     cascade) could not be created.
-- The refusal names the offending counts, one example per table, and the operator's
-- options; nothing is modified.
--
-- WHAT IS RESTORED
--   * the three columns renamed back (principal -> user_id, text -> uuid, dropping
--     the 'account:' prefix), the three original foreign key constraint names with
--     their original on delete behaviour, and the index name billing_claims_user;
--   * billing_private.purchase_devices (+ its index, RLS setting and revokes) is
--     dropped: the pre-017 schema has no membership concept;
--   * ensure_account(uuid) (from 015) and aggregate_entitlement(uuid) (from 012)
--     replace their text versions;
--   * ai_private.quota_status(text,boolean,integer) and public.ai_quota_service
--     (from 016) and public.billing_service (from 016) replace the 017/019/020
--     bodies, with the revoke/grant statements the originals carried;
--   * billing_private.plus_source(text) (020) is dropped;
--   * sync_private.contracts 'cloud-state-v2' and 'operation-v2' are re-registered
--     from 202609110009_contract_v2.sql, i.e. the pre-018 puzzle-required payloads.
--     202609110009_contract_v2_reregister.sql is NOT that payload — it already
--     contains 018's amendment — so the payloads are generated from 009 here rather
--     than referenced. No RPC reads the v2 contract rows yet (supabase/README.md),
--     so this part is inert today; it fixes what a future migrate_sync_state would
--     validate against. If post-018 clients have already written v2 states without
--     the puzzle object, re-apply 202609170018 (an upsert) before any v2 write path
--     is wired.
--
-- THE A4 UNIFICATION TO free_limit 30 IS KEPT, DELIBERATELY.
-- 202609170017:31-40 re-issued 202609130015's statements because production's
-- applied set ends at 202609140016 and the 30 limit was never applied there: live
-- free_limit was 50 for about 150 rows plus one row at 3, and
-- ai_private.enforce_free_limit_30 did not exist. Unifying on 30 is a product
-- decision (017:31-40) that does not depend on whether membership is account- or
-- device-keyed, so this reverse keeps the column default, keeps the trigger and its
-- revoke, and does not rewrite ai_private.principals. Going back to 50 would restore
-- a value the product decision calls wrong, and would be a data change of its own
-- with its own migration. The pre-M4 code path still works; it simply observes 30.
--
-- NOT RESTORED, ON PURPOSE (ACL). ai_private.quota_status(text,boolean,integer)
-- stays revoked from public/anon/authenticated/service_role (202609170020:185).
-- Pre-017 it was never granted either — schema ai_private is revoked as a whole and
-- only the security-definer RPCs reach it — so the reverse does not re-grant.
--
-- WHAT THIS SCRIPT CANNOT DO (read before running it in anger)
--   * It cannot reverse 017 once a device principal owns billing data: the guard
--     refuses, because those rows cannot be represented pre-017. Fix forward is the
--     only lossless option; see the guard message.
--   * It does not roll back the deploy code, and the two must move together: the
--     restored RPCs read user_id, the M4 containers read principal. Use
--     scripts/billing-rollback-device-principal.sh, which stops the containers first
--     and restores the pre-deploy SHA immediately after this COMMIT.
--   * It does not re-derive per-device usage from 020's chain-shared member counters
--     (ai_private.buckets rows with period 'member:<date>' stay where 020 put them).
--     This is the same accepted limitation docs/account-api.md records for a 020
--     revert: the owner's device keeps showing the group's used count and its peers
--     show 0.
--   * It does not restore membership: an account-prefixed purchase_devices row is
--     dropped with its table. For a chain that still has an owner the entitlement is
--     unaffected (the restored aggregate_entitlement(uuid) re-derives it from
--     store_purchases.user_id), but a membership on a chain whose owner is NULL
--     simply disappears, and any stale projection it left behind is corrected on the
--     next apple_verify/aggregate call.
--   * It touches nothing outside 017/018/019/020: auth.users, ai_private.buckets,
--     ai_private.free_pools, ai_private.requests, ai_private.principals and
--     billing_private.billing_events are left exactly as they are.
--   * Verified against a local embedded PostgreSQL 17 with migrations 001..020
--     applied, including both guard refusals; it is not a substitute for running the
--     wrapper's preflight against the real database before it is needed.

begin;

-- >>> GUARD-BEGIN
-- Read-only. scripts/billing-rollback-device-principal.sh extracts exactly this
-- block for its preflight, so the safety decision has a single source of truth.
do $rollback_guard$
declare
  shape integer;
  problems text[] := '{}';
  n bigint;
  example text;
  device_rows bigint;
  chain_rows bigint;
  claim_rows bigint;
  entitlement_rows bigint;
begin
  -- Shape first: this file only reverses 017. If the renamed columns are absent the
  -- database is already reversed (nothing to do) or 017 was never applied.
  select count(*) into shape from information_schema.columns
    where table_schema = 'billing_private' and column_name = 'principal'
      and table_name in ('store_purchases','billing_claims','account_entitlements');
  if shape <> 3 then
    raise exception using message = format(
$rollback_msg$DEVICE_PRINCIPAL_ROLLBACK_WRONG_SHAPE: expected 3 billing_private principal columns (202609170017 applied), found %s.
Nothing was changed. Either the device-principal migration is not applied on this database
(there is nothing to reverse) or it has already been reversed.
$rollback_msg$, shape);
  end if;

  -- Every billing row must still be account-owned: 'account:' || auth.users.id.
  select count(*), min(principal) into n, example
    from billing_private.purchase_devices
    where principal !~ '^account:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
  if n > 0 then
    problems := problems || format('%s row(s) in billing_private.purchase_devices use a device principal (example %L); dropping that table deletes the membership', n, example);
  end if;

  select count(*), min(principal) into n, example
    from billing_private.store_purchases
    where principal is not null
      and principal !~ '^account:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
  if n > 0 then
    problems := problems || format('%s row(s) in billing_private.store_purchases are owned by a device principal (example %L); a purchase chain with no user_id cannot be restored', n, example);
  end if;

  select count(*), min(principal) into n, example
    from billing_private.billing_claims
    where principal !~ '^account:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
  if n > 0 then
    problems := problems || format('%s row(s) in billing_private.billing_claims belong to a device principal (example %L); billing_claims.user_id is not null and cannot stay empty', n, example);
  end if;

  select count(*), min(principal) into n, example
    from billing_private.account_entitlements
    where principal is not null
      and principal !~ '^account:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
  if n > 0 then
    problems := problems || format('%s row(s) in billing_private.account_entitlements belong to a device principal (example %L); the restored table is keyed on user_id', n, example);
  end if;

  if cardinality(problems) > 0 then
    raise exception using message = format(
$rollback_msg$DEVICE_PRINCIPAL_ROLLBACK_REFUSED: 202609170017 cannot be reversed on this database.
Why: pre-017 has nowhere to put a device principal. The inverse mapping is
     'account:' || user_id::text, so a 'guest_<24hex>' principal has no user_id to
     recover and every row below would have to be destroyed to continue.
Details:
  - %s
Options:
  1. Fix forward (recommended, and the only lossless choice): keep 202609170017
     applied and keep the M4 containers running, then fix the defect in place.
  2. If those rows are expendable (pre-launch or test data only), delete them and
     re-run this script or the wrapper:
       delete from billing_private.store_purchases where principal !~ '^account:';   -- cascades purchase_devices
       delete from billing_private.billing_claims where principal !~ '^account:';
       delete from billing_private.account_entitlements where principal !~ '^account:';
       delete from billing_private.purchase_devices where principal !~ '^account:';
     That destroys the paid entitlements, claims and memberships of those device
     principals. There is no supported re-map from a device principal to an account.
  3. Re-run afterwards; nothing has changed so far, and this guard runs first.
$rollback_msg$, array_to_string(problems, E'\\n  - '));
  end if;

  -- 017 dropped the foreign keys to auth.users and nothing re-creates them, so an
  -- account deleted during the M4 window leaves a dangling 'account:<uuid>'. The
  -- restored constraints cannot be created while such a row is present.
  select count(*), min(r.uid::text) into n, example
    from (select substring(principal from 9)::uuid as uid
            from billing_private.store_purchases where principal is not null
          union all select substring(principal from 9)::uuid
            from billing_private.billing_claims
          union all select substring(principal from 9)::uuid
            from billing_private.account_entitlements where principal is not null) r
    where not exists (select 1 from auth.users u where u.id = r.uid);
  if n > 0 then
    raise exception using message = format(
$rollback_msg$DEVICE_PRINCIPAL_ROLLBACK_REFUSED: 202609170017 cannot be reversed on this database.
Why: %s billing row(s) point at an auth.users account that no longer exists
     (example %s). The pre-017 schema restores foreign keys to auth.users
     (store_purchases_user_id_fkey on delete set null, billing_claims_user_id_fkey
     and account_entitlements_user_id_fkey on delete cascade), so they cannot be
     created while those rows are present.
Options:
  1. Clear them the way the pre-017 schema would have (a deleted account nulls its
     purchase chain and cascades its claim and entitlement rows):
       update billing_private.store_purchases set principal = null
         where principal is not null
           and substring(principal from 9)::uuid not in (select id from auth.users);
       delete from billing_private.billing_claims
         where substring(principal from 9)::uuid not in (select id from auth.users);
       delete from billing_private.account_entitlements
         where substring(principal from 9)::uuid not in (select id from auth.users);
  2. Or restore those accounts first, then re-run.
  3. Fix forward and keep 202609170017 applied.
Nothing has changed so far, and this guard runs first.
$rollback_msg$, n, example);
  end if;

  select count(*) into device_rows from billing_private.purchase_devices;
  select count(*) into chain_rows from billing_private.store_purchases where principal is not null;
  select count(*) into claim_rows from billing_private.billing_claims;
  select count(*) into entitlement_rows from billing_private.account_entitlements;
  raise notice 'device-principal reverse preflight: % chain(s), % claim(s) and % entitlement(s) map back to auth.users ids; % purchase_devices row(s) are dropped (the pre-017 schema has no membership table)',
    chain_rows, claim_rows, entitlement_rows, device_rows;
end $rollback_guard$;
-- <<< GUARD-END

-- ---------------------------------------------------------------------------
-- 202609170018 reversal: re-register the pre-018 puzzle-required v2 contracts.
-- Generated from 202609110009_contract_v2.sql and rewritten as an upsert, so the
-- statement is correct whether the original or the amended payload is registered.
-- ---------------------------------------------------------------------------
${pre018.map(upsert).join('\n')}

-- ---------------------------------------------------------------------------
-- 202609170017 inverse DDL. Order, and why it is this order:
--   1. Drop the three functions bound to the renamed columns before the renames.
--      Not because the DDL would be refused: verified on local PostgreSQL 17, both
--      RENAME COLUMN and ALTER COLUMN TYPE succeed while a SQL-language body still
--      references the old name, and the function then fails at call time with
--      "column sp.principal does not exist" (plpgsql bodies are not tracked at all).
--      Dropping them first keeps every state of this transaction consistent, and it
--      is required anyway because 017 cannot change a parameter type in place: the
--      text versions must go before the uuid versions can be created.
--   2. purchase_devices goes before the renames. It is 017's own object and carries
--      the FK to store_purchases; nothing the pre-017 code calls reads it, and the
--      restored aggregate_entitlement(uuid) derives entitlement from the chain owner
--      instead. The DROP is order-independent (verified: it succeeds while the
--      post-017 plpgsql body still references the table); dropping it first keeps
--      the intermediate states of this transaction readable.
--   3. rename, retype, then restore the FK constraints and the index name.
--   4. recreate the pre-017 bodies. The SQL-language ensure_account(uuid) must come
--      after the renames: SQL bodies are parsed and validated at CREATE time
--      (verified: creating it while the column is still named principal fails with
--      "column user_id does not exist"), so the column has to be a uuid first. The
--      ACLs are re-issued afterwards.
-- ---------------------------------------------------------------------------
drop function if exists billing_private.plus_source(text);
drop function if exists billing_private.ensure_account(text);
drop function if exists billing_private.aggregate_entitlement(text);

-- 017's membership table, with its index, row level security setting and revokes.
drop table if exists billing_private.purchase_devices;

-- 'account:' is 8 characters, so substring(x from 9) is the inverse of 017's
-- 'account:' || user_id::text, and the NULL branches keep ownerless rows ownerless.
alter table billing_private.store_purchases rename column principal to user_id;
alter table billing_private.store_purchases alter column user_id type uuid
  using case when user_id is null then null else substring(user_id from 9)::uuid end;
alter table billing_private.store_purchases
  add constraint store_purchases_user_id_fkey foreign key (user_id) references auth.users(id) on delete set null;

alter table billing_private.billing_claims rename column principal to user_id;
alter table billing_private.billing_claims alter column user_id type uuid
  using substring(user_id from 9)::uuid;
alter index billing_private.billing_claims_principal rename to billing_claims_user;
alter table billing_private.billing_claims
  add constraint billing_claims_user_id_fkey foreign key (user_id) references auth.users(id) on delete cascade;

alter table billing_private.account_entitlements rename column principal to user_id;
alter table billing_private.account_entitlements alter column user_id type uuid
  using case when user_id is null then null else substring(user_id from 9)::uuid end;
alter table billing_private.account_entitlements
  add constraint account_entitlements_user_id_fkey foreign key (user_id) references auth.users(id) on delete cascade;

-- ---------------------------------------------------------------------------
-- Pre-017 function bodies, extracted verbatim at build time:
--   ensure_account(uuid)        <- 202609130015_free_quota_30.sql
--   aggregate_entitlement(uuid) <- 202609110012_billing_verify.sql
--   quota_status / ai_quota_service / billing_service <- 202609140016_guest_free_pool.sql
-- These are the definitions production runs today. 019 and 020 replaced bodies
-- only, so restoring 016's bodies is the exact inverse of both.
-- ---------------------------------------------------------------------------
${ensureAccount}

${aggregateEntitlement}

${quotaStatus}

-- Already present in every post-016 database; re-stated so the restored function
-- set is complete and matches 016 exactly.
${quotaStatusShort}

${aiQuotaService}
revoke all on function public.ai_quota_service(text,jsonb) from public,anon,authenticated;
grant execute on function public.ai_quota_service(text,jsonb) to service_role;

${billingService}
revoke all on function public.billing_service(text,jsonb) from public,anon,authenticated;
grant execute on function public.billing_service(text,jsonb) to service_role;
-- The one-shot revoke 016 used to cover the billing_private helpers it relies on.
revoke all on all functions in schema billing_private from public,anon,authenticated,service_role;

-- ---------------------------------------------------------------------------
-- KEPT DELIBERATELY: the A4 unification to free_limit 30 (see the header). The
-- column default, the trigger and the rows are already in the unified state, so
-- only the trigger's ACL is re-asserted here; nothing in ai_private.principals is
-- rewritten and the default is not reset to 50.
-- ---------------------------------------------------------------------------
revoke all on function ai_private.enforce_free_limit_30() from public,anon,authenticated,service_role;

-- Success summary for the server log, inside the same transaction so a failure here
-- still rolls the whole reverse back.
do $rollback_summary$
declare
  chains bigint; claims bigint; entitlements bigint;
begin
  select count(*) into chains from billing_private.store_purchases where user_id is not null;
  select count(*) into claims from billing_private.billing_claims;
  select count(*) into entitlements from billing_private.account_entitlements;
  raise notice 'device-principal reverse applied: % account-owned chain(s), % claim(s), % entitlement(s); the pre-017 RPCs and the pre-018 contracts are live — restore the pre-deploy code in the same window',
    chains, claims, entitlements;
end $rollback_summary$;

-- Verification (read-only, run after COMMIT):
--   select table_name, column_name, data_type from information_schema.columns
--     where table_schema = 'billing_private'
--       and table_name in ('store_purchases','billing_claims','account_entitlements')
--       and column_name in ('user_id','principal') order by 1, 2;
--   select conname, confdeltype from pg_constraint
--     where conname in ('store_purchases_user_id_fkey','billing_claims_user_id_fkey',
--                       'account_entitlements_user_id_fkey');
--   select indexname from pg_indexes
--     where schemaname = 'billing_private' and indexname like 'billing_claims_%';
--   select count(*) from information_schema.tables
--     where table_schema = 'billing_private' and table_name = 'purchase_devices';
--   select p.proname, pg_get_function_identity_arguments(p.oid) from pg_proc p
--     join pg_namespace n on n.oid = p.pronamespace
--     where (n.nspname = 'billing_private' and p.proname in ('ensure_account','aggregate_entitlement','plus_source'))
--        or (n.nspname = 'public' and p.proname in ('billing_service','ai_quota_service')) order by 1, 2;
--   select name, schema -> 'required' ? 'puzzle' as puzzle_required
--     from sync_private.contracts order by name;   -- cloud-state-v2 must be true again

commit;
`;

const target = new URL('migrations/' + output, root);
if (process.argv.includes('--check')) {
  if (readFileSync(target, 'utf8') !== sql) throw new Error('Rollback migration is stale: ' + output);
} else {
  writeFileSync(fileURLToPath(target), sql);
}
