import test, {before, after} from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID, createHash} from 'node:crypto';
import {database} from './database.mjs';

let db;
before(async () => { db=await database(); });
after(async () => { await db?.close(); });

const TABLES = ['store_purchases','billing_claims','billing_events','account_entitlements','purchase_devices'];

const PRODUCT_MONTHLY='com.hayden.daymosaic.plus.monthly';
const PRODUCT_YEARLY='com.hayden.daymosaic.plus.yearly';

// Device principals are the existing guest_* shape (product decision D1). In
// production POST /api/auth/guest derives the principal from the device_id and
// creates the ai_private.principals row as a side effect; these tests call the
// RPC directly, so they create that row themselves and pass the principal.
async function device() {
  const value=createHash('sha256').update(randomUUID()).digest('hex');
  const principal='guest_'+value.slice(0,24);
  await db.admin.query(
    `insert into ai_private.principals(id,support_code,free_limit)
      values($1,$2,30) on conflict (id) do nothing`,[principal,'TF-'+value.slice(24,32)]);
  return {principal};
}
const billingRpc=(action,data={})=>db.rpc(null,'billing_service',[action,data],'service_role');
const claimToken=async(d)=>{
  const claim=await billingRpc('claim_register',{principal:d.principal,provider:'apple',
    productId:PRODUCT_MONTHLY,claimId:randomUUID()});
  return claim.appAccountToken;
};
function verify(principal,{transaction,token,productId=PRODUCT_MONTHLY,status='active',
  expiresAt='2026-12-01T00:00:00.000Z',environment='sandbox',claimId=null,bindDevice}={}){
  return billingRpc('apple_verify',{principal,originalTransactionId:transaction,productId,
    storeStatus:status,expiresAt,environment,storeReferenceCiphertext:'cipher',
    appAccountToken:token,claimId,...(bindDevice===undefined?{}:{bindDevice})});
}
// Join `count` devices to one chain. The first device creates the chain and
// mints the appAccountToken; the others present the receipt carrying that same
// token, which is what a restored purchase on a second device looks like.
async function join(transaction,count){
  const members=[];
  for(let i=0;i<count;i++){
    const d=await device();
    const token=members.length?members[0].token:await claimToken(d);
    const result=await verify(d.principal,{transaction,token});
    members.push({...d,token,result});
  }
  return members;
}
const keyHash=t=>createHash('sha256').update('apple|'+t).digest('hex');
const chainOf=async t=>(await db.admin.query(
  `select id,principal,product_id,store_status,expires_at from billing_private.store_purchases
    where purchase_key_hash=$1`,[keyHash(t)])).rows;
const deviceRows=async id=>(await db.admin.query(
  `select principal,revoked_at from billing_private.purchase_devices
    where purchase_id=$1 order by bound_at,principal`,[id])).rows;
const activeDevices=async id=>(await db.admin.query(
  `select count(*)::int n from billing_private.purchase_devices
    where purchase_id=$1 and revoked_at is null`,[id])).rows[0].n;
const entitlementOf=async principal=>(await db.admin.query(
  `select plan,status,valid_until,entitlement_revision from billing_private.account_entitlements
    where principal=$1`,[principal])).rows[0];
// The offset a member `resetsAt` label must carry, taken from the runtime's IANA
// zone data rather than a constant, so an entitlement kept in a DST zone is
// checked against the offset in force at that instant.
const zoneOffset=(zone,instant)=>new Intl.DateTimeFormat('en-US',
  {timeZone:zone,timeZoneName:'longOffset'}).formatToParts(instant)
  .find(part=>part.type==='timeZoneName').value.replace('GMT','')||'+00:00';
const zoneWall=(zone,instant)=>new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',
  month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})
  .format(instant).replace(', ','T');
// Charge `count` calls through the same service-role RPC the AI route charges with.
async function charge(principal,count,billingEnvironment='sandbox'){
  const supportCode=(await db.admin.query('select support_code from ai_private.principals where id=$1',
    [principal])).rows[0].support_code;
  for(let i=0;i<count;i++){
    const requestID=randomUUID();
    const reserved=await db.rpc(null,'ai_quota_service',['reserve',{principal,supportCode,freeLimit:30,
      billingEnvironment,
      memberLimit:30,requestID,bodyHash:'e'.repeat(64),attempt:randomUUID()}],'service_role');
    assert.equal(reserved.period.startsWith('member:'),true,JSON.stringify(reserved));
    assert.deepEqual(await db.rpc(null,'ai_quota_service',['finish',{principal,supportCode,freeLimit:30,
      billingEnvironment,
      memberLimit:30,requestID,attempt:reserved.attempt,consume:true}],'service_role'),{ok:true});
  }
}

test('billing service requires a device principal and denies unprivileged roles',async()=>{
  const d=await device();
  assert.equal((await billingRpc('entitlement',{principal:d.principal})).plan,'free');
  // A Time Fragment account session is not a billing principal any more.
  const account='account:'+randomUUID();
  for(const action of ['entitlement','claim_register','claim_get','apple_verify']){
    assert.equal((await billingRpc(action,{principal:account,claimId:randomUUID()})).code,'DEVICE_REQUIRED',action);
  }
  // A well-formed but never-seen device principal is refused too: the principal
  // row is created by /api/auth/guest, not by this RPC.
  assert.equal((await billingRpc('entitlement',{principal:'guest_'+'0'.repeat(24)})).code,'DEVICE_REQUIRED');
  const user=await db.account(); const session=await user.device();
  for(const role of ['anon','authenticated']){
    await assert.rejects(db.rpc(session,'billing_service',['entitlement',{principal:d.principal}],role),/permission denied/);
  }
});

