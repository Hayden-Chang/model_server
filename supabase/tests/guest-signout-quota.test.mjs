import test, {before, after} from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID, createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {database} from './database.mjs';

let db;
before(async()=>{db=await database();await rpc('finish_import');});
after(async()=>{await db?.close();});
const hash='a'.repeat(64);
function guest(){
  const value=createHash('sha256').update(randomUUID()).digest('hex');
  return {principal:'guest_'+value.slice(0,24),supportCode:'TF-'+value.slice(24,32),freeLimit:30};
}
async function account(){
  const user=await db.account(),device=await user.device();
  return {...guest(),principal:'account:'+user.id,sessionID:device.session,user,device};
}
const rpc=(action,data={})=>db.rpc(null,'ai_quota_service',[action,data],'service_role');
const claim=(g,a)=>rpc('claim',{...a,guest:g.principal,guestSupportCode:g.supportCode});
const reserve=(p,requestID=randomUUID(),extra={})=>rpc('reserve',{...p,requestID,bodyHash:hash,attempt:randomUUID(),...extra});
const finish=(p,id,attempt,consume=true)=>rpc('finish',{...p,requestID:id,attempt,consume});
async function use(p,count){
  for(let i=0;i<count;i++){
    const id=randomUUID(),r=await reserve(p,id);
    assert.ok(r.attempt,JSON.stringify(r));
    assert.deepEqual(await finish(p,id,r.attempt),{ok:true});
  }
}

test('signed-out claimed guest can keep planning without replenishing free quota',async()=>{
  const g=guest(),a=await account();await use(g,7);await use(a,3);
  assert.equal((await claim(g,a)).used,7);
  const signedOut=await rpc('status',g);
  assert.equal(signedOut.remaining,23,JSON.stringify(signedOut));
  await use(a,2);await use(g,1);
  assert.equal((await rpc('status',g)).used,10);
  assert.equal((await rpc('status',a)).used,10);
  for(let i=0;i<3;i++)assert.equal((await claim(g,a)).remaining,20);
  await db.admin.query('delete from auth.sessions where id=$1',[a.sessionID]);
  assert.equal((await reserve(a)).code,'ACCOUNT_UNAVAILABLE');
  await use(g,1);
  assert.equal((await rpc('status',g)).remaining,19);
});

test('linked guests and account serialize the last free call and refunds',async()=>{
  const g=guest(),g2=guest(),a=await account();await use(g,29);
  await claim(g,a);await claim(g2,a);
  const actors=[g,a,g2],ids=actors.map(()=>randomUUID());
  const results=await Promise.all(actors.map((p,i)=>reserve(p,ids[i])));
  assert.equal(results.filter(x=>x.attempt).length,1);
  assert.equal(results.filter(x=>x.code==='AI_QUOTA_EXHAUSTED').length,2);
  const winner=results.findIndex(x=>x.attempt);
  await finish(actors[winner],ids[winner],results[winner].attempt,false);
  await finish(actors[winner],ids[winner],results[winner].attempt,false);
  for(const p of actors)assert.equal((await rpc('status',p)).remaining,1);
  await use(g2,1);
  for(const p of actors)assert.equal((await reserve(p)).code,'AI_QUOTA_EXHAUSTED');
});

test('signed-out guest cannot inherit account membership or billing access',async()=>{
  const g=guest(),a=await account();await use(g,4);await claim(g,a);
  await db.admin.query(`insert into billing_private.account_entitlements(user_id,plan,status)
    values($1,'plus','active')`,[a.user.id]);
  await use(a,3);
  assert.ok((await rpc('status',a)).period.startsWith('member:'));
  const status=await rpc('status',g);
  assert.equal(status.period,'free');assert.equal(status.used,4);assert.equal(status.resetsAt,null);
  await use(g,1);
  assert.equal((await rpc('status',a)).used,3);
  assert.equal((await rpc('status',g)).used,5);
  const denied=await db.rpc(null,'billing_service',['entitlement',g],'service_role');
  assert.equal(denied.code,'ACCOUNT_REQUIRED');
});

test('linked request replay is rejected across login changes',async()=>{
  const g=guest(),a=await account();await claim(g,a);
  const id=randomUUID(),r=await reserve(g,id);
  assert.equal((await reserve(a,id)).code,'AI_REQUEST_IN_PROGRESS');
  await finish(g,id,r.attempt);
  assert.equal((await reserve(a,id)).code,'AI_REQUEST_ALREADY_COMPLETED');
  assert.equal((await reserve(a,id,{bodyHash:'b'.repeat(64)})).code,'AI_REQUEST_ID_CONFLICT');
  const id2=randomUUID(),r2=await reserve(a,id2);await finish(a,id2,r2.attempt);
  assert.equal((await reserve(g,id2)).code,'AI_REQUEST_ALREADY_COMPLETED');
});

test('expiry from either identity refunds the shared pool once',async()=>{
  const g=guest(),a=await account();await claim(g,a);
  const id=randomUUID(),r=await reserve(g,id);
  await db.admin.query("update ai_private.requests set expires_at=now()-interval '1 second' where principal=$1",[g.principal]);
  assert.equal((await rpc('status',a)).used,0);
  assert.equal((await rpc('status',g)).used,0);
  const retry=await reserve(a,id);
  assert.equal((await finish(g,id,r.attempt)).code,'AI_RESERVATION_EXPIRED');
  await finish(a,id,retry.attempt);
  assert.equal((await rpc('status',g)).used,1);
});

