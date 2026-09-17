import test from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {database} from './database.mjs';

const MIGRATION=new URL('../migrations/202609170017_device_principal_billing.sql',import.meta.url);
const ROLLBACK=new URL('../migrations/202609170017_device_principal_billing_rollback.sql',import.meta.url);
const THROUGH='202609140016_guest_free_pool.sql';
const PRODUCT='com.hayden.daymosaic.plus.monthly';

const apply=async(db,file)=>db.admin.query(await readFile(file,'utf8'));
const scalar=async(db,sql,values=[])=>(await db.admin.query(sql,values)).rows[0];
const columns=async(db,name)=>(await db.admin.query(
  `select table_name,data_type from information_schema.columns
    where table_schema='billing_private' and column_name=$1 order by table_name`,[name])).rows;

test('the migration preserves account-owned entitlements, backfills device rows and the rollback reverses both',async()=>{
  const db=await database({through:THROUGH});
  try{
    const owner=await db.account();
    const other=await db.account();
    // Production has zero account-owned chains (A2), so this is not production
    // data; it exists to prove the backfill and the rollback are order-preserving
    // if the assumption ever stops holding.
    await db.admin.query(`insert into billing_private.store_purchases
      (provider,environment,purchase_key_hash,store_reference_ciphertext,user_id,product_id,store_status,
       expires_at,last_store_event_at)
      values('apple','production',$1,'cipher',$2,$3,'active','2026-12-01T00:00:00Z',now())`,
      ['a'.repeat(64),owner.id,PRODUCT]);
    // A chain left ownerless by an account deletion keeps its NULL principal.
    await db.admin.query(`insert into billing_private.store_purchases
      (provider,environment,purchase_key_hash,store_reference_ciphertext,user_id,product_id,store_status)
      values('apple','sandbox',$1,'cipher',null,$2,'expired')`,['b'.repeat(64),PRODUCT]);
    await db.admin.query(`insert into billing_private.account_entitlements
      (user_id,plan,status,valid_until,entitlement_revision,account_timezone)
      values($1,'plus','active','2026-12-01T00:00:00Z',7,'Asia/Shanghai')`,[owner.id]);
    await db.admin.query(`insert into billing_private.account_entitlements(user_id,plan,status)
      values($1,'free','expired')`,[other.id]);
    await db.admin.query(`insert into billing_private.billing_claims
      (claim_id,user_id,provider,product_id,expected_account_identifier_hash,request_hash)
      values($1,$2,'apple',$3,$4,$5)`,[randomUUID(),owner.id,PRODUCT,'e'.repeat(64),'r'.repeat(64)]);

    const chainsBefore=(await db.admin.query(
      `select id,user_id,product_id,store_status,expires_at,environment,last_store_event_at
        from billing_private.store_purchases order by id`)).rows;
    const entitlementsBefore=(await db.admin.query(
      `select user_id,plan,status,valid_until,entitlement_revision,account_timezone
        from billing_private.account_entitlements order by user_id`)).rows;

    await apply(db,MIGRATION);

    // The backfill must be a faithful rename, not a re-derivation: the migration
    // never re-aggregates, so every projection row is byte-for-byte unchanged.
    const entitlementsAfter=(await db.admin.query(
      `select principal,plan,status,valid_until,entitlement_revision,account_timezone
        from billing_private.account_entitlements order by principal`)).rows;
    assert.deepEqual(entitlementsAfter,entitlementsBefore.map(row=>({
      principal:'account:'+row.user_id,plan:row.plan,status:row.status,valid_until:row.valid_until,
      entitlement_revision:row.entitlement_revision,account_timezone:row.account_timezone})));
    const linked=(await db.admin.query(
      `select sp.purchase_key_hash,pd.principal,pd.revoked_at,pd.bound_at
        from billing_private.purchase_devices pd
        join billing_private.store_purchases sp on sp.id=pd.purchase_id
        order by sp.purchase_key_hash`)).rows;
    assert.equal(linked.length,1,'only the chain that still has an owner gains a device row');
    assert.equal(linked[0].principal,'account:'+owner.id);
    assert.equal(linked[0].revoked_at,null);
    assert.equal(linked[0].bound_at instanceof Date,true);
    assert.equal((await scalar(db,'select principal from billing_private.billing_claims')).principal,
      'account:'+owner.id,'the claim index and column rename keep every row');
    // 015 was already applied here, so the re-issued statements must be no-ops.
    assert.equal((await scalar(db,`select count(*)::int n from pg_trigger
      where tgname='enforce_free_limit_30'`)).n,1,'the 015 catch-up must not duplicate its trigger');
    assert.equal((await scalar(db,`select column_default from information_schema.columns
      where table_schema='ai_private' and table_name='principals' and column_name='free_limit'`)).column_default,'30');

    await apply(db,ROLLBACK);

    const chainsRestored=(await db.admin.query(
      `select id,user_id,product_id,store_status,expires_at,environment,last_store_event_at
        from billing_private.store_purchases order by id`)).rows;
    assert.deepEqual(chainsRestored,chainsBefore,'every chain maps back to the same user_id');
    assert.deepEqual(await columns(db,'user_id'),
      [{table_name:'account_entitlements',data_type:'uuid'},
       {table_name:'billing_claims',data_type:'uuid'},
       {table_name:'store_purchases',data_type:'uuid'}]);
    assert.deepEqual(await columns(db,'principal'),[]);
    assert.equal((await scalar(db,`select count(*)::int n from information_schema.tables
      where table_schema='billing_private' and table_name='purchase_devices'`)).n,0);
    assert.equal((await db.admin.query(
      `select conname from pg_constraint where conname in ('store_purchases_user_id_fkey',
        'billing_claims_user_id_fkey','account_entitlements_user_id_fkey')`)).rows.length,3);
  }finally{await db.close();}
});