test('production entitlement does not grant a sandbox purchase on the same device',async()=>{
  const d=await device();
  const token=await claimToken(d);
  const sandbox=await verify(d.principal,{transaction:'sandbox-only-'+randomUUID(),token});
  assert.equal(sandbox.plan,'plus');

  const supportCode=(await db.admin.query(
    'select support_code from ai_private.principals where id=$1',[d.principal])).rows[0].support_code;
  await db.admin.query('update ai_private.runtime set legacy_import_complete=true');
  try {
    const productionQuota=await db.rpc(null,'ai_quota_service',['status',{
      principal:d.principal,supportCode,freeLimit:30,memberLimit:30,
      billingEnvironment:'production'}],'service_role');
    assert.equal(productionQuota.resetsAt,null);
  } finally {
    await db.admin.query('update ai_private.runtime set legacy_import_complete=false');
  }

  const production=await billingRpc('entitlement',{
    principal:d.principal,billingEnvironment:'production'});
  assert.equal(production.plan,'free');
  assert.equal(production.aiQuota.limit,30);
  assert.deepEqual(production.billingSources,[]);

  const productionTransaction='production-'+randomUUID();
  const bought=await verify(d.principal,{
    transaction:productionTransaction,token,environment:'production'});
  assert.equal(bought.plan,'plus');
  assert.equal(bought.billingSources.length,1);
  assert.equal(bought.billingSources[0].productId,PRODUCT_MONTHLY);
  const reread=await billingRpc('entitlement',{
    principal:d.principal,billingEnvironment:'production'});
  assert.equal(reread.entitlementRevision,bought.entitlementRevision);
  const reverified=await verify(d.principal,{
    transaction:productionTransaction,token,environment:'production'});
  assert.equal(reverified.entitlementRevision,bought.entitlementRevision+1);
});

test('a production verification cannot relabel a sandbox purchase chain',async()=>{
  const d=await device();
  const token=await claimToken(d);
  const transaction='cross-environment-'+randomUUID();
  assert.equal((await verify(d.principal,{transaction,token})).plan,'plus');
  const production=await verify(d.principal,{transaction,token,environment:'production'});
  assert.equal(production.code,'ENVIRONMENT_MISMATCH');
  const chains=await db.admin.query(
    'select environment from billing_private.store_purchases where purchase_key_hash=$1',
    [keyHash(transaction)]);
  assert.deepEqual(chains.rows.map(row=>row.environment),['sandbox']);
});

test('a dual-environment device charges the production chain owner',async()=>{
  const sandboxOwner=await device();
  const productionOwner=await device();
  const member=await device();
  const sandboxToken=await claimToken(sandboxOwner);
  const productionToken=await claimToken(productionOwner);
  const sandboxTransaction='sandbox-meter-'+randomUUID();
  const productionTransaction='production-meter-'+randomUUID();
  await verify(sandboxOwner.principal,{transaction:sandboxTransaction,token:sandboxToken});
  await verify(productionOwner.principal,{transaction:productionTransaction,
    token:productionToken,environment:'production'});
  await verify(member.principal,{transaction:sandboxTransaction,token:sandboxToken});
  await verify(member.principal,{transaction:productionTransaction,
    token:productionToken,environment:'production'});

  await db.admin.query('update ai_private.runtime set legacy_import_complete=true');
  try {
    await charge(member.principal,1,'production');
  } finally {
    await db.admin.query('update ai_private.runtime set legacy_import_complete=false');
  }
  const rows=(await db.admin.query(`select principal,used from ai_private.buckets
    where period like 'member:%' and principal=any($1::text[])`,
    [[sandboxOwner.principal,productionOwner.principal,member.principal]])).rows;
  assert.deepEqual(rows.map(row=>[row.principal,row.used]),[[productionOwner.principal,1]],
    JSON.stringify({sandboxOwner:sandboxOwner.principal,
      productionOwner:productionOwner.principal,member:member.principal}));
});

test('one purchase owner has independent sandbox and production daily quotas',async()=>{
  const owner=await device();
  const peer=await device();
  const token=await claimToken(owner);
  for(const environment of ['sandbox','production']){
    const transaction=environment+'-same-owner-'+randomUUID();
    await verify(owner.principal,{transaction,token,environment});
    await verify(peer.principal,{transaction,token,environment});
  }
  const entitlement=(principal,billingEnvironment)=>billingRpc('entitlement',{principal,billingEnvironment});
  await db.admin.query('update ai_private.runtime set legacy_import_complete=true');
  try {
    await charge(owner.principal,1,'sandbox');
    assert.equal((await entitlement(owner.principal,'production')).aiQuota.used,0,
      'A sandbox call must not consume the same owner production allowance');
    assert.equal((await entitlement(peer.principal,'sandbox')).aiQuota.used,1,
      'Restored devices still share the purchase owner allowance within one environment');
    await charge(peer.principal,30,'production');
    assert.equal((await entitlement(owner.principal,'production')).aiQuota.remaining,0);
    assert.equal((await entitlement(owner.principal,'sandbox')).aiQuota.remaining,29);
    await charge(peer.principal,1,'sandbox');
    assert.equal((await entitlement(owner.principal,'sandbox')).aiQuota.used,2);
    const supportCode=(await db.admin.query('select support_code from ai_private.principals where id=$1',[owner.principal])).rows[0].support_code;
    const request={principal:owner.principal,supportCode,freeLimit:30,memberLimit:30,
      requestID:randomUUID(),attempt:randomUUID(),bodyHash:'e'.repeat(64)};
    const sandboxHold=await db.rpc(null,'ai_quota_service',['reserve',{...request,billingEnvironment:'sandbox'}],'service_role');
    assert.equal(sandboxHold.used,3);
    await db.rpc(null,'ai_quota_service',['finish',{...request,attempt:sandboxHold.attempt,
      billingEnvironment:'sandbox',consume:false}],'service_role');
    assert.equal((await entitlement(owner.principal,'sandbox')).aiQuota.used,2);
    assert.equal((await entitlement(owner.principal,'production')).aiQuota.used,30);
  } finally {
    await db.admin.query('update ai_private.runtime set legacy_import_complete=false');
  }
});

