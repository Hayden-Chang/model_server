import test, {before, after} from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID, createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {database} from './database.mjs';

let db;
before(async () => { db=await database(); });
after(async () => { await db?.close(); });
const hash='a'.repeat(64);
function guest() {
  const value=createHash('sha256').update(randomUUID()).digest('hex');
  return {principal:'guest_'+value.slice(0,24),supportCode:'TF-'+value.slice(24,32),freeLimit:30};
}
async function account() {
  const user=await db.account(); const device=await user.device();
  return {...guest(),principal:'account:'+user.id,sessionID:device.session,device,user};
}
const rpc=(action,data={})=>db.rpc(null,'ai_quota_service',[action,data],'service_role');
const serviceRpc=name=>db.rpc(null,name,[],'service_role');
const reserve=(p, requestID=randomUUID(), extra={})=>rpc('reserve',{...p,requestID,bodyHash:hash,attempt:randomUUID(),...extra});
const finish=(p,requestID,attempt,consume=true)=>rpc('finish',{...p,requestID,attempt,consume});
async function use(p,count) {
  for(let i=0;i<count;i++){const id=randomUUID();const r=await reserve(p,id);await finish(p,id,r.attempt);}
}

// --- purchase-chain fixtures ------------------------------------------------
// The daily member allowance is shared by the devices bound to one purchase
// chain (product decision E1), so these cases need a real chain. The shape is
// the one billing.test.mjs uses: the first device mints the appAccountToken and
// creates the chain, the others present a receipt carrying that same token,
// which is what a restored purchase on a second device looks like.
const PRODUCT='com.hayden.daymosaic.plus.monthly';
const MEMBER={memberLimit:30};
async function device(){
  const d=guest();
  await db.admin.query(`insert into ai_private.principals(id,support_code,free_limit)
    values($1,$2,30) on conflict (id) do nothing`,[d.principal,d.supportCode]);
  return d;
}
const billingRpc=(action,data={})=>db.rpc(null,'billing_service',[action,data],'service_role');
async function chain(transaction,count){
  const members=[];
  for(let i=0;i<count;i++){
    const d=await device();
    const token=members.length?members[0].token:(await billingRpc('claim_register',{principal:d.principal,
      provider:'apple',productId:PRODUCT,claimId:randomUUID()})).appAccountToken;
    const result=await billingRpc('apple_verify',{principal:d.principal,originalTransactionId:transaction,
      productId:PRODUCT,storeStatus:'active',expiresAt:'2026-12-01T00:00:00.000Z',
      environment:'sandbox',storeReferenceCiphertext:'cipher',appAccountToken:token});
    assert.equal(result.plan,'plus',JSON.stringify(result));
    members.push({...d,token});
  }
  return members;
}
const memberRow=(principal,period)=>db.admin.query(
  `select used from ai_private.buckets where principal=$1 and period=$2`,[principal,period]);

test('migration gate rejects new quota; imports metadata exactly once',async()=>{
  const p=guest();
  await assert.rejects(rpc('status',p),/AI_IMPORT_REQUIRED/);
  const data={...p,limit:50,developmentEnabled:true,importHash:'fixed',
    buckets:[{period:'free',used:31}],completedRequests:['legacy-completed']};
  await rpc('import',data);await rpc('import',data);
  await assert.rejects(rpc('import',{...data,importHash:'changed'}),/AI_IMPORT_CHANGED/);
  await rpc('finish_import');
  const imported=await rpc('status',p);
  assert.equal(imported.used,31);
  assert.equal(imported.limit,30,'legacy imports must keep usage but adopt the current free limit');
  assert.equal(imported.remaining,0,'lowering the limit must not refill an existing account');
  assert.equal((await reserve(p)).code,'AI_QUOTA_EXHAUSTED');
  assert.equal((await reserve(p,'legacy-completed')).code,'AI_REQUEST_ALREADY_COMPLETED');
  await assert.rejects(rpc('import',data),/AI_IMPORT_ALREADY_CLOSED/);
});

