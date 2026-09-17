// Manual verification harness for the device-principal reverse migration
// (migrations/202609170099_billing_device_principal_down.sql).
//
// Not part of the forward test sweep and not run by `npm run test:sync` on
// purpose: it applies ALL migrations including the recovery script, so it must
// never be wired into the normal gate. Run it by hand after changing the reverse
// migration or its generator:
//
//   cd supabase && node scripts/verify-rollback-roundtrip.mjs
//
// It applies migrations 001..020 to a local embedded PostgreSQL, seeds
// account-owned billing data, proves each guard refusal leaves the schema
// untouched, runs the manual reverse, and asserts the pre-017 shape from
// information_schema/pg_catalog rather than by eye.
//
// Resolves the repo root from this file so it is not tied to one worktree.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { database } from '../tests/database.mjs';

import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const DOWN = readFileSync(REPO + '/supabase/migrations/202609170099_billing_device_principal_down.sql', 'utf8');
const PRODUCT = 'com.hayden.daymosaic.plus.monthly';
const GUEST = 'guest_' + 'c'.repeat(24);

const db = await database();
const q = (sql, values = []) => db.admin.query(sql, values);
const rows = async (sql, values = []) => (await q(sql, values)).rows;
const one = async (sql, values = []) => (await rows(sql, values))[0];
const group = name => console.log('\n== ' + name);

async function applyDown(label) {
  try {
    await q(DOWN);
    return {ok: true};
  } catch (error) {
    await drain();
    return {ok: false, message: String(error.message)};
  }
}
async function drain() {
  // A multi-statement simple query that fails mid-file leaves the session in an
  // aborted transaction (the trailing COMMIT is skipped); psql closes the
  // connection instead, which rolls back. Reset here so the harness can continue.
  try { await q('rollback'); } catch {}
}
async function rejection(sql, expect) {
  try {
    await q(sql);
    await drain();
    throw new Error('expected rejection containing ' + expect);
  } catch (error) {
    await drain();
    if (!String(error.message).includes(expect)) throw error;
    return String(error.message);
  }
}
const columns = async name => (await rows(
  `select table_name,data_type,is_nullable from information_schema.columns
    where table_schema='billing_private' and column_name=$1 order by table_name`, [name]));
const signatureExists = async (schema, name, args) => (await one(
  `select count(*)::int n from pg_proc p join pg_namespace n2 on n2.oid=p.pronamespace
    where n2.nspname=$1 and p.proname=$2 and pg_get_function_identity_arguments(p.oid)=$3`,
  [schema, name, args])).n === 1;

