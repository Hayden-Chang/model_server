import test, {before, after} from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {database} from './database.mjs';

let db;
before(async () => { db=await database(); });
after(async () => { await db?.close(); });

const TABLES = ['store_purchases','billing_claims','billing_events','account_entitlements'];

async function account() {
  const user=await db.account(); const device=await user.device();
  return {principal:'account:'+user.id, sessionID:device.session, device, user};
}
const billingRpc=(action,data={})=>db.rpc(null,'billing_service',[action,data],'service_role');
const PRODUCT_MONTHLY='com.hayden.daymosaic.plus.monthly';

test('billing service rejects guests and unprivileged roles',async()=>{
  const guest='guest_'+'a'.repeat(24);
  assert.equal((await billingRpc('entitlement',{principal:guest,sessionID:randomUUID()})).code,'ACCOUNT_REQUIRED');
  const a=await account();
  for(const role of ['anon','authenticated']){
    await assert.rejects(db.rpc(a.device,'billing_service',['entitlement',{principal:a.principal}],role),/permission denied/);
  }
});

test('unavailable accounts cannot read billing state',async()=>{
  const noSession=await account();
  await db.admin.query('delete from auth.sessions where id=$1',[noSession.sessionID]);
  assert.equal((await billingRpc('entitlement',{principal:noSession.principal,sessionID:noSession.sessionID})).code,'ACCOUNT_UNAVAILABLE');
  const pending=await account();
  await db.admin.query('insert into sync_private.accounts(user_id,deletion_pending) values($1,true)',[pending.user.id]);
  assert.equal((await billingRpc('entitlement',{principal:pending.principal,sessionID:pending.sessionID})).code,'ACCOUNT_UNAVAILABLE');
});

test('claim registration is idempotent with a stable per-account token',async()=>{
  const a=await account(); const claimId=randomUUID();
  const first=await billingRpc('claim_register',{principal:a.principal,sessionID:a.sessionID,
    provider:'apple',productId:PRODUCT_MONTHLY,claimId});
  assert.equal(first.claimId,claimId);
  assert.equal(/^[0-9a-f-]{36}$/.test(first.appAccountToken),true);
  const second=await billingRpc('claim_register',{principal:a.principal,sessionID:a.sessionID,
    provider:'apple',productId:PRODUCT_MONTHLY,claimId});
  assert.equal(second.appAccountToken,first.appAccountToken);
  assert.equal(second.expiresAt,first.expiresAt);
  const sameClaimOtherProduct=await billingRpc('claim_register',{principal:a.principal,sessionID:a.sessionID,
    provider:'apple',productId:'com.hayden.daymosaic.plus.yearly',claimId});
  assert.equal(sameClaimOtherProduct.code,'CLAIM_CONFLICT');
  const other=await account();
  const otherAccountSameClaim=await billingRpc('claim_register',{principal:other.principal,sessionID:other.sessionID,
    provider:'apple',productId:PRODUCT_MONTHLY,claimId});
  assert.equal(otherAccountSameClaim.code,'CLAIM_CONFLICT');
  const status=await billingRpc('claim_get',{principal:a.principal,sessionID:a.sessionID,claimId});
  assert.equal(status.status,'pending');
  assert.equal((await billingRpc('claim_get',{principal:a.principal,sessionID:a.sessionID,
    claimId:randomUUID()})).code,'CLAIM_NOT_FOUND');
});

test('entitlement defaults to the free pool and mirrors the AI ledger',async()=>{
  const a=await account();
  const entitlement=await billingRpc('entitlement',{principal:a.principal,sessionID:a.sessionID});
  assert.equal(entitlement.plan,'free');
  assert.equal(entitlement.status,'expired');
  assert.equal(entitlement.entitlementRevision,0);
  assert.equal(entitlement.aiQuota.limit,50);
  assert.equal(entitlement.aiQuota.remaining,50);
  assert.deepEqual(entitlement.billingSources,[]);
  await db.admin.query(
    `insert into ai_private.principals(id,user_id,support_code,free_limit)
      values($1,$2,'TF-BILL-TEST',50) on conflict (id) do nothing`,
    [a.principal,a.user.id]);
  await db.admin.query(
    `insert into ai_private.buckets(principal,period,used) values($1,'free',21)
      on conflict (principal,period) do update set used=excluded.used`,[a.principal]);
  const after=await billingRpc('entitlement',{principal:a.principal,sessionID:a.sessionID});
  assert.equal(after.aiQuota.used,21);
  assert.equal(after.aiQuota.remaining,29);
});