test('clients cannot call quota RPC, helpers or read ledger tables',async()=>{
  const a=await account();
  for(const role of ['anon','authenticated']){
    await assert.rejects(db.rpc(a.device,'ai_quota_service',['status',a],role),/permission denied/);
    await assert.rejects(db.call(a.device,'select * from ai_private.principals',[],role),/permission denied/);
    await assert.rejects(db.call(a.device,"select ai_private.quota_status('x',false)",[],role),/permission denied/);
  }
  assert.deepEqual(await db.rpc(a.device,'ai_account_identity'),{userID:a.user.id,sessionID:a.sessionID});
  await assert.rejects(db.rpc({...a.device,session:randomUUID()},'ai_account_identity'),/ACCOUNT_UNAVAILABLE/);
  await db.admin.query('update auth.users set email_confirmed_at=null where id=$1',[a.user.id]);
  await assert.rejects(db.rpc(a.device,'ai_account_identity'),/ACCOUNT_UNAVAILABLE/);
  assert.equal((await reserve(a)).code,'ACCOUNT_UNAVAILABLE');
  await db.admin.query('update auth.users set email_confirmed_at=now() where id=$1',[a.user.id]);
  await db.admin.query('update auth.users set is_anonymous=true where id=$1',[a.user.id]);
  await assert.rejects(db.rpc(a.device,'ai_account_identity'),/ACCOUNT_UNAVAILABLE/);
});

test('different sessions share 30 account calls while another account stays independent',async()=>{
  const a=await account();const other=await account();const second=await a.user.device();
  await use(a,15);await use({...a,sessionID:second.session},15);
  assert.equal((await reserve(a)).code,'AI_QUOTA_EXHAUSTED');
  assert.equal((await rpc('status',a)).used,30);
  assert.equal((await rpc('status',other)).remaining,30);
});

test('concurrent reservations cannot overspend or invoke the same request twice',async()=>{
  const p=guest();
  const results=await Promise.all(Array.from({length:36},()=>reserve(p)));
  assert.equal(results.filter(x=>x.attempt).length,30);
  assert.equal(results.filter(x=>x.code==='AI_QUOTA_EXHAUSTED').length,6);
  const other=guest();const id=randomUUID();
  const duplicates=await Promise.all(Array.from({length:8},()=>reserve(other,id)));
  assert.equal(duplicates.filter(x=>x.attempt).length,1);
  assert.equal((await rpc('status',other)).used,1);
});

// Regression: concurrent first requests for a brand-new principal used to raise
// a support_code unique violation instead of creating exactly one principal row.
test('concurrent first-time principal creation never violates support-code uniqueness',async()=>{
  for(let round=0;round<12;round++){
    const p=guest();
    const results=await Promise.all(Array.from({length:6},()=>reserve(p)));
    assert.equal(results.every(x=>typeof x==='object'&&x!==null),true);
    assert.equal(results.filter(x=>x.attempt).length,6);
    assert.equal((await rpc('status',p)).used,6);
  }
});

test('same transport attempt is idempotent; changed body and completed replay are rejected',async()=>{
  const p=guest();const id=randomUUID(), attempt=randomUUID();
  assert.equal((await reserve(p,id,{attempt})).attempt,attempt);
  assert.equal((await reserve(p,id,{attempt})).attempt,attempt);
  assert.equal((await rpc('status',p)).used,1);
  assert.equal((await reserve(p,id,{bodyHash:'b'.repeat(64)})).code,'AI_REQUEST_ID_CONFLICT');
  await finish(p,id,attempt);await finish(p,id,attempt);
  assert.equal((await reserve(p,id)).code,'AI_REQUEST_ALREADY_COMPLETED');
});

test('refund and expiry decrement once; stale attempts cannot finish a replacement',async()=>{
  const p=guest();const id=randomUUID();const first=await reserve(p,id);
  await finish(p,id,first.attempt,false);await finish(p,id,first.attempt,false);
  assert.equal((await rpc('status',p)).used,0);
  const second=await reserve(p,id);
  await db.admin.query("update ai_private.requests set expires_at=now()-interval '1 second' where principal=$1",[p.principal]);
  const third=await reserve(p,id);
  assert.notEqual(second.attempt,third.attempt);
  assert.equal((await finish(p,id,second.attempt)).code,'AI_RESERVATION_EXPIRED');
  await finish(p,id,third.attempt);
  assert.equal((await rpc('status',p)).used,1);
});