try {
  group('precondition: migrations 001..020 applied, post-017 shape');
  assert.deepEqual((await columns('principal')).map(r => r.table_name),
    ['account_entitlements', 'billing_claims', 'purchase_devices', 'store_purchases']);
  assert.equal(await signatureExists('billing_private', 'ensure_account', 'target_principal text'), true);
  assert.equal(await signatureExists('billing_private', 'plus_source', 'p_actor text'), true);
  assert.equal((await one(`select (schema->'required') ? 'puzzle' as puzzle_required
    from sync_private.contracts where name='cloud-state-v2'`)).puzzle_required, false);
  console.log('ok: principal columns, text functions, puzzle-optional contract are all present');

  group('seed account-owned billing data (plus one account-prefixed device row)');
  const owner = await db.account();
  const other = await db.account();
  const ownerDevice = await owner.device();
  const acct = id => 'account:' + id;
  await q(`insert into billing_private.store_purchases
    (provider,environment,purchase_key_hash,store_reference_ciphertext,principal,product_id,store_status,
     expires_at,last_store_event_at)
    values('apple','production',$1,'cipher',$2,$3,'active','2026-12-01T00:00:00Z',now())`,
    ['a'.repeat(64), acct(owner.id), PRODUCT]);
  await q(`insert into billing_private.store_purchases
    (provider,environment,purchase_key_hash,store_reference_ciphertext,principal,product_id,store_status)
    values('apple','sandbox',$1,'cipher',$2,$3,'expired')`, ['b'.repeat(64), acct(other.id), PRODUCT]);
  await q(`insert into billing_private.store_purchases
    (provider,environment,purchase_key_hash,store_reference_ciphertext,principal,product_id,store_status)
    values('apple','sandbox',$1,'cipher',null,$2,'expired')`, ['d'.repeat(64), PRODUCT]);
  const token = randomUUID();
  await q(`insert into billing_private.account_entitlements
    (principal,purchase_account_token,plan,status,valid_until,entitlement_revision,account_timezone)
    values($1,$2,'plus','active','2026-12-01T00:00:00Z',7,'Asia/Shanghai')`, [acct(owner.id), token]);
  await q(`insert into billing_private.account_entitlements(principal,plan,status)
    values($1,'free','expired')`, [acct(other.id)]);
  await q(`insert into billing_private.billing_claims
    (claim_id,principal,provider,product_id,expected_account_identifier_hash,request_hash)
    values($1,$2,'apple',$3,$4,$5)`, [randomUUID(), acct(owner.id), PRODUCT, 'e'.repeat(64), 'f'.repeat(64)]);
  // 017's own backfill shape: an account-owned chain becomes a device row for its owner.
  await q(`insert into billing_private.purchase_devices(purchase_id,principal)
    select id,principal from billing_private.store_purchases where principal=$1`, [acct(owner.id)]);
  // Post-017 production shape for the A4 unification: the trigger and the default
  // are already 30, and every principal row is 30.
  await q(`insert into ai_private.principals(id,user_id,support_code,free_limit) values
    ($1,$2,'TF-RT-OWNER',30),($3,$4,'TF-RT-OTHER',30)`,
    [acct(owner.id), owner.id, acct(other.id), other.id]);
  await q(`update ai_private.runtime set legacy_import_complete=true`);
  assert.equal((await one('select count(*)::int n from billing_private.purchase_devices')).n, 1);
  assert.equal((await one('select count(*)::int n from billing_private.store_purchases')).n, 3);
  console.log('ok: 3 chains (2 account-owned, 1 ownerless), 2 entitlements, 1 claim, 1 device row');

  group('guard 1: device principal owns a purchase chain (store_purchases.principal)');
  await q(`insert into billing_private.store_purchases
    (provider,environment,purchase_key_hash,store_reference_ciphertext,principal,product_id,store_status)
    values('apple','sandbox',$1,'cipher',$2,$3,'active')`, ['1'.repeat(64), GUEST, PRODUCT]);
  let msg = await rejection(DOWN, 'DEVICE_PRINCIPAL_ROLLBACK_REFUSED');
  assert.match(msg, /store_purchases are owned by a device principal/);
  assert.match(msg, /Fix forward/);
  assert.deepEqual((await columns('principal')).map(r => r.table_name),
    ['account_entitlements', 'billing_claims', 'purchase_devices', 'store_purchases'],
    'refusal must leave the schema untouched');
  assert.match(msg, /^DEVICE_PRINCIPAL_ROLLBACK_REFUSED/);
  console.log('ok: refused, schema unchanged. message:\n' + msg.split('\n').slice(0, 8).join('\n'));
  await q(`delete from billing_private.store_purchases where principal=$1`, [GUEST]);

  group('guard 2: non-account principal only in purchase_devices (account-owned chain)');
  await q(`insert into billing_private.purchase_devices(purchase_id,principal)
    select id,$1 from billing_private.store_purchases where principal=$2`, [GUEST, acct(owner.id)]);
  msg = await rejection(DOWN, 'DEVICE_PRINCIPAL_ROLLBACK_REFUSED');
  assert.match(msg, /purchase_devices use a device principal/);
  assert.deepEqual((await columns('principal')).map(r => r.table_name),
    ['account_entitlements', 'billing_claims', 'purchase_devices', 'store_purchases'],
    'refusal must leave the schema untouched');
  console.log('ok: refused, schema unchanged. first line: ' + msg.split('\n')[0]);
  assert.match(msg, /^DEVICE_PRINCIPAL_ROLLBACK_REFUSED/);
  await q(`delete from billing_private.purchase_devices where principal=$1`, [GUEST]);

  group('guard 3: device principal in billing_claims and account_entitlements');
  await q(`insert into billing_private.billing_claims
    (claim_id,principal,provider,product_id,expected_account_identifier_hash,request_hash)
    values($1,$2,'apple',$3,$4,$5)`, [randomUUID(), GUEST, PRODUCT, '2'.repeat(64), '3'.repeat(64)]);
  msg = await rejection(DOWN, 'DEVICE_PRINCIPAL_ROLLBACK_REFUSED');
  assert.match(msg, /billing_claims belong to a device principal/);
  await q(`delete from billing_private.billing_claims where principal=$1`, [GUEST]);
  await q(`insert into billing_private.account_entitlements(principal,plan,status) values($1,'plus','active')`, [GUEST]);
  msg = await rejection(DOWN, 'DEVICE_PRINCIPAL_ROLLBACK_REFUSED');
  assert.match(msg, /account_entitlements belong to a device principal/);
  await q(`delete from billing_private.account_entitlements where principal=$1`, [GUEST]);
  assert.deepEqual((await columns('principal')).map(r => r.table_name),
    ['account_entitlements', 'billing_claims', 'purchase_devices', 'store_purchases']);
  console.log('ok: both refused with a per-table reason, schema unchanged');

  group('guard 4: dangling account uuid (auth.users row deleted while 017 is applied)');
  const victim = await db.account();
  await q(`insert into billing_private.account_entitlements(principal,plan,status) values($1,'plus','active')`, [acct(victim.id)]);
  await q(`delete from auth.users where id=$1`, [victim.id]);
  msg = await rejection(DOWN, 'DEVICE_PRINCIPAL_ROLLBACK_REFUSED');
  assert.match(msg, /no longer exists/);
  assert.match(msg, /billing_claims_user_id_fkey/);
  console.log('ok: refused with the foreign-key reason. first lines:\n' + msg.split('\n').slice(0, 4).join('\n'));
  await q(`delete from billing_private.account_entitlements where principal=$1`, [acct(victim.id)]);

  group('apply the manual reverse migration (single transaction)');
  const applied = await applyDown('reverse');
  assert.equal(applied.ok, true, applied.message);
  console.log('ok: reverse migration committed');

  group('post-reverse shape assertions (information_schema / pg_catalog)');
  assert.deepEqual(await columns('user_id'), [
    {table_name: 'account_entitlements', data_type: 'uuid', is_nullable: 'NO'},
    {table_name: 'billing_claims', data_type: 'uuid', is_nullable: 'NO'},
    {table_name: 'store_purchases', data_type: 'uuid', is_nullable: 'YES'},
  ]);
  assert.deepEqual(await columns('principal'), [], 'no principal column may survive anywhere in billing_private');
  assert.equal((await one(`select count(*)::int n from information_schema.tables
    where table_schema='billing_private' and table_name='purchase_devices'`)).n, 0);
  assert.equal((await one(`select count(*)::int n from pg_indexes
    where schemaname='billing_private' and indexname='purchase_devices_principal'`)).n, 0);
  const fks = await rows(`select conname, confdeltype, contype from pg_constraint
    where conname in ('store_purchases_user_id_fkey','billing_claims_user_id_fkey',
      'account_entitlements_user_id_fkey') order by conname`);
  assert.deepEqual(fks, [
    {conname: 'account_entitlements_user_id_fkey', confdeltype: 'c', contype: 'f'},
    {conname: 'billing_claims_user_id_fkey', confdeltype: 'c', contype: 'f'},
    {conname: 'store_purchases_user_id_fkey', confdeltype: 'n', contype: 'f'},
  ]);
  const idx = await rows(`select indexname from pg_indexes
    where schemaname='billing_private' and indexname in ('billing_claims_user','billing_claims_principal')`);
  assert.deepEqual(idx.map(r => r.indexname), ['billing_claims_user']);
  assert.match((await one(`select indexdef from pg_indexes
    where schemaname='billing_private' and indexname='billing_claims_user'`)).indexdef, /\(user_id, created_at\)/);
  assert.equal(await signatureExists('billing_private', 'ensure_account', 'target_user uuid'), true);
  assert.equal(await signatureExists('billing_private', 'ensure_account', 'target_principal text'), false);
  assert.equal(await signatureExists('billing_private', 'aggregate_entitlement', 'target_user uuid'), true);
  assert.equal(await signatureExists('billing_private', 'aggregate_entitlement', 'target_principal text'), false);
  assert.equal(await signatureExists('billing_private', 'plus_source', 'p_actor text'), false);
  assert.equal(await signatureExists('ai_private', 'quota_status', 'actor text, dev_allowed boolean, member_limit integer'), true);
  assert.equal(await signatureExists('ai_private', 'quota_status', 'actor text, dev_allowed boolean'), true);
  assert.equal(await signatureExists('public', 'ai_quota_service', 'p_action text, p_data jsonb'), true);
  assert.equal(await signatureExists('public', 'billing_service', 'p_action text, p_data jsonb'), true);
  assert.equal((await one(`select count(*)::int n from pg_trigger where tgname='enforce_free_limit_30'`)).n, 1);
  assert.equal((await one(`select column_default from information_schema.columns
    where table_schema='ai_private' and table_name='principals' and column_name='free_limit'`)).column_default, '30');
  assert.deepEqual((await rows('select distinct free_limit from ai_private.principals')).map(r => r.free_limit), [30]);
  // The 30 unification must still be enforced after the reverse, not just present.
  await q(`update ai_private.principals set free_limit=50 where id=$1`, [acct(owner.id)]);
  assert.deepEqual((await rows('select distinct free_limit from ai_private.principals')).map(r => r.free_limit), [30],
    'enforce_free_limit_30 must still clamp a legacy 50 write after the reverse');
  assert.equal((await one(`select (schema->'required') ? 'puzzle' as puzzle_required
    from sync_private.contracts where name='cloud-state-v2'`)).puzzle_required, true);
  assert.equal((await one(`select (schema->>'$id') as id from sync_private.contracts
    where name='operation-v2'`)).id, 'urn:daymosaic:operation:2');
  console.log('ok: columns/types/nullability, FKs + on delete, index name, function signatures,');
  console.log('    30-unification and puzzle-required contract are all pre-017 again');

  group('data survived the round trip');
  const chainOwners = (await rows(`select user_id from billing_private.store_purchases`)).map(r => r.user_id);
  assert.equal(chainOwners.filter(x => x === owner.id).length, 1);
  assert.equal(chainOwners.filter(x => x === other.id).length, 1);
  assert.equal(chainOwners.filter(x => x === null).length, 1);
  assert.equal((await one(`select user_id from billing_private.billing_claims`)).user_id, owner.id);
  assert.equal((await one(`select user_id from billing_private.account_entitlements
    where plan='plus'`)).user_id, owner.id);
  assert.equal((await one(`select purchase_account_token from billing_private.account_entitlements
    where user_id=$1`, [owner.id])).purchase_account_token, token);
  console.log('ok: chains, claim, entitlement and its appAccountToken map back to the same uuids');

  group('restored FK behaviour is real (delete an account)');
  await q(`delete from auth.users where id=$1`, [other.id]);
  const afterDelete = await rows(`select user_id from billing_private.store_purchases where environment='sandbox' and user_id is null`);
  assert.equal(afterDelete.length, 2, 'the deleted account chain is set null');
  assert.equal((await one(`select count(*)::int n from billing_private.account_entitlements where user_id=$1`, [other.id])).n, 0,
    'the deleted account entitlement cascaded');
  console.log('ok: store_purchases is set null and account_entitlements cascaded, as pre-017');

  group('round trip is usable: restored RPCs execute against the restored schema');
  const tokenRow = await one(`select purchase_account_token from billing_private.account_entitlements where user_id=$1`, [owner.id]);
  const byToken = await one(`select public.billing_service('account_by_token',$1::jsonb) as v`,
    [JSON.stringify({appAccountToken: tokenRow.purchase_account_token})]);
  assert.deepEqual(byToken.v, {userID: owner.id}, '016 billing_service returns userID, not principal');
  const entitlement = await one(`select public.billing_service('entitlement',$1::jsonb) as v`,
    [JSON.stringify({principal: 'account:' + owner.id, sessionID: ownerDevice.session})]);
  assert.equal(entitlement.v.plan, 'plus');
  assert.equal(entitlement.v.status, 'active');
  assert.equal(entitlement.v.entitlementRevision, 7);
  assert.equal(entitlement.v.aiQuota.limit, 30);
  assert.deepEqual(entitlement.v.billingSources.map(s => s.productId), [PRODUCT]);
  const guestQuota = await one(`select public.ai_quota_service('status',$1::jsonb) as v`,
    [JSON.stringify({principal: GUEST, supportCode: 'TF-RT-0001', freeLimit: 30, memberLimit: 30})]);
  assert.equal(guestQuota.v.limit, 30);
  assert.equal(guestQuota.v.period, 'free');
  assert.equal(guestQuota.v.used, 0);
  assert.equal(guestQuota.v.supportCode, 'TF-RT-0001');
  const accountRequired = await one(`select public.billing_service('entitlement',$1::jsonb) as v`,
    [JSON.stringify({principal: GUEST})]);
  assert.deepEqual(accountRequired.v, {code: 'ACCOUNT_REQUIRED'}, 'the device-principal path is gone again');
  console.log('ok: account_by_token -> ' + JSON.stringify(byToken.v));
  console.log('ok: entitlement -> plan=' + entitlement.v.plan + ' revision=' + entitlement.v.entitlementRevision
    + ' aiQuota.limit=' + entitlement.v.aiQuota.limit + ' sources=' + JSON.stringify(entitlement.v.billingSources.map(s => s.productId)));
  console.log('ok: ai_quota_service status for a guest principal -> ' + JSON.stringify(guestQuota.v));
  console.log('ok: billing_service entitlement for a guest principal -> ' + JSON.stringify(accountRequired.v));

  group('the file is fail-closed when run again on an already reversed database');
  const again = await applyDown('again');
  assert.equal(again.ok, false);
  assert.match(again.message, /DEVICE_PRINCIPAL_ROLLBACK_WRONG_SHAPE/);
  assert.deepEqual(await columns('principal'), []);
  console.log('ok: second run refused with DEVICE_PRINCIPAL_ROLLBACK_WRONG_SHAPE and changed nothing');

  console.log('\nALL ROUND-TRIP CHECKS PASSED');
} finally {
  await db.close();
}