test('production worker reads only production events and purchase chains',async()=>{
  const seen={};
  for(const environment of ['sandbox','production']){
    const eventId=environment+'-'+randomUUID();
    await billingRpc('event_receive',{provider:'apple',environment,
      eventId,payloadHash:'a'.repeat(64),
      replayMaterialCiphertext:'signed-notification'});
    const d=await device();
    await verify(d.principal,{transaction:environment+'-'+randomUUID(),
      token:await claimToken(d),environment});
    seen[environment]={eventId,principal:d.principal};
  }
  const pending=await billingRpc('event_pending',{billingEnvironment:'production'});
  assert.ok(pending.events.some(event=>event.eventId===seen.production.eventId));
  assert.ok(!pending.events.some(event=>event.eventId===seen.sandbox.eventId));
  assert.ok(pending.events.every(event=>event.environment==='production'));
  const reconcile=await billingRpc('reconcile_list',{billingEnvironment:'production'});
  assert.ok(reconcile.chains.some(chain=>chain.principal===seen.production.principal));
  assert.ok(!reconcile.chains.some(chain=>chain.principal===seen.sandbox.principal));
  assert.ok(reconcile.chains.every(chain=>chain.environment==='production'));
});

test('apple verify returns ACCOUNT_TOKEN_UNKNOWN instead of raising on a bad token',async()=>{
  const d=await device();
  const attempt=data=>billingRpc('apple_verify',{principal:d.principal,originalTransactionId:'bad-token',
    productId:PRODUCT_MONTHLY,environment:'sandbox',storeStatus:'active',...data});
  // Workers must resolve a known purchase principal; malformed and unknown
  // nonempty tokens must not fall through to device binding.
  assert.equal((await attempt({bindDevice:false})).code,'ACCOUNT_TOKEN_UNKNOWN');
  assert.equal((await attempt({appAccountToken:'',bindDevice:false})).code,'ACCOUNT_TOKEN_UNKNOWN');
  assert.equal((await attempt({appAccountToken:'not-a-uuid'})).code,'ACCOUNT_TOKEN_UNKNOWN');
  assert.equal((await attempt({appAccountToken:randomUUID()})).code,'ACCOUNT_TOKEN_UNKNOWN');
  assert.equal((await billingRpc('account_by_token',{appAccountToken:'not-a-uuid'})).code,'ACCOUNT_TOKEN_UNKNOWN');
});

test('claimless Apple offer code binds an authenticated device and enforces the chain device limit',async()=>{
  const transaction='offer-'+randomUUID();
  assert.equal((await billingRpc('account_by_purchase',{
    originalTransactionId:transaction,environment:'sandbox'})).code,'ACCOUNT_TOKEN_UNKNOWN');
  const members=[];
  for(let i=0;i<4;i++){
    const d=await device();
    const result=await verify(d.principal,{transaction,token:i===1?undefined:'',
      expiresAt:'2099-12-01T00:00:00.000Z',bindDevice:true});
    members.push({...d,result});
  }
  for(const member of members.slice(0,3)){
    assert.equal(member.result.plan,'plus',JSON.stringify(member.result));
    assert.equal((await billingRpc('entitlement',{principal:member.principal})).plan,'plus');
  }
  assert.equal(members[3].result.code,'DEVICE_LIMIT_REACHED');
  const chain=await chainOf(transaction);
  assert.equal(chain.length,1);
  assert.equal(chain[0].principal,members[0].principal);
  assert.equal(await activeDevices(chain[0].id),3);
  const owner=await billingRpc('account_by_purchase',{
    originalTransactionId:transaction,environment:'sandbox'});
  assert.equal(owner.principal,members[0].principal);
  assert.equal(/^[0-9a-f-]{36}$/.test(owner.appAccountToken),true);
  assert.equal((await billingRpc('account_by_purchase',{
    originalTransactionId:transaction,environment:'production'})).code,'ACCOUNT_TOKEN_UNKNOWN');
  assert.equal((await billingRpc('entitlement',{principal:members[3].principal})).plan,'free');
});

test('claim registration is idempotent with a stable per-device token',async()=>{
  const a=await device(); const claimId=randomUUID();
  const first=await billingRpc('claim_register',{principal:a.principal,
    provider:'apple',productId:PRODUCT_MONTHLY,claimId});
  assert.equal(first.claimId,claimId);
  assert.equal(/^[0-9a-f-]{36}$/.test(first.appAccountToken),true);
  const second=await billingRpc('claim_register',{principal:a.principal,
    provider:'apple',productId:PRODUCT_MONTHLY,claimId});
  assert.equal(second.appAccountToken,first.appAccountToken);
  assert.equal(second.expiresAt,first.expiresAt);
  const sameClaimOtherProduct=await billingRpc('claim_register',{principal:a.principal,
    provider:'apple',productId:PRODUCT_YEARLY,claimId});
  assert.equal(sameClaimOtherProduct.code,'CLAIM_CONFLICT');
  const other=await device();
  const otherDeviceSameClaim=await billingRpc('claim_register',{principal:other.principal,
    provider:'apple',productId:PRODUCT_MONTHLY,claimId});
  assert.equal(otherDeviceSameClaim.code,'CLAIM_CONFLICT');
  const status=await billingRpc('claim_get',{principal:a.principal,claimId});
  assert.equal(status.status,'pending');
  assert.equal((await billingRpc('claim_get',{principal:a.principal,
    claimId:randomUUID()})).code,'CLAIM_NOT_FOUND');
  // A claim belongs to the device that registered it.
  const foreign=await billingRpc('claim_get',{principal:other.principal,claimId});
  assert.equal(foreign.code,'CLAIM_CONFLICT');
});