test('apple verify binds the chain, aggregates plus and confirms the claim',async()=>{
  const a=await account(); const claimId=randomUUID();
  const claim=await billingRpc('claim_register',{principal:a.principal,sessionID:a.sessionID,
    provider:'apple',productId:PRODUCT_MONTHLY,claimId});
  const result=await billingRpc('apple_verify',{principal:a.principal,sessionID:a.sessionID,
    originalTransactionId:'900001',productId:PRODUCT_MONTHLY,appAccountToken:claim.appAccountToken,
    environment:'sandbox',storeStatus:'active',expiresAt:'2026-12-01T00:00:00.000Z',
    storeReferenceCiphertext:'cipher',claimId});
  assert.equal(result.plan,'plus');
  assert.equal(result.status,'active');
  assert.equal(result.entitlementRevision,1);
  assert.equal(result.validUntil,'2026-12-01T00:00:00.000Z');
  assert.equal(result.billingSources.length,1);
  assert.equal(result.billingSources[0].provider,'apple');
  assert.equal(result.billingSources[0].productId,PRODUCT_MONTHLY);
  const status=await billingRpc('claim_get',{principal:a.principal,sessionID:a.sessionID,claimId});
  assert.equal(status.status,'verified');
  const chain=(await db.admin.query(
    `select user_id,store_status from billing_private.store_purchases
      where purchase_key_hash=$1`,[status.purchaseKeyHash])).rows;
  assert.equal(chain.length,1);
  assert.equal(String(chain[0].user_id),a.user.id);
});

test('apple verify guards tokens, re-binding and updates expired chains',async()=>{
  const a=await account(); const b=await account();
  await billingRpc('entitlement',{principal:a.principal,sessionID:a.sessionID});
  await billingRpc('entitlement',{principal:b.principal,sessionID:b.sessionID});
  const tokenA=(await db.admin.query(
    `select purchase_account_token from billing_private.account_entitlements where user_id=$1`,[a.user.id])).rows[0].purchase_account_token;
  const tokenB=(await db.admin.query(
    `select purchase_account_token from billing_private.account_entitlements where user_id=$1`,[b.user.id])).rows[0].purchase_account_token;
  const bound=await billingRpc('apple_verify',{principal:a.principal,sessionID:a.sessionID,
    originalTransactionId:'900002',productId:PRODUCT_MONTHLY,appAccountToken:tokenA,
    environment:'sandbox',storeStatus:'active',expiresAt:'2026-12-01T00:00:00.000Z',
    storeReferenceCiphertext:'cipher',claimId:null});
  assert.equal(bound.plan,'plus');
  assert.equal(bound.status,'active');
  const mismatch=await billingRpc('apple_verify',{principal:b.principal,sessionID:b.sessionID,
    originalTransactionId:'900002',productId:PRODUCT_MONTHLY,appAccountToken:tokenA,
    environment:'sandbox',storeStatus:'active',expiresAt:'2026-12-01T00:00:00.000Z',
    storeReferenceCiphertext:'cipher',claimId:null});
  assert.equal(mismatch.code,'ACCOUNT_MISMATCH');
  const rebind=await billingRpc('apple_verify',{principal:b.principal,sessionID:b.sessionID,
    originalTransactionId:'900002',productId:PRODUCT_MONTHLY,appAccountToken:tokenB,
    environment:'sandbox',storeStatus:'active',expiresAt:'2026-12-01T00:00:00.000Z',
    storeReferenceCiphertext:'cipher',claimId:null});
  assert.equal(rebind.code,'TRANSACTION_ALREADY_BOUND');
  const expired=await billingRpc('apple_verify',{principal:a.principal,sessionID:a.sessionID,
    originalTransactionId:'900002',productId:PRODUCT_MONTHLY,appAccountToken:tokenA,
    environment:'sandbox',storeStatus:'expired',expiresAt:'2026-09-01T00:00:00.000Z',
    storeReferenceCiphertext:'cipher',claimId:null});
  assert.equal(expired.plan,'free');
  assert.equal(expired.status,'expired');
  assert.equal(expired.entitlementRevision,bound.entitlementRevision+1);
});
test('billing tables deny every role including service_role',async()=>{
  const a=await db.account();
  for(const table of TABLES){
    for(const role of ['anon','authenticated','service_role']){
      await assert.rejects(db.call(a.device,`select * from billing_private.${table}`,[],role),/permission denied/);
      await assert.rejects(db.call(a.device,`insert into billing_private.${table} default values`,[],role),/permission denied/);
    }
  }
});