test('active plus entitlement grants the member daily pool in the account timezone',async()=>{
  const a=await account();
  await db.admin.query(
    `insert into billing_private.account_entitlements(principal,plan,status,account_timezone)
      values($1,'plus','active','Asia/Shanghai')
      on conflict (principal) do update set plan='plus',status='active'`,[a.principal]);
  await db.admin.query(
    `insert into ai_private.principals(id,user_id,support_code,free_limit)
      values($1,$2,'TF-MEMBER-01',50) on conflict (id) do nothing`,[a.principal,a.user.id]);
  const status=await rpc('status',{...a,memberLimit:30});
  assert.equal(status.period.startsWith('member:'),true);
  assert.equal(status.limit,30);
  assert.equal(status.remaining,30);
  assert.ok(status.resetsAt.endsWith('+08:00'));
  await use({...a,memberLimit:30},2);
  const after=await rpc('status',{...a,memberLimit:30});
  assert.equal(after.used,2);
  assert.equal(after.remaining,28);
  const freePool=(await db.admin.query(
    `select used from ai_private.buckets where principal=$1 and period='free'`,[a.principal])).rows;
  assert.equal(freePool.length,0);
});

test('free principals keep the lifetime pool while a member gets a reset time',async()=>{
  const free=guest();
  const status=await rpc('status',free);
  assert.equal(status.period,'free');
  assert.equal(status.limit,30);
  assert.equal(status.used,0);
  assert.equal(status.remaining,30);
  assert.equal(status.resetsAt,null,'the lifetime free pool never resets');
  const [owner]=await chain('free-vs-member',1);
  const member=await rpc('status',{...owner,...MEMBER});
  assert.equal(member.period.startsWith('member:'),true,JSON.stringify(member));
  assert.ok(member.resetsAt.endsWith('+08:00'),JSON.stringify(member));
});

test('two devices on one purchase chain share one daily counter',async()=>{
  const [a,b]=await chain('shared-counter',2);
  // Device A spends 8 of the chain's 30.
  await use({...a,...MEMBER},8);
  // Device B reads the same counter: 8 used, 22 left, the same reset instant.
  const seenByB=await rpc('status',{...b,...MEMBER});
  assert.equal(seenByB.period.startsWith('member:'),true,JSON.stringify(seenByB));
  assert.equal(seenByB.limit,30);
  assert.equal(seenByB.used,8);
  assert.equal(seenByB.remaining,22);
  assert.equal((await rpc('status',{...a,...MEMBER})).resetsAt,seenByB.resetsAt);
  // Device B spends the remaining 22: 8 + 22 is the whole chain allowance.
  await use({...b,...MEMBER},22);
  // The 31st request from either device is refused, at the same reset time.
  const refusedA=await reserve({...a,...MEMBER});
  const refusedB=await reserve({...b,...MEMBER});
  assert.equal(refusedA.code,'AI_DAILY_QUOTA_EXHAUSTED',JSON.stringify(refusedA));
  assert.equal(refusedB.code,'AI_DAILY_QUOTA_EXHAUSTED',JSON.stringify(refusedB));
  assert.equal(refusedB.limit,30);
  assert.equal(refusedB.used,30);
  assert.equal(refusedB.remaining,0);
  assert.equal(refusedB.resetsAt,seenByB.resetsAt);
  // Both devices report the same exhausted counter.
  const afterA=await rpc('status',{...a,...MEMBER});
  const afterB=await rpc('status',{...b,...MEMBER});
  assert.equal(afterA.used,30);
  assert.equal(afterA.remaining,0);
  assert.equal(afterB.used,30);
  assert.equal(afterB.remaining,0);
  // One meter row, owned by the chain's first device. The joining device keeps
  // its own receipts but owns no member bucket.
  const rows=(await db.admin.query(`select principal,used from ai_private.buckets
    where period=$1 and principal in ($2,$3)`,[afterA.period,a.principal,b.principal])).rows;
  assert.deepEqual(rows.map(r=>[r.principal,r.used]),[[a.principal,30]],JSON.stringify(rows));
  // A device on a different chain is untouched.
  const [other]=await chain('shared-counter-other',1);
  const isolated=await rpc('status',{...other,...MEMBER});
  assert.equal(isolated.used,0);
  assert.equal(isolated.remaining,30);
});