test('claim registration mints a claim id when the client omits one',async()=>{
  // First purchase and the retry after a cancel both omit claimId, so the RPC
  // must mint one instead of failing the NOT NULL primary key.
  for(const claimId of [null,undefined]){
    const a=await device();
    const claim=await billingRpc('claim_register',{principal:a.principal,
      provider:'apple',productId:PRODUCT_MONTHLY,claimId});
    assert.equal(/^[0-9a-f-]{36}$/.test(claim.claimId),true,JSON.stringify(claim));
    assert.equal(/^[0-9a-f-]{36}$/.test(claim.appAccountToken),true);
    const status=await billingRpc('claim_get',{principal:a.principal,claimId:claim.claimId});
    assert.equal(status.status,'pending');
    const result=await verify(a.principal,{transaction:'claim-'+claim.claimId,
      token:claim.appAccountToken,claimId:claim.claimId});
    assert.equal(result.plan,'plus');
    assert.equal((await billingRpc('claim_get',{principal:a.principal,claimId:claim.claimId})).status,'verified');
  }
});

test('entitlement defaults to the free pool and mirrors the AI ledger',async()=>{
  const a=await device();
  const entitlement=await billingRpc('entitlement',{principal:a.principal});
  assert.equal(entitlement.plan,'free');
  assert.equal(entitlement.status,'expired');
  assert.equal(entitlement.entitlementRevision,0);
  assert.equal(entitlement.aiQuota.limit,30);
  assert.equal(entitlement.aiQuota.remaining,30);
  assert.deepEqual(entitlement.billingSources,[]);
  await db.admin.query(`update ai_private.principals set free_limit=50 where id=$1`,[a.principal]);
  await db.admin.query(
    `insert into ai_private.buckets(principal,period,used) values($1,'free',21)
      on conflict (principal,period) do update set used=excluded.used`,[a.principal]);
  const after=await billingRpc('entitlement',{principal:a.principal});
  assert.equal(after.aiQuota.used,21);
  assert.equal(after.aiQuota.limit,30,'legacy writes must be clamped to the current free limit');
  assert.equal(after.aiQuota.remaining,9);
  assert.equal(after.aiQuota.resetsAt,null,'the lifetime free pool never resets');
});

test('plus entitlement and apple_verify report the member daily pool, not the free pool',async()=>{
  // This is the only case in this file that charges through the AI ledger, so it
  // closes the legacy-import gate itself instead of in the file-level hook.
  await db.rpc(null,'ai_quota_service',['finish_import',{}],'service_role');
  const member=(await join('quota-source-plus',1))[0];
  const supportCode=(await db.admin.query('select support_code from ai_private.principals where id=$1',
    [member.principal])).rows[0].support_code;
  // The verify response that bound the chain already reports the member period.
  assert.equal(member.result.plan,'plus');
  assert.equal(member.result.aiQuota.limit,30);
  assert.equal(member.result.aiQuota.used,0);
  assert.equal(member.result.aiQuota.remaining,30);
  assert.ok(member.result.aiQuota.resetsAt?.endsWith('+08:00'),JSON.stringify(member.result));
  // Two calls through the same service-role RPC the AI route charges with.
  for(let i=0;i<2;i++){
    const requestID=randomUUID();
    const reserved=await db.rpc(null,'ai_quota_service',['reserve',{principal:member.principal,
      supportCode,freeLimit:30,memberLimit:30,requestID,bodyHash:'f'.repeat(64),
      attempt:randomUUID()}],'service_role');
    assert.equal(reserved.period.startsWith('member:'),true,JSON.stringify(reserved));
    await db.rpc(null,'ai_quota_service',['finish',{principal:member.principal,supportCode,
      freeLimit:30,memberLimit:30,requestID,attempt:reserved.attempt,consume:true}],'service_role');
  }
  const entitlement=await billingRpc('entitlement',{principal:member.principal});
  assert.equal(entitlement.plan,'plus');
  assert.equal(entitlement.aiQuota.limit,30);
  assert.equal(entitlement.aiQuota.used,2);
  assert.equal(entitlement.aiQuota.remaining,28);
  assert.equal(entitlement.aiQuota.resetsAt,member.result.aiQuota.resetsAt);
  // The reported usage is the member bucket the AI path wrote, and the lifetime
  // free pool stays untouched: reading that pool here was the defect.
  const buckets=(await db.admin.query(
    `select period,used from ai_private.buckets
      where principal=$1 and period like 'member:%'`,[member.principal])).rows;
  assert.equal(buckets.length,1);
  assert.equal(buckets[0].used,2);
  const free=(await db.admin.query(
    `select f.used from ai_private.free_pools f
      join ai_private.principals p on p.free_pool_id=f.id where p.id=$1`,[member.principal])).rows[0].used;
  assert.equal(free,0);
  // apple_verify answers from the same ledger as entitlement.
  const reverified=await verify(member.principal,{transaction:'quota-source-plus',token:member.token});
  assert.equal(reverified.plan,'plus');
  assert.equal(reverified.aiQuota.used,2);
  assert.equal(reverified.aiQuota.remaining,28);
  assert.equal(reverified.aiQuota.resetsAt,entitlement.aiQuota.resetsAt);
});

