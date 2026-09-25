import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile,readdir} from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
import {database} from './database.mjs';

const directory=new URL('../migrations/',import.meta.url);
const names=(await readdir(directory)).filter(name=>name.endsWith('_offer_code_claimless_verification.sql'));
assert.equal(names.length,1);
const migration=await readFile(new URL(names[0],directory),'utf8');
const principal='guest_'+'b'.repeat(24);
const productId='com.hayden.daymosaic.plus.monthly';
const data={principal,productId,billingEnvironment:'sandbox',environment:'sandbox',
  storeStatus:'active',expiresAt:'2099-12-01T00:00:00Z',storeReferenceCiphertext:'cipher'};

async function deployedDatabase(){
  // Reproduce the actual upgrade order: 024/025/026 were deployed before offers.
  const db=await database({through:'202609230023'});
  for(const name of ['202609240024_billing_environment_isolation.sql',
    '202609250025_billing_environment_quota.sql','202609250026_storekit_current_purchase.sql']){
    await db.admin.query(await readFile(new URL(name,directory),'utf8'));
  }
  await db.admin.query("insert into ai_private.principals(id,support_code,free_limit) values($1,'TF-OFFER-MIGRATION',30)",[principal]);
  await db.admin.query('update ai_private.runtime set legacy_import_complete=true');
  return db;
}

test('offer migration preserves deployed current selection and quota while enabling claimless sync',async()=>{
  const db=await deployedDatabase();
  try{
    const rpc=(action,extra={})=>db.rpc(null,'billing_service',[action,{...data,...extra}],'service_role');
    const claim=await rpc('claim_register',{provider:'apple'});
    const originalTransactionId=randomUUID();
    assert.equal((await rpc('apple_sync',{originalTransactionId,appAccountToken:claim.appAccountToken})).plan,'plus');
    const quota={principal,supportCode:'TF-OFFER-MIGRATION',freeLimit:30,memberLimit:30,billingEnvironment:'sandbox'};
    const reserved=await db.rpc(null,'ai_quota_service',['reserve',{...quota,requestID:randomUUID(),attempt:randomUUID(),bodyHash:'a'.repeat(64)}],'service_role');
    assert.match(reserved.period,/^member:sandbox:/);
    const before=(await db.admin.query('select id,period,used from ai_private.buckets where principal=$1',[principal])).rows;
    await db.admin.query(migration);
    assert.equal((await rpc('apple_sync')).plan,'free','empty current selection must still revoke membership');
    assert.equal((await rpc('apple_verify',{originalTransactionId,appAccountToken:claim.appAccountToken})).plan,'free',
      'historical verification must not override the empty selection');
    assert.equal((await rpc('apple_sync',{originalTransactionId,appAccountToken:''})).plan,'plus');
    const production=await rpc('entitlement',{billingEnvironment:'production',environment:'production'});
    assert.equal(production.plan,'free');
    assert.deepEqual(production.billingSources,[]);
    assert.deepEqual((await db.admin.query('select id,period,used from ai_private.buckets where principal=$1',[principal])).rows,before);
    assert.equal((await rpc('account_by_purchase',{originalTransactionId})).principal,principal);
    assert.equal((await rpc('apple_verify',{originalTransactionId,environment:'production',billingEnvironment:'production'})).code,'ENVIRONMENT_MISMATCH');
    for(const role of ['anon','authenticated','service_role']){
      await assert.rejects(db.rpc(null,'billing_service_unscoped',['entitlement',data],role),/permission denied/);
    }
    await db.admin.query(migration);
    assert.equal((await rpc('apple_sync',{originalTransactionId,appAccountToken:''})).plan,'plus');
    assert.equal(await db.rpc(null,'billing_environment_schema',[],'service_role'),27);
  }finally{await db.close();}
});

test('offer migration keeps current snapshot and environment wrappers intact',async()=>{
  const db=await deployedDatabase();
  try{
    const definitions=()=>db.admin.query(`select proname,pg_get_functiondef(oid) as definition,proacl::text
      from pg_proc where oid in ('public.billing_service(text,jsonb)'::regprocedure,
        'public.billing_service_before_storekit(text,jsonb)'::regprocedure) order by proname`);
    const before=(await definitions()).rows;
    await db.admin.query(migration);
    assert.deepEqual((await definitions()).rows,before);
  }finally{await db.close();}
});


test('offer migration refuses a database without current StoreKit selection',async()=>{
  const db=await database({through:'202609250025_billing_environment_quota.sql'});
  try{
    await assert.rejects(db.admin.query(migration),/BILLING_STOREKIT_MIGRATION_026_REQUIRED/);
    await db.admin.query('rollback');
    assert.equal(await db.rpc(null,'billing_environment_schema',[],'service_role'),25);
  }finally{await db.close();}
});