test('the rollback refuses to run once device principals own billing rows',async()=>{
  const db=await database({through:THROUGH});
  try{
    await apply(db,MIGRATION);
    const principal='guest_'+'c'.repeat(24);
    const chain=(await db.admin.query(`insert into billing_private.store_purchases
      (provider,environment,purchase_key_hash,store_reference_ciphertext,principal,product_id,store_status)
      values('apple','sandbox',$1,'cipher',$2,$3,'active') returning id`,
      ['c'.repeat(64),principal,PRODUCT])).rows[0].id;
    await db.admin.query('insert into billing_private.purchase_devices(purchase_id,principal) values($1,$2)',
      [chain,principal]);
    await assert.rejects(apply(db,ROLLBACK),/DEVICE_PRINCIPAL_DATA_PRESENT/);
    await db.admin.query('delete from billing_private.purchase_devices where principal=$1',[principal]);
    await db.admin.query(`insert into billing_private.account_entitlements(principal,plan,status)
      values($1,'plus','active')`,[principal]);
    await assert.rejects(apply(db,ROLLBACK),/DEVICE_PRINCIPAL_DATA_PRESENT/);
    await db.admin.query('delete from billing_private.account_entitlements where principal=$1',[principal]);
    await db.admin.query(`insert into billing_private.billing_claims
      (claim_id,principal,provider,product_id,expected_account_identifier_hash,request_hash)
      values($1,$2,'apple',$3,$4,$5)`,[randomUUID(),principal,PRODUCT,'f'.repeat(64),'g'.repeat(64)]);
    await assert.rejects(apply(db,ROLLBACK),/DEVICE_PRINCIPAL_DATA_PRESENT/);
    // Every rejection above is atomic: the schema is still the migrated one.
    assert.deepEqual((await columns(db,'principal')).map(row=>row.table_name),
      ['account_entitlements','billing_claims','purchase_devices','store_purchases']);
    // With the device rows gone the rollback is allowed again.
    await db.admin.query('delete from billing_private.billing_claims where principal=$1',[principal]);
    await db.admin.query('delete from billing_private.store_purchases where principal=$1',[principal]);
    await apply(db,ROLLBACK);
    assert.equal((await scalar(db,`select count(*)::int n from information_schema.tables
      where table_schema='billing_private' and table_name='purchase_devices'`)).n,0);
  }finally{await db.close();}
});

test('the migration applies the free-quota-30 statements to a database that skipped them',async()=>{
  const db=await database({through:THROUGH});
  try{
    // Reproduce the verified live state (A4): the applied set ends at 016 but 015
    // was never applied, so free_limit is still 50 (plus one legacy row at 3) and
    // the enforcement trigger does not exist.
    await db.admin.query('drop trigger enforce_free_limit_30 on ai_private.principals');
    await db.admin.query('alter table ai_private.principals alter column free_limit set default 50');
    await db.admin.query(`insert into ai_private.principals(id,support_code,free_limit)
      values('guest_${'1'.repeat(24)}','TF-A4-50',50),('guest_${'2'.repeat(24)}','TF-A4-3',3)`);
    assert.deepEqual((await db.admin.query(
      'select distinct free_limit from ai_private.principals order by free_limit')).rows.map(row=>row.free_limit),
      [3,50]);
    await apply(db,MIGRATION);
    assert.deepEqual((await db.admin.query(
      'select distinct free_limit from ai_private.principals')).rows.map(row=>row.free_limit),[30],
      'every principal must be unified on 30');
    assert.equal((await scalar(db,`select column_default from information_schema.columns
      where table_schema='ai_private' and table_name='principals' and column_name='free_limit'`)).column_default,'30');
    // The re-issued trigger must clamp later legacy writes too, not only the rows
    // that existed while the migration ran.
    await db.admin.query('update ai_private.principals set free_limit=50 where id=$1',[`guest_${'1'.repeat(24)}`]);
    await db.admin.query(`insert into ai_private.principals(id,support_code,free_limit)
      values('guest_${'3'.repeat(24)}','TF-A4-LATE',50)`);
    assert.deepEqual((await db.admin.query(
      'select distinct free_limit from ai_private.principals')).rows.map(row=>row.free_limit),[30]);
  }finally{await db.close();}
});