test('both devices on one chain read the same shared daily counter',async()=>{
  // The member display must keep consuming the ledger the AI path charges
  // (202609170019's F2), and that ledger is now shared per purchase chain (E1).
  const m=await join('shared-display',2);
  const [a,b]=m;
  for(const member of m){
    const entitlement=await billingRpc('entitlement',{principal:member.principal});
    assert.equal(entitlement.plan,'plus');
    assert.equal(entitlement.aiQuota.limit,30);
    assert.equal(entitlement.aiQuota.used,0);
    assert.equal(entitlement.aiQuota.remaining,30);
    assert.ok(entitlement.aiQuota.resetsAt?.endsWith('+08:00'),JSON.stringify(entitlement));
  }
  await charge(a.principal,5);
  // The joining device reports the first device's consumption, and the same
  // reset instant, without any client change.
  const seen=await billingRpc('entitlement',{principal:b.principal});
  assert.equal(seen.plan,'plus');
  assert.equal(seen.aiQuota.used,5);
  assert.equal(seen.aiQuota.remaining,25);
  assert.equal(seen.aiQuota.resetsAt,(await billingRpc('entitlement',{principal:a.principal})).aiQuota.resetsAt);
  // apple_verify answers from the same shared ledger.
  const reverified=await verify(b.principal,{transaction:'shared-display',token:b.token});
  assert.equal(reverified.plan,'plus');
  assert.equal(reverified.aiQuota.used,5);
  assert.equal(reverified.aiQuota.remaining,25);
  // One meter row, at the chain owner, and the free pools stay untouched.
  const rows=(await db.admin.query(`select principal,used from ai_private.buckets
    where period like 'member:%' and principal=any($1::text[])`,[m.map(x=>x.principal)])).rows;
  assert.deepEqual(rows.map(r=>[r.principal,r.used]),[[a.principal,5]],JSON.stringify(rows));
  const free=(await db.admin.query(`select f.used from ai_private.free_pools f
    join ai_private.principals p on p.free_pool_id=f.id where p.id=any($1::text[])`,
    [m.map(x=>x.principal)])).rows;
  assert.deepEqual(free.map(r=>r.used),[0,0]);
});

// Regression: the member `resetsAt` label used to append a literal '+08:00' to
// the entitlement timezone's wall clock, so any entitlement whose
// account_timezone was not Asia/Shanghai advertised the wrong offset. Nothing
// in this repository ever wrote a different zone, which is why the defect was
// invisible: it showed up only once a row carried one. The default is kept in
// the case above, so this one moves the row instead of adding a second surface.
test('the member resetsAt offset labels the entitlement timezone, not a fixed +08:00',async()=>{
  const zones=['Asia/Kolkata','America/New_York'];
  for(const zone of zones){
    const [d]=await join('resets-offset-'+zone,1);
    // The zone lives on the entitlement row, which is what quota_status reads.
    await db.admin.query(`update billing_private.account_entitlements set account_timezone=$2
      where principal=$1`,[d.principal,zone]);
    const entitlement=await billingRpc('entitlement',{principal:d.principal});
    const status=entitlement.aiQuota;
    assert.equal(entitlement.plan,'plus',JSON.stringify(entitlement));
    assert.equal(status.limit,30);
    assert.equal(status.remaining,30);
    assert.equal(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$/.test(status.resetsAt),true,
      JSON.stringify(status));
    const resets=new Date(status.resetsAt);
    // The label is an ISO 8601 instant: the offsets below are real, so parsing it
    // must land on the true reset instant and not on a shifted one.
    assert.equal(status.resetsAt.endsWith(zoneOffset(zone,resets)),true,
      zone+': '+status.resetsAt+' must carry its own offset');
    assert.equal(status.resetsAt.slice(0,19),zoneWall(zone,resets),
      zone+': the wall clock must be local midnight in that zone');
    assert.equal(status.resetsAt.slice(11),'00:00:00'+zoneOffset(zone,resets),
      zone+': a daily member reset is local midnight');
    // The instant is the point of the fix: correcting the label must not move it.
    // A daily meter resets at the next local midnight, so the instant must be
    // within the coming day and exactly on that boundary.
    const untilReset=resets.getTime()-Date.now();
    assert.equal(untilReset>0&&untilReset<=86400000,true,
      zone+': the reset must be the next local midnight, not a shifted instant: '+status.resetsAt);
    // apple_verify answers from the same function, so it carries the same label.
    const reverified=await verify(d.principal,{transaction:'resets-offset-'+zone,token:d.token});
    assert.equal(reverified.plan,'plus',JSON.stringify(reverified));
    assert.equal(reverified.aiQuota.resetsAt,status.resetsAt);
    // The label change must not touch the meter the AI path charges.
    assert.equal((await db.admin.query(`select used from ai_private.buckets
      where principal=$1 and period=$2`,[d.principal,status.period])).rows.length,0);
  }
  // A free principal still reports no reset at all.
  const free=await device();
  assert.equal((await billingRpc('entitlement',{principal:free.principal})).aiQuota.resetsAt,null);
});

test('device cap case 1: the first device creates the chain and gets plus',async()=>{
  const d1=await device();
  const claim=await billingRpc('claim_register',{principal:d1.principal,
    provider:'apple',productId:PRODUCT_MONTHLY,claimId:randomUUID()});
  const result=await verify(d1.principal,{transaction:'cap-1',token:claim.appAccountToken,claimId:claim.claimId});
  assert.equal(result.plan,'plus');
  assert.equal(result.status,'active');
  assert.equal(result.validUntil,'2026-12-01T00:00:00.000Z');
  assert.equal(result.billingSources.length,1);
  const chain=(await chainOf('cap-1'))[0];
  assert.equal(chain.principal,d1.principal,'the creating device owns the chain');
  assert.equal((await deviceRows(chain.id)).length,1);
  assert.equal((await deviceRows(chain.id))[0].revoked_at,null);
  assert.equal((await billingRpc('claim_get',{principal:d1.principal,claimId:claim.claimId})).status,'verified');
});

