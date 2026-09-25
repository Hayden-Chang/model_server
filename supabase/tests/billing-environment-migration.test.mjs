import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
import {database} from './database.mjs';

const migration=await readFile(new URL('../migrations/202609250025_billing_environment_quota.sql',import.meta.url),'utf8');
const principal='guest_'+'a'.repeat(24);
const data={principal,supportCode:'TF-ENV-MIGRATION',freeLimit:30,memberLimit:30,billingEnvironment:'sandbox'};
async function legacyDatabase(environment='sandbox'){
  const db=await database({through:'202609240024_billing_environment_isolation.sql'});
  await db.admin.query(`insert into ai_private.principals(id,support_code,free_limit) values($1,$2,30)`,[principal,data.supportCode]);
  const claim=await db.rpc(null,'billing_service',['claim_register',{principal,provider:'apple',
    productId:'com.hayden.daymosaic.plus.monthly',claimId:randomUUID()}],'service_role');
  await db.rpc(null,'billing_service',['apple_verify',{principal,originalTransactionId:randomUUID(),
    productId:'com.hayden.daymosaic.plus.monthly',storeStatus:'active',expiresAt:'2099-12-01T00:00:00Z',
    environment,storeReferenceCiphertext:'cipher',appAccountToken:claim.appAccountToken}],'service_role');
  await db.admin.query('update ai_private.runtime set legacy_import_complete=true');
  return db;
}

test('environment quota migration preserves legacy usage and in-flight refunds and can rerun',async()=>{
  const db=await legacyDatabase();
  try{
    const rpc=(action,extra={})=>db.rpc(null,'ai_quota_service',[action,{...data,...extra}],'service_role');
    const requestID=randomUUID(),attempt=randomUUID();
    const reserved=await rpc('reserve',{requestID,attempt,bodyHash:'a'.repeat(64)});
    assert.match(reserved.period,/^member:\d{4}-\d{2}-\d{2}$/);
    const old=(await db.admin.query('select id,period,used from ai_private.buckets where principal=$1',[principal])).rows[0];
    await db.admin.query(migration);
    await db.admin.query(migration);
    const migrated=(await db.admin.query('select id,period,used from ai_private.buckets where principal=$1',[principal])).rows[0];
    assert.deepEqual(migrated,{...old,period:old.period.replace('member:','member:sandbox:')});
    assert.equal((await rpc('status')).used,1);
    assert.equal((await rpc('finish',{requestID,attempt:reserved.attempt,consume:false})).ok,true);
    assert.equal((await rpc('status')).used,0);
    assert.equal(await db.rpc(null,'billing_environment_schema',[],'service_role'),25);
    for(const role of ['anon','authenticated']){
      await assert.rejects(db.rpc(null,'billing_environment_schema',[],role),/permission denied/);
    }
  }finally{await db.close();}
});

test('environment quota migration refuses ambiguous production history and leaves counters untouched',async()=>{
  const db=await legacyDatabase('production');
  try{
    await db.admin.query("insert into ai_private.buckets(principal,period,used) values($1,'member:2026-09-25',7)",[principal]);
    await assert.rejects(db.admin.query(migration),/BILLING_LEGACY_QUOTA_ENVIRONMENT_AMBIGUOUS/);
    await db.admin.query('rollback');
    assert.deepEqual((await db.admin.query('select period,used from ai_private.buckets where principal=$1',[principal])).rows,
      [{period:'member:2026-09-25',used:7}]);
  }finally{await db.close();}
});

test('environment quota migration refuses a destination collision without losing usage',async()=>{
  const db=await legacyDatabase();
  try{
    await db.admin.query(`insert into ai_private.buckets(principal,period,used) values
      ($1,'member:2026-09-25',7),($1,'member:sandbox:2026-09-25',2)`,[principal]);
    await assert.rejects(db.admin.query(migration),/BILLING_LEGACY_QUOTA_DESTINATION_EXISTS/);
    await db.admin.query('rollback');
    assert.equal((await db.admin.query('select sum(used)::int total from ai_private.buckets where principal=$1',[principal])).rows[0].total,9);
  }finally{await db.close();}
});
