import test, {before, after} from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID, createHash} from 'node:crypto';
import {database} from './database.mjs';

let db;
before(async () => { db=await database(); });
after(async () => { await db?.close(); });
const hash='a'.repeat(64);
function guest() {
  const value=createHash('sha256').update(randomUUID()).digest('hex');
  return {principal:'guest_'+value.slice(0,24),supportCode:'TF-'+value.slice(24,32),freeLimit:50};
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

test('migration gate rejects new quota; imports metadata exactly once',async()=>{
  const p=guest();
  await assert.rejects(rpc('status',p),/AI_IMPORT_REQUIRED/);
  const data={...p,limit:50,developmentEnabled:true,importHash:'fixed',
    buckets:[{period:'free',used:21}],completedRequests:['legacy-completed']};
  await rpc('import',data);await rpc('import',data);
  await assert.rejects(rpc('import',{...data,importHash:'changed'}),/AI_IMPORT_CHANGED/);
  await rpc('finish_import');
  assert.equal((await rpc('status',p)).used,21);
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

test('different sessions share 50 account calls while another account stays independent',async()=>{
  const a=await account();const other=await account();const second=await a.user.device();
  await use(a,25);await use({...a,sessionID:second.session},25);
  assert.equal((await reserve(a)).code,'AI_QUOTA_EXHAUSTED');
  assert.equal((await rpc('status',a)).used,50);
  assert.equal((await rpc('status',other)).remaining,50);
});

test('concurrent reservations cannot overspend or invoke the same request twice',async()=>{
  const p={...guest(),freeLimit:2};
  const results=await Promise.all(Array.from({length:8},()=>reserve(p)));
  assert.equal(results.filter(x=>x.attempt).length,2);
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

test('retired development flag no longer grants a daily pool',async()=>{
  await rpc('membership',{...p,developmentAllowed:true,enabled:true});
  await use({...p,developmentAllowed:true},3);
  const status=await rpc('status',{...p,developmentAllowed:true});
  assert.equal(status.period,'free');
  assert.equal(status.used,3);
  assert.equal(status.limit,50);
  assert.equal(status.enabled,false);
  const a=await account();
  assert.equal((await rpc('membership',{...a,developmentAllowed:true,enabled:true})).code,'DEVELOPMENT_MEMBERSHIP_DISABLED');
});

test('claim takes max usage, carries request receipts and is safe to retry',async()=>{
  const g=guest(),a=await account();await use(g,7);await use(a,3);
  const payload={...a,guest:g.principal,guestSupportCode:g.supportCode};
  assert.equal((await rpc('claim',payload)).used,7);
  assert.equal((await rpc('claim',payload)).used,7);
  assert.equal((await rpc('status',g)).code,'AI_ACCOUNT_REQUIRED');
  const id=(await db.admin.query('select request_id from ai_private.requests where principal=$1 limit 1',[g.principal])).rows[0].request_id;
  assert.equal((await reserve(a,id)).code,'AI_REQUEST_ALREADY_COMPLETED');
  // The installation's original support code still resolves to the live account.
  assert.equal((await rpc('admin_status',{supportCode:g.supportCode})).used,7);
  assert.equal((await rpc('admin_reset',{supportCode:g.supportCode})).used,0);
  assert.equal((await rpc('claim',{...await account(),guest:g.principal,guestSupportCode:g.supportCode})).code,'AI_GUEST_ALREADY_CLAIMED');
  await db.admin.query('delete from auth.users where id=$1',[a.user.id]);
  assert.equal((await rpc('status',g)).code,'AI_ACCOUNT_REQUIRED');
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
  assert.equal(entry.buckets.find(b=>b.period==='free').limit,50);
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
  assert.equal((await rpc('status',g)).used,1);
  assert.equal((await reserve(g,done)).code,'AI_REQUEST_ALREADY_COMPLETED');
});