test('device cap case 2: the second and third devices join and project their own plus',async()=>{
  const m=await join('cap-2',3);
  const chain=(await chainOf('cap-2'))[0];
  for(const member of m) assert.equal(member.result.code,undefined,JSON.stringify(member.result));
  assert.equal(await activeDevices(chain.id),3);
  for(const member of m) assert.equal((await entitlementOf(member.principal)).plan,'plus');
  // The projection is per principal, not per chain.
  assert.equal(m[1].result.plan,'plus');
  assert.equal(m[2].result.plan,'plus');
  assert.equal(m[1].result.entitlementRevision,1);
});

test('device cap case 3: the fourth device is rejected and leaves no trace',async()=>{
  const m=await join('cap-3',3);
  const chain=(await chainOf('cap-3'))[0];
  const d4=await device();
  const result=await verify(d4.principal,{transaction:'cap-3',token:m[0].token});
  assert.equal(result.code,'DEVICE_LIMIT_REACHED');
  assert.equal(result.plan,undefined);
  assert.equal((await deviceRows(chain.id)).length,3);
  assert.equal((await deviceRows(chain.id)).some(row=>row.principal===d4.principal),false);
  const entitlement=await entitlementOf(d4.principal);
  assert.equal(entitlement.plan,'free','a rejected device must not receive a plus projection');
  assert.equal(entitlement.entitlement_revision,'0');
});

test('device cap case 4: a repeated submission from a bound device is idempotent',async()=>{
  const m=await join('cap-4',1);
  const chain=(await chainOf('cap-4'))[0];
  const again=await verify(m[0].principal,{transaction:'cap-4',token:m[0].token});
  assert.equal(again.plan,'plus');
  assert.equal(await activeDevices(chain.id),1);
  assert.equal((await deviceRows(chain.id)).length,1);
});

test('device cap case 5: a revoked device may re-join and regains plus',async()=>{
  const m=await join('cap-5',3);
  const chain=(await chainOf('cap-5'))[0];
  await db.admin.query(`update billing_private.purchase_devices set revoked_at=now()
    where purchase_id=$1 and principal=$2`,[chain.id,m[0].principal]);
  assert.equal(await activeDevices(chain.id),2);
  await db.admin.query('select billing_private.aggregate_entitlement($1)',[m[0].principal]);
  assert.equal((await entitlementOf(m[0].principal)).plan,'free','a revoked device loses plus');
  const revived=await verify(m[0].principal,{transaction:'cap-5',token:m[0].token});
  assert.equal(revived.plan,'plus','D3: a revoked device may re-join');
  assert.equal(await activeDevices(chain.id),3);
  const row=(await deviceRows(chain.id)).find(r=>r.principal===m[0].principal);
  assert.equal(row.revoked_at,null);
});

test('device cap case 6: a revoked device cannot revive into a full chain',async()=>{
  const m=await join('cap-6',3);
  const chain=(await chainOf('cap-6'))[0];
  await db.admin.query(`update billing_private.purchase_devices set revoked_at=now()
    where purchase_id=$1 and principal=$2`,[chain.id,m[0].principal]);
  assert.equal(await activeDevices(chain.id),2);
  const d4=await device();
  assert.equal((await verify(d4.principal,{transaction:'cap-6',token:m[0].token})).plan,'plus');
  assert.equal(await activeDevices(chain.id),3,'the new device takes the freed slot');
  // The quota check must trigger on a revive, not only on the first join: a naive
  // 'if not found then' lets this call clear revoked_at and reach 4 active
  // devices, growing by one on every later revoke/revive cycle.
  const revive=await verify(m[0].principal,{transaction:'cap-6',token:m[0].token});
  assert.equal(revive.code,'DEVICE_LIMIT_REACHED');
  assert.equal(await activeDevices(chain.id),3,'the cap must not be exceeded by a revive');
  const row=(await deviceRows(chain.id)).find(r=>r.principal===m[0].principal);
  assert.notEqual(row.revoked_at,null,'a refused revive must leave revoked_at set');
});

test('device cap case 8: a bindDevice false service call never clears a revocation',async()=>{
  const m=await join('cap-8',1);
  const chain=(await chainOf('cap-8'))[0];
  await db.admin.query(`update billing_private.purchase_devices set revoked_at=now()
    where purchase_id=$1 and principal=$2`,[chain.id,m[0].principal]);
  // billing_worker notifications and reconcile call apple_verify with
  // bindDevice=false: they must still update the chain and re-aggregate, but
  // never touch chain membership, or support bans would be undone silently.
  const result=await verify(m[0].principal,{transaction:'cap-8',token:m[0].token,
    status:'expired',expiresAt:'2026-09-01T00:00:00.000Z',bindDevice:false});
  assert.equal(result.plan,'free');
  assert.equal(result.status,'expired');
  assert.equal((await chainOf('cap-8'))[0].store_status,'expired','the chain is still updated');
  const row=(await deviceRows(chain.id)).find(r=>r.principal===m[0].principal);
  assert.notEqual(row.revoked_at,null,'a service call must not revive a support-revoked device');
  assert.equal(await activeDevices(chain.id),0);
  // The same call cannot silently re-add a non-member either.
  const stranger=await device();
  const added=await verify(stranger.principal,{transaction:'cap-8',token:m[0].token,bindDevice:false});
  assert.equal(added.plan,'free');
  assert.equal((await deviceRows(chain.id)).length,1);
});

test('device cap case 9: a later joiner never rewrites chain ownership',async()=>{
  const m=await join('cap-9',2);
  const chain=(await chainOf('cap-9'))[0];
  assert.equal(chain.principal,m[0].principal);
  const again=await verify(m[1].principal,{transaction:'cap-9',token:m[0].token});
  assert.equal(again.plan,'plus');
  assert.equal((await chainOf('cap-9'))[0].principal,m[0].principal,
    'reconcile resolves the chain through its original principal');
});