test('purchase chain is unique per provider/environment/hash and survives account deletion',async()=>{
  const a=await db.account();
  const key='h'.repeat(64);
  const insert=(environment,provider,product)=>db.admin.query(
    `insert into billing_private.store_purchases
       (provider,environment,purchase_key_hash,store_reference_ciphertext,user_id,product_id,store_status)
     values($1,$2,$3,'cipher',$4,$5,'active')`,[provider,environment,key,a.id,product]);
  await insert('sandbox','apple','com.hayden.daymosaic.plus.monthly');
  await assert.rejects(insert('sandbox','apple','com.hayden.daymosaic.plus.monthly'),/duplicate key/);
  await insert('production','apple','com.hayden.daymosaic.plus.yearly');
  await insert('sandbox','google','com.hayden.daymosaic.plus.yearly');
  await db.admin.query('delete from auth.users where id=$1',[a.id]);
  const rows=(await db.admin.query(
    `select user_id from billing_private.store_purchases
      where purchase_key_hash=$1 and environment='sandbox' and provider='apple'`,[key])).rows;
  assert.equal(rows.length,1);
  assert.equal(rows[0].user_id,null);
});

test('claim defaults to pending, status is constrained, entitlement defaults follow the contract',async()=>{
  const a=await db.account();
  const claim=randomUUID();
  await db.admin.query(
    `insert into billing_private.billing_claims
       (claim_id,user_id,provider,product_id,expected_account_identifier_hash,request_hash)
     values($1,$2,'apple','com.hayden.daymosaic.plus.monthly',$3,$4)`,
    [claim,a.id,'e'.repeat(64),'r'.repeat(64)]);
  const rows=(await db.admin.query(
    'select status from billing_private.billing_claims where claim_id=$1',[claim])).rows;
  assert.equal(rows[0].status,'pending');
  await assert.rejects(db.admin.query(
    `update billing_private.billing_claims set status='weird' where claim_id=$1`,[claim]),/check constraint/);
  await assert.rejects(db.admin.query(
    `insert into billing_private.store_purchases
       (provider,environment,purchase_key_hash,store_reference_ciphertext,product_id,store_status,ack_attempts)
     values('apple','production',$1,'cipher','com.hayden.daymosaic.plus.monthly','active',-1)`,
    ['a'.repeat(64)]),/check constraint/);
  const ent=(await db.admin.query(
    'insert into billing_private.account_entitlements(user_id) values($1) returning *',[a.id])).rows[0];
  assert.equal(ent.plan,'free');
  assert.equal(ent.status,'expired');
  assert.equal(String(ent.entitlement_revision),'0');
  await assert.rejects(db.admin.query(
    `insert into billing_private.account_entitlements(user_id,plan) values($1,'gold')`,[a.id]),/check constraint/);
});

test('billing events dedupe by provider/environment/event id',async()=>{
  const eventId=randomUUID();
  const insert=(hash)=>db.admin.query(
    `insert into billing_private.billing_events
       (provider,environment,event_id,payload_hash,replay_material_ciphertext)
     values('apple','production',$1,$2,'cipher')`,[eventId,hash]);
  await insert('p'.repeat(64));
  await assert.rejects(insert('q'.repeat(64)),/duplicate key/);
  const rows=(await db.admin.query(
    `select status,attempts from billing_private.billing_events where event_id=$1`,[eventId])).rows;
  assert.equal(rows[0].status,'received');
  assert.equal(rows[0].attempts,0);
});