// The load-bearing case. The locks reserve already took are actor-scoped, so
// without a lock on the shared meter row both devices read `remaining = 1` and
// both consume it.
test('two devices on one chain cannot both spend the last request',async()=>{
  for(let round=0;round<4;round++){
    const [a,b]=await chain('race-'+round,2);
    const period=(await rpc('status',{...a,...MEMBER})).period;
    await db.admin.query(`insert into ai_private.buckets(principal,period,used) values($1,$2,29)
      on conflict(principal,period) do update set used=excluded.used`,[a.principal,period]);
    const results=await Promise.all([reserve({...a,...MEMBER}),reserve({...b,...MEMBER})]);
    assert.equal(results.filter(x=>x.attempt).length,1,JSON.stringify(results));
    assert.equal(results.filter(x=>x.code==='AI_DAILY_QUOTA_EXHAUSTED').length,1,JSON.stringify(results));
    assert.equal((await memberRow(a.principal,period)).rows[0].used,30,
      'the shared meter must never exceed the limit');
  }
  // All three devices of one chain at once, still one request short.
  const [a,b,c]=await chain('race-burst',3);
  const period=(await rpc('status',{...a,...MEMBER})).period;
  await db.admin.query(`insert into ai_private.buckets(principal,period,used) values($1,$2,29)
    on conflict(principal,period) do update set used=excluded.used`,[a.principal,period]);
  const burst=await Promise.all([a,b,c].flatMap(d=>[0,1,2,3].map(()=>reserve({...d,...MEMBER}))));
  assert.equal(burst.filter(x=>x.attempt).length,1,JSON.stringify(burst));
  assert.equal(burst.filter(x=>x.code==='AI_DAILY_QUOTA_EXHAUSTED').length,11,JSON.stringify(burst));
  assert.equal((await memberRow(a.principal,period)).rows[0].used,30);
});

test('a revoked device leaves the group and keeps its own free pool',async()=>{
  const [a,b]=await chain('revoked-device',2);
  await use({...a,...MEMBER},4);
  const purchase=(await db.admin.query(
    `select id from billing_private.store_purchases where principal=$1`,[a.principal])).rows[0];
  await db.admin.query(`update billing_private.purchase_devices set revoked_at=now()
    where purchase_id=$1 and principal=$2`,[purchase.id,b.principal]);
  await db.admin.query('select billing_private.aggregate_entitlement($1)',[b.principal]);
  // The revoked device is no longer part of the group: it reads its own
  // lifetime free pool again, and its calls stay off the chain's meter.
  const revoked=await rpc('status',{...b,...MEMBER});
  assert.equal(revoked.period,'free',JSON.stringify(revoked));
  assert.equal(revoked.used,0);
  assert.equal(revoked.remaining,30);
  assert.equal(revoked.resetsAt,null);
  await use({...b,...MEMBER},2);
  assert.equal((await rpc('status',{...b,...MEMBER})).used,2);
  const shared=(await db.admin.query(`select used from ai_private.buckets
    where principal=$1 and period like 'member:%'`,[a.principal])).rows;
  assert.equal(shared.length,1);
  assert.equal(shared[0].used,4,'a revoked device must not touch the chain meter');
  assert.equal((await rpc('status',{...a,...MEMBER})).used,4);
  assert.equal((await rpc('status',{...a,...MEMBER})).remaining,26);
});