test('device cap case 10: a deleted account principal is rejected and not resurrected',async()=>{
  const user=await db.account();
  const principal='account:'+user.id;
  await db.admin.query(`insert into ai_private.principals(id,user_id,support_code,free_limit)
    values($1,$2,'TF-DELETED-01',30)`,[principal,user.id]);
  await db.admin.query(`insert into billing_private.account_entitlements(principal,plan,status,valid_until)
    values($1,'plus','active','2026-12-01T00:00:00Z')`,[principal]);
  await db.admin.query('delete from auth.users where id=$1',[user.id]);
  // Deleting the account cascades the account:<uuid> principal row away, which is
  // what makes the billing gate fail closed instead of resurrecting the row.
  assert.equal((await db.admin.query('select count(*)::int n from ai_private.principals where id=$1',[principal])).rows[0].n,0);
  const result=await verify(principal,{transaction:'cap-10',token:await claimToken(await device())});
  assert.equal(result.code,'DEVICE_REQUIRED');
  const entitlement=await entitlementOf(principal);
  assert.equal(entitlement.plan,'plus','the stored projection is left untouched, not recomputed');
  assert.equal(entitlement.entitlement_revision,'0','the rejected call must not run ensure_account or aggregation');
  assert.equal((await chainOf('cap-10')).length,0);
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
  const owner='account:'+a.id;
  const key='h'.repeat(64);
  const insert=(environment,provider,product)=>db.admin.query(
    `insert into billing_private.store_purchases
       (provider,environment,purchase_key_hash,store_reference_ciphertext,principal,product_id,store_status)
     values($1,$2,$3,'cipher',$4,$5,'active')`,[provider,environment,key,owner,product]);
  await insert('sandbox','apple',PRODUCT_MONTHLY);
  await assert.rejects(insert('sandbox','apple',PRODUCT_MONTHLY),/duplicate key/);
  await insert('production','apple',PRODUCT_YEARLY);
  await insert('sandbox','google',PRODUCT_YEARLY);
  await db.admin.query('delete from auth.users where id=$1',[a.id]);
  const rows=(await db.admin.query(
    `select principal from billing_private.store_purchases
      where purchase_key_hash=$1 and environment='sandbox' and provider='apple'`,[key])).rows;
  assert.equal(rows.length,1);
  // The chain no longer has an auth.users FK to null the owner out; the account's
  // principal row is gone instead, so the chain is unreachable and fails closed.
  assert.equal(rows[0].principal,owner);
});