test('account deletion leaves guest usage and replay protection intact',async()=>{
  const g=guest(),a=await account();await use(g,6);await claim(g,a);await use(a,2);
  const id=randomUUID(),r=await reserve(g,id);await finish(g,id,r.attempt);
  await db.admin.query('delete from auth.users where id=$1',[a.user.id]);
  assert.equal((await rpc('status',g)).used,9);
  assert.equal((await reserve(g,id)).code,'AI_REQUEST_ALREADY_COMPLETED');
  await use(g,1);
  assert.equal((await rpc('status',g)).remaining,20);
});

test('billing free quota reflects signed-out guest consumption',async()=>{
  const g=guest(),a=await account();await claim(g,a);await use(g,3);await use(a,2);
  const result=await db.rpc(null,'billing_service',['entitlement',{principal:a.principal,sessionID:a.sessionID}],'service_role');
  assert.equal(result.aiQuota.used,5);assert.equal(result.aiQuota.remaining,25);
});

test('support reset waits for every linked identity and resets only its free group',async()=>{
  const g=guest(),a=await account(),other=guest();await claim(g,a);await use(other,2);
  const id=randomUUID(),r=await reserve(g,id);
  assert.equal((await rpc('admin_reset',{supportCode:a.supportCode})).code,'AI_REQUEST_IN_PROGRESS');
  await finish(g,id,r.attempt);
  assert.equal((await rpc('admin_reset',{supportCode:g.supportCode})).used,0);
  assert.equal((await rpc('status',a)).used,0);
  assert.equal((await rpc('status',other)).used,2);
});

test('legacy rollback snapshot uses shared consumption and refunds all abandoned calls',async()=>{
  const g=guest(),a=await account();await claim(g,a);await use(a,3);await use(g,2);
  await reserve(g);await reserve(a);
  const snapshot=await db.rpc(null,'ai_quota_export_legacy',[],'service_role');
  const entry=snapshot.principals.find(x=>x.principal===g.principal);
  assert.equal(entry.buckets.find(x=>x.period==='free').used,5);
  assert.equal(entry.completedRequests.length,5);
  await db.rpc(null,'ai_quota_rollback',[],'service_role');
  await rpc('finish_import');
  assert.equal((await rpc('status',g)).used,5);
  assert.equal((await rpc('status',a)).used,5);
});

test('free pool storage is private and duplicate principal creation does not leak pools',async()=>{
  const g=guest();
  for(let i=0;i<4;i++)await rpc('status',g);
  assert.equal((await db.admin.query(`select count(*)::int n from ai_private.free_pools f
    where not exists(select 1 from ai_private.principals p where p.free_pool_id=f.id)`)).rows[0].n,0);
  for(const role of ['anon','authenticated','service_role']){
    await assert.rejects(db.call(null,'select * from ai_private.free_pools',[],role),/permission denied/);
  }
});

test('forward migration restores existing claimed guests without resetting usage or pending refunds',async()=>{
  const old=await database({through:'202609130015_free_quota_30.sql'});
  const oldRpc=(action,data={})=>old.rpc(null,'ai_quota_service',[action,data],'service_role');
  const oldUse=async(p,count)=>{
    for(let i=0;i<count;i++){
      const requestID=randomUUID(),attempt=randomUUID();
      assert.equal((await oldRpc('reserve',{...p,requestID,attempt,bodyHash:hash})).attempt,attempt);
      await oldRpc('finish',{...p,requestID,attempt,consume:true});
    }
  };
  try{
    await oldRpc('finish_import');
    const g=guest(),g2=guest(),exhausted=guest();
    const user=await old.account(),device=await user.device();
    const a={...guest(),principal:'account:'+user.id,sessionID:device.session};
    await oldUse(g,7);await oldUse(a,3);
    await oldRpc('claim',{...a,guest:g.principal,guestSupportCode:g.supportCode});
    await oldUse(a,2);
    await oldRpc('claim',{...a,guest:g2.principal,guestSupportCode:g2.supportCode});
    assert.equal((await oldRpc('status',g)).code,'AI_ACCOUNT_REQUIRED');
    const requestID=randomUUID(),attempt=randomUUID();
    await oldRpc('reserve',{...a,requestID,attempt,bodyHash:hash});
    await oldRpc('status',exhausted);
    await old.admin.query("insert into ai_private.buckets(principal,period,used) values($1,'free',35)",[exhausted.principal]);
    await old.admin.query(await readFile(new URL('../migrations/202609140016_guest_free_pool.sql',import.meta.url),'utf8'));
    for(const p of [g,g2,a])assert.equal((await oldRpc('status',p)).used,10);
    await oldRpc('finish',{...a,requestID,attempt,consume:false});
    for(const p of [g,g2,a])assert.equal((await oldRpc('status',p)).remaining,21);
    await oldUse(g,1);
    assert.equal((await oldRpc('status',a)).used,10);
    assert.equal((await oldRpc('status',exhausted)).used,35);
    assert.equal((await oldRpc('status',exhausted)).remaining,0);
    assert.equal((await old.admin.query("select count(*)::int n from sync_private.contracts where name in ('cloud-state-v2','operation-v2')")).rows[0].n,2);
  }finally{await old.close();}
});