test('a refund from one device and a peer expiry both restore the shared counter',async()=>{
  const [a,b]=await chain('shared-refund',2);
  const id=randomUUID();
  const reserved=await reserve({...a,...MEMBER},id);
  assert.equal(reserved.remaining,29);
  assert.equal((await rpc('status',{...b,...MEMBER})).used,1);
  await finish({...a,...MEMBER},id,reserved.attempt,false);
  assert.equal((await rpc('status',{...b,...MEMBER})).used,0);
  assert.equal((await rpc('status',{...b,...MEMBER})).remaining,30);
  // A peer's abandoned hold counts against the shared meter, and only that
  // peer's own free-pool group would reclaim it, so any device on the chain
  // must reclaim it here or a vanished device locks the group out for the day.
  await reserve({...b,...MEMBER});
  await db.admin.query("update ai_private.requests set expires_at=now()-interval '1 second' where principal=$1",[b.principal]);
  assert.equal((await rpc('status',{...a,...MEMBER})).used,1,'an expired hold still counts until it is reclaimed');
  const mine=await reserve({...a,...MEMBER});
  assert.equal(mine.remaining,29,JSON.stringify(mine));
  assert.equal((await rpc('status',{...a,...MEMBER})).used,1,'the reclaim refunds the peer hold, then this call spends one');
  assert.equal((await rpc('status',{...b,...MEMBER})).used,1);
});

test('a support reset on one device clears the chain counter it shares',async()=>{
  const [a,b]=await chain('admin-reset-shared',2);
  await use({...a,...MEMBER},6);
  assert.equal((await rpc('admin_status',{supportCode:b.supportCode})).used,6);
  assert.equal((await rpc('admin_reset',{supportCode:b.supportCode})).used,0);
  const cleared=await rpc('status',{...a,...MEMBER});
  assert.equal(cleared.used,0);
  assert.equal(cleared.remaining,30);
  assert.equal((await memberRow(a.principal,cleared.period)).rows[0].used,0);
});

test('a chain whose owner principal row is gone falls back to per-device metering',async()=>{
  // ai_private.buckets.principal has a foreign key to ai_private.principals, so
  // a scope whose principal row is gone would fail every member's reserve rather
  // than merely losing the sharing. Reachable for a legacy account-owned chain
  // whose account was deleted; a guest principal survives account deletion.
  const [a,b]=await chain('deleted-owner',2);
  await db.admin.query('delete from ai_private.principals where id=$1',[a.principal]);
  assert.equal((await db.admin.query(
    `select count(*)::int n from billing_private.purchase_devices where principal=$1`,[a.principal])).rows[0].n,1,
    'the chain membership row outlives the principal row');
  const status=await rpc('status',{...b,...MEMBER});
  assert.equal(status.period.startsWith('member:'),true,JSON.stringify(status));
  assert.equal(status.used,0);
  assert.equal((await reserve({...b,...MEMBER})).remaining,29,
    'the member must still be able to reserve instead of failing the foreign key');
  assert.equal((await memberRow(b.principal,status.period)).rows[0].used,1);
  assert.equal((await rpc('status',{...b,...MEMBER})).used,1);
});

test('the reverse export carries the chain meter instead of dropping member usage',async()=>{
  const [a,b]=await chain('export-shared',2);
  await use({...a,...MEMBER},3);
  await use({...b,...MEMBER},2);
  const snapshot=await serviceRpc('ai_quota_export_legacy');
  const owner=snapshot.principals.find(x=>x.principal===a.principal);
  const member=owner.buckets.filter(x=>x.period.startsWith('member:'));
  assert.equal(member.length,1,JSON.stringify(owner.buckets));
  assert.equal(member[0].used,5,'the group usage must reach the legacy export');
  assert.equal(member[0].limit,50,'the legacy store keeps its own member limit');
  const peer=snapshot.principals.find(x=>x.principal===b.principal);
  assert.equal(peer.buckets.some(x=>x.period.startsWith('member:')),false,JSON.stringify(peer.buckets));
});