test('claim defaults to pending, status is constrained, entitlement defaults follow the contract',async()=>{
  const a=await db.account();
  const owner='account:'+a.id;
  const claim=randomUUID();
  await db.admin.query(
    `insert into billing_private.billing_claims
       (claim_id,principal,provider,product_id,expected_account_identifier_hash,request_hash)
     values($1,$2,'apple',$3,$4,$5)`,
    [claim,owner,PRODUCT_MONTHLY,'e'.repeat(64),'r'.repeat(64)]);
  const rows=(await db.admin.query(
    'select status from billing_private.billing_claims where claim_id=$1',[claim])).rows;
  assert.equal(rows[0].status,'pending');
  await assert.rejects(db.admin.query(
    `update billing_private.billing_claims set status='weird' where claim_id=$1`,[claim]),/check constraint/);
  await assert.rejects(db.admin.query(
    `insert into billing_private.store_purchases
       (provider,environment,purchase_key_hash,store_reference_ciphertext,product_id,store_status,ack_attempts)
     values('apple','production',$1,'cipher',$2,'active',-1)`,
    ['a'.repeat(64),PRODUCT_MONTHLY]),/check constraint/);
  const ent=(await db.admin.query(
    'insert into billing_private.account_entitlements(principal) values($1) returning *',[owner])).rows[0];
  assert.equal(ent.plan,'free');
  assert.equal(ent.status,'expired');
  assert.equal(String(ent.entitlement_revision),'0');
  await assert.rejects(db.admin.query(
    `insert into billing_private.account_entitlements(principal,plan) values($1,'gold')`,[owner]),/check constraint/);
  // chain membership is capped in the RPC, not by a shape check on the column:
  // backfilled account chains legitimately carry account:<uuid> rows.
  await db.admin.query(
    `insert into billing_private.store_purchases
       (provider,environment,purchase_key_hash,store_reference_ciphertext,product_id,store_status)
     values('apple','production',$1,'cipher',$2,'active')`,['z'.repeat(64),PRODUCT_MONTHLY]);
  await assert.rejects(db.admin.query(
    `insert into billing_private.purchase_devices(purchase_id,principal)
     values((select id from billing_private.store_purchases where purchase_key_hash=$1),'short')`,
    ['z'.repeat(64)]),/check constraint/);
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

test('billing events dedupe, conflict on hash change and list for retry',async()=>{
  const eventId=randomUUID();
  const receive=(hash)=>billingRpc('event_receive',{principal:'guest_'+'1'.repeat(24),
    provider:'apple',environment:'sandbox',eventId,payloadHash:hash,
    replayMaterialCiphertext:'signed-payload'});
  const first=await receive('p'.repeat(64));
  assert.equal(first.received,true);
  const dup=await receive('p'.repeat(64));
  assert.equal(dup.received,false);
  assert.equal((await receive('q'.repeat(64))).code,'EVENT_CONFLICT');
  const mark=await billingRpc('event_mark',{principal:'guest_'+'1'.repeat(24),
    provider:'apple',environment:'sandbox',eventId,status:'failed',
    lastErrorCode:'APP_STORE_UNAVAILABLE'});
  assert.equal(mark.updated,1);
  const pending=await billingRpc('event_pending',{principal:'guest_'+'1'.repeat(24),maxAttempts:8,limit:20});
  const row=pending.events.find(e=>e.eventId===eventId);
  assert.equal(row.attempts,1);
  assert.equal(row.replayMaterialCiphertext,'signed-payload');
  await billingRpc('event_mark',{principal:'guest_'+'1'.repeat(24),
    provider:'apple',environment:'sandbox',eventId,status:'processed'});
  const drained=await billingRpc('event_pending',{principal:'guest_'+'1'.repeat(24),maxAttempts:8,limit:20});
  assert.equal(drained.events.find(e=>e.eventId===eventId),undefined);
});

test('billing event accepts and replays an Apple-sized signed payload',async()=>{
  const eventId=randomUUID();
  const signedPayload='j'.repeat(19039);
  const payloadHash=createHash('sha256').update(signedPayload).digest('hex');
  const data={provider:'apple',environment:'sandbox',eventId,payloadHash,
    replayMaterialCiphertext:signedPayload};
  assert.deepEqual(await billingRpc('event_receive',data),{received:true});
  assert.deepEqual(await billingRpc('event_receive',data),{received:false});
  const pending=await billingRpc('event_pending',{maxAttempts:8,limit:100});
  assert.equal(pending.events.find(event=>event.eventId===eventId)?.replayMaterialCiphertext,
    signedPayload);
});

test('reconcile list exposes bound active chains with their principal and token',async()=>{
  const m=await join('reconcile-1',2);
  const resolved=await billingRpc('account_by_token',{appAccountToken:m[0].token});
  assert.equal(resolved.principal,m[0].principal);
  assert.equal(resolved.userID,undefined,'the worker now receives a principal, not a user id');
  const chains=(await billingRpc('reconcile_list',{principal:'guest_'+'2'.repeat(24)})).chains;
  const row=chains.find(c=>c.principal===m[0].principal);
  assert.notEqual(row,undefined);
  assert.equal(row.purchaseAccountToken,m[0].token);
  assert.equal(row.userId,undefined);
  // The joining device is a member, not an owner, so it is not a chain to reconcile.
  assert.equal(chains.find(c=>c.principal===m[1].principal),undefined);
});

// The Apple account can change while the guest/device token stays the same.
const syncCurrent=(d,transaction,token,environment='sandbox')=>billingRpc('apple_sync',{
  principal:d.principal,billingEnvironment:environment,
  ...(transaction ? {originalTransactionId:transaction,productId:PRODUCT_MONTHLY,
    appAccountToken:token,storeStatus:'active',expiresAt:'2026-12-01T00:00:00.000Z',
    environment,storeReferenceCiphertext:'cipher',bindDevice:true}: {})
});

test('current StoreKit empty snapshot removes old device membership without changing another device',async()=>{
  const [a,b]=await join('switch-empty-'+randomUUID(),2);
  await syncCurrent(a,null);
  const result=await billingRpc('entitlement',{principal:a.principal});
  assert.equal(result.plan,'free');
  assert.deepEqual(result.billingSources,[]);
  assert.equal((await billingRpc('entitlement',{principal:b.principal})).plan,'plus');
  await db.rpc(null,'ai_quota_service',['finish_import',{}],'service_role');
  const quota=await db.rpc(null,'ai_quota_service',['status',{principal:a.principal,
    billingEnvironment:'sandbox',freeLimit:30,supportCode:(await db.admin.query('select support_code from ai_private.principals where id=$1',[a.principal])).rows[0].support_code}],'service_role');
  assert.equal(quota.period,'free');
});

test('current StoreKit selection survives delayed old transactions and can switch back',async()=>{
  const a=await device(), token=await claimToken(a);
  const old='old-'+randomUUID(), current='new-'+randomUUID();
  await verify(a.principal,{transaction:old,token});
  await syncCurrent(a,current,token);
  let result=await syncCurrent(a,null);
  assert.equal(result.plan,'free');
  await verify(a.principal,{transaction:old,token});
  assert.equal((await billingRpc('entitlement',{principal:a.principal})).plan,'free');
  result=await syncCurrent(a,old,token);
  assert.equal(result.plan,'plus');
  assert.equal(result.billingSources.length,1);
});

test('current StoreKit snapshot is environment scoped and failed verification preserves selection',async()=>{
  const a=await device(), token=await claimToken(a);
  const transaction='scope-'+randomUUID();
  await syncCurrent(a,transaction,token);
  assert.equal((await syncCurrent(a,null,null,'production')).plan,'free');
  assert.equal((await billingRpc('entitlement',{principal:a.principal,billingEnvironment:'sandbox'})).plan,'plus');
  assert.equal((await syncCurrent(a,'bad-'+randomUUID(),randomUUID())).code,'ACCOUNT_TOKEN_UNKNOWN');
  assert.equal((await billingRpc('entitlement',{principal:a.principal})).plan,'plus');
});

test('current StoreKit selection filters quota source and denies public snapshot writes',async()=>{
  const [a]=await join('prior-'+randomUUID(),1);
  const current='current-'+randomUUID();
  const [owner]=await join(current,1);
  await syncCurrent(a,current,owner.token);
  await db.admin.query(`select set_config('app.billing_environment','sandbox',false)`);
  const meter=(await db.admin.query('select * from billing_private.plus_source($1)',[a.principal])).rows[0];
  assert.equal(meter.scope,owner.principal);
  for(const role of ['anon','authenticated','service_role']) {
    await assert.rejects(db.call(null,'select * from billing_private.storekit_selections',[],role),/permission denied/);
  }
  assert.equal((await billingRpc('apple_sync',{principal:'account:'+randomUUID()})).code,'DEVICE_REQUIRED');
  assert.equal((await billingRpc('apple_sync',{principal:'guest_'+'f'.repeat(24)})).code,'UNAUTHORIZED');
});