test('the shared member quota migration is re-runnable and fails closed on re-keying',async()=>{
  const sql=await readFile(new URL('../migrations/202609170020_shared_member_quota.sql',import.meta.url),'utf8');
  // Re-applying it on a ledger that already holds shared rows and member usage
  // must be a no-op: every object is create or replace, and the guard must not
  // fire on rows that already live at their chain owner.
  await db.admin.query(sql);
  await db.admin.query(sql);
  // The guard is what stops the switch from silently stranding usage: a member
  // row at a device whose best chain is owned by someone else would be re-keyed.
  const old=await database({through:'202609170019_billing_ai_quota_source.sql'});
  try{
    const owner='guest_'+'a'.repeat(24), peer='guest_'+'b'.repeat(24);
    await old.admin.query(`insert into ai_private.principals(id,support_code,free_limit)
      values($1,'TF-OWNER-01',30),($2,'TF-PEER-01',30)`,[owner,peer]);
    const purchase=(await old.admin.query(`insert into billing_private.store_purchases
        (provider,environment,purchase_key_hash,store_reference_ciphertext,principal,product_id,store_status)
      values('apple','sandbox',repeat('c',64),'cipher',$1,'${PRODUCT}','active') returning id`,[owner])).rows[0].id;
    await old.admin.query(`insert into billing_private.purchase_devices(purchase_id,principal)
      values($1,$2),($1,$3)`,[purchase,owner,peer]);
    await old.admin.query(`insert into ai_private.buckets(principal,period,used)
      values($1,'member:2026-09-17',3)`,[peer]);
    await assert.rejects(old.admin.query(sql),/would be re-keyed/);
    // With the row at its chain owner the same file applies cleanly.
    await old.admin.query(`update ai_private.buckets set principal=$1
      where principal=$2 and period='member:2026-09-17'`,[owner,peer]);
    await old.admin.query(sql);
  }finally{await old.close();}
});

test('retired development flag no longer grants a daily pool',async()=>{
  const p=guest();
  await rpc('membership',{...p,developmentAllowed:true,enabled:true});
  await use({...p,developmentAllowed:true},3);
  const status=await rpc('status',{...p,developmentAllowed:true});
  assert.equal(status.period,'free');
  assert.equal(status.used,3);
  assert.equal(status.limit,30);
  assert.equal(status.enabled,false);
  const a=await account();
  assert.equal((await rpc('membership',{...a,developmentAllowed:true,enabled:true})).code,'DEVELOPMENT_MEMBERSHIP_DISABLED');
});

test('claim takes max usage, carries request receipts and is safe to retry',async()=>{
  const g=guest(),a=await account();await use(g,7);await use(a,3);
  const payload={...a,guest:g.principal,guestSupportCode:g.supportCode};
  assert.equal((await rpc('claim',payload)).used,7);
  assert.equal((await rpc('claim',payload)).used,7);
  assert.equal((await rpc('status',g)).used,7);
  const id=(await db.admin.query('select request_id from ai_private.requests where principal=$1 limit 1',[g.principal])).rows[0].request_id;
  assert.equal((await reserve(a,id)).code,'AI_REQUEST_ALREADY_COMPLETED');
  // The installation's original support code still resolves to the live account.
  assert.equal((await rpc('admin_status',{supportCode:g.supportCode})).used,7);
  assert.equal((await rpc('admin_reset',{supportCode:g.supportCode})).used,0);
  assert.equal((await rpc('claim',{...await account(),guest:g.principal,guestSupportCode:g.supportCode})).code,'AI_GUEST_ALREADY_CLAIMED');
  await db.admin.query('delete from auth.users where id=$1',[a.user.id]);
  assert.equal((await rpc('status',g)).used,0);
  assert.equal((await db.admin.query('select count(*)::int n from ai_private.requests where principal=$1',[a.principal])).rows[0].n,0);
});

test('claim waits for in-flight calls and concurrent claimers have one winner',async()=>{
  const g=guest(),a=await account(),b=await account();const id=randomUUID();const pending=await reserve(g,id);
  const claim=actor=>rpc('claim',{...actor,guest:g.principal,guestSupportCode:g.supportCode});
  assert.equal((await claim(a)).code,'AI_REQUEST_IN_PROGRESS');
  await finish(g,id,pending.attempt);
  const results=await Promise.all([claim(a),claim(b)]);
  assert.equal(results.filter(x=>x.used===1).length,1);
  assert.equal(results.filter(x=>x.code==='AI_GUEST_ALREADY_CLAIMED').length,1);
});

test('revoked and deleting accounts fail closed, including after identity resolution',async()=>{
  const a=await account();await rpc('status',a);
  await db.admin.query('delete from auth.sessions where id=$1',[a.sessionID]);
  assert.equal((await reserve(a)).code,'ACCOUNT_UNAVAILABLE');
  const b=await account();await db.admin.query('insert into sync_private.accounts(user_id,deletion_pending) values($1,true)',[b.user.id]);
  await assert.rejects(db.rpc(b.device,'ai_account_identity'),/ACCOUNT_UNAVAILABLE/);
  assert.equal((await reserve(b)).code,'ACCOUNT_UNAVAILABLE');
});

test('admin resets preserve receipts and reject outstanding reservations',async()=>{
  const p=guest();const id=randomUUID();const r=await reserve(p,id);
  assert.equal((await rpc('admin_reset',{supportCode:p.supportCode})).code,'AI_REQUEST_IN_PROGRESS');
  await finish(p,id,r.attempt);
  assert.equal((await rpc('admin_reset',{supportCode:p.supportCode})).used,0);
  assert.equal((await reserve(p,id)).code,'AI_REQUEST_ALREADY_COMPLETED');
});

test('rollback export returns guests only and excludes abandoned reservations',async()=>{
  const g=guest(),a=await account();const ids=[randomUUID(),randomUUID()];
  for(const id of ids){const r=await reserve(g,id);await finish(g,id,r.attempt);}
  await use(a,3);
  await reserve(g,randomUUID());
  const snapshot=await serviceRpc('ai_quota_export_legacy');
  assert.equal(typeof snapshot.exportedAt,'string');
  const entry=snapshot.principals.find(p=>p.principal===g.principal);
  assert.ok(entry,'guest must be exported');
  assert.equal(entry.supportCode,g.supportCode);
  assert.equal(entry.buckets.find(b=>b.period==='free').used,2);
  assert.equal(entry.buckets.find(b=>b.period==='free').limit,30);
  assert.deepEqual([...entry.completedRequests].sort(),[...ids].sort());
  assert.equal(snapshot.principals.some(p=>p.principal===a.principal),false,'account must not enter the legacy export');
  for(const role of ['anon','authenticated']){
    await assert.rejects(db.rpc(a.device,'ai_quota_export_legacy',[],role),/permission denied/);
    await assert.rejects(db.rpc(a.device,'ai_quota_rollback',[],role),/permission denied/);
    await assert.rejects(db.rpc(a.device,'ai_quota_reset_import',[],role),/permission denied/);
  }
});

test('rollback refunds reservations, closes the gate and clears guests before re-cutover',async()=>{
  const g=guest(),a=await account();const done=randomUUID();
  const consumed=await reserve(g,done);await finish(g,done,consumed.attempt);
  const pendingId=randomUUID();await reserve(g,pendingId);
  await reserve(a,randomUUID());
  const closed=await serviceRpc('ai_quota_rollback');
  assert.equal(closed.gateOpen,false);
  assert.ok(closed.refundedReservations>=1);
  assert.equal((await db.admin.query('select state from ai_private.requests where principal=$1 and request_id=$2',[g.principal,pendingId])).rows[0].state,'refunded');
  assert.equal((await db.admin.query('select used from ai_private.buckets where principal=$1 and period=$2',[g.principal,'free'])).rows[0].used,1);
  await assert.rejects(reserve(g),/AI_IMPORT_REQUIRED/);
  const reset=await serviceRpc('ai_quota_reset_import');
  assert.ok(reset.removedGuestPrincipals>0);
  assert.equal((await db.admin.query('select count(*)::int n from ai_private.principals where id=$1',[g.principal])).rows[0].n,0);
  assert.equal((await db.admin.query('select count(*)::int n from ai_private.principals where id=$1',[a.principal])).rows[0].n,1);
  await rpc('import',{...g,limit:50,developmentEnabled:false,importHash:'rollback-restore',
    buckets:[{period:'free',used:1}],completedRequests:[done]});
  await rpc('finish_import');
  const restored=await rpc('status',g);
  assert.equal(restored.used,1);
  assert.equal(restored.limit,30);
  assert.equal((await reserve(g,done)).code,'AI_REQUEST_ALREADY_COMPLETED');
});
