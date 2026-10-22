import test, {before, after, beforeEach} from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID, createHash} from 'node:crypto';
import {database} from './database.mjs';
let db;
before(async()=>{db=await database(); await quota('finish_import');});
after(async()=>{await db?.close();});
beforeEach(async()=>{await db.admin.query('truncate ai_private.duration_runs cascade');});
const quota=(action,data={})=>db.rpc(null,'ai_quota_service',[action,data],'service_role');
const rpc=(action,data={})=>db.rpc(null,'duration_analysis_run_service',[action,data],'service_role');
function identity(){const value=createHash('sha256').update(randomUUID()).digest('hex');
  return {principal:'guest_'+value.slice(0,24),supportCode:'TF-'+value.slice(16,24),freeLimit:30,
    billingEnvironment:'sandbox',runID:randomUUID()};}
function step(key){return {key,input:{category:key},inputHash:createHash('sha256').update(key).digest('hex')};}
async function start(p,steps=[step('meal')]){
  return rpc('start',{...p,input:{frozen:'same'},inputHash:'b'.repeat(64),attempt:randomUUID(),steps,
    quotaData:{supportCode:p.supportCode,freeLimit:30,memberLimit:30}});
}
const claim=()=>rpc('claim',{billingEnvironment:'sandbox',attempt:randomUUID()});
const finish=(s,result={items:[{difficulty:1}]},steps=[])=>rpc('complete',{...s,result,steps});

test('start reserves once, immutable replay and private status are isolated',async()=>{
  const p=identity();const a=await start(p);const b=await start(p);
  assert.equal(a.runID,b.runID);assert.equal((await quota('status',p)).used,1);
  assert.equal((await rpc('start',{...p,inputHash:'c'.repeat(64)})).code,'AI_REQUEST_ID_CONFLICT');
  assert.equal((await quota('status',p)).used,1);
  assert.equal((await rpc('get',{...p,principal:identity().principal})).code,'ANALYSIS_RUN_NOT_FOUND');
  assert.equal((await rpc('get',{...p,billingEnvironment:'production'})).code,'ANALYSIS_RUN_NOT_FOUND');
  for(const role of ['anon','authenticated']){
    await assert.rejects(db.rpc(null,'duration_analysis_run_service',['get',p],role),/permission denied/);
    await assert.rejects(db.call(null,'select * from ai_private.duration_runs',[],role),/permission denied/);
  }
});

test('saved result and debit are atomic; lost response replays without another charge',async()=>{
  const p=identity();await start(p);const s=await claim();
  await db.admin.query("update ai_private.requests set state='refunded' where principal=$1",[p.principal]);
  await assert.rejects(finish(s),/ANALYSIS_QUOTA_FINISH_FAILED/);
  assert.equal((await rpc('get',p)).steps[0].result,null);
  assert.equal((await rpc('get',p)).steps[0].status,'running');
  await db.admin.query("update ai_private.requests set state='reserved' where principal=$1",[p.principal]);
  const done=await finish(s);assert.equal(done.status,'completed');assert.equal(done.chargedPoints,1);
  assert.deepEqual(await finish(s),done);assert.deepEqual(await rpc('get',p),done);
  assert.equal((await quota('status',p)).used,1);
});

test('partial retry keeps successful snapshots and only queues failed work',async()=>{
  const p=identity();await start(p,[step('a'),step('b')]);
  const a=await claim();const b=await claim();await finish(a);
  const partial=await rpc('fail',b);assert.equal(partial.status,'partial');assert.equal(partial.chargedPoints,1);
  await rpc('retry',{...p,attempt:randomUUID()});const next=await claim();assert.equal(next.key,b.key);
  assert.deepEqual((await rpc('get',p)).steps.find(x=>x.categoryID===a.key).result,{items:[{difficulty:1}]});
  await finish(next);assert.equal((await quota('status',p)).used,1);assert.deepEqual(await claim(),{});
});

test('total failure refunds once; retry can consume at most one point',async()=>{
  const p=identity();await start(p);const s=await claim();
  assert.equal((await rpc('fail',s)).status,'failed');assert.equal((await quota('status',p)).used,0);
  await rpc('fail',s);assert.equal((await quota('status',p)).used,0);
  await rpc('retry',{...p,attempt:randomUUID()});assert.equal((await quota('status',p)).used,1);
  await finish(await claim());assert.equal((await quota('status',p)).used,1);
});

test('crashed worker lease recovers and old attempt cannot publish',async()=>{
  const p=identity();await start(p);const old=await claim();assert.deepEqual(await claim(),{});
  await db.admin.query("update ai_private.duration_steps set lease_until=now()-interval '1 second'");
  const replacement=await claim();assert.equal(replacement.key,old.key);assert.notEqual(replacement.attempt,old.attempt);
  assert.equal((await finish(old)).code,'ANALYSIS_STALE_ATTEMPT');
  await finish(replacement);assert.equal((await quota('status',p)).used,1);
});

test('twenty active categories is a shared limit, not a total category cap',async()=>{
  const p=identity();await start(p,Array.from({length:27},(_,i)=>step(String(i).padStart(2,'0'))));
  const active=[];
  for(let round=0;round<40 && active.length<20;round++){
    const values=await Promise.all(Array.from({length:8},claim));active.push(...values.filter(x=>x.key));
    assert.ok(active.length<=20);
  }
  assert.equal(active.length,20);assert.deepEqual(await claim(),{});
  await finish(active.shift());const extra=await claim();assert.ok(extra.key);active.push(extra);
  for(const s of active)await finish(s);
  let next;while((next=await claim()).key)await finish(next);
  const done=await rpc('get',p);assert.equal(done.steps.length,27);assert.equal(done.status,'completed');
  assert.equal((await quota('status',p)).used,1);
});

test('classification checkpoint and category jobs save together; server cache is complete',async()=>{
  const p=identity();await start(p,[step('__classify__')]);const c=await claim();
  const classified={categories:[{id:'meal'}],items:[{id:'new',categoryID:'meal'}]};
  const checkpoint=await finish(c,classified,[step('meal')]);
  assert.deepEqual(checkpoint.classification,classified);assert.equal(checkpoint.steps[0].status,'queued');
  await finish(await claim());
  const p2={...p,runID:randomUUID()};const cached=await start(p2,[step('meal')]);
  assert.equal(cached.status,'completed');assert.equal(cached.chargedPoints,0);
  assert.equal(cached.steps[0].result.cached,true);assert.ok(cached.steps[0].result.items);
  assert.equal((await quota('status',p2)).used,1,'only the first real analysis charged');
  const foreign=await start(identity(),[{...step('meal'),cached:true}]);
  assert.equal(foreign.steps[0].status,'queued','client hint cannot impersonate a server cache hit');
});


test('classification pagination survives restart without rerunning saved pages',async()=>{
  const p=identity();await start(p,[step('__classify__')]);const first=await claim();
  const page={categories:[],items:[{id:'one',categoryID:'meal'}]};
  const nextInput={tasks:[{id:'two'}]};
  await rpc('complete',{...first,result:page,steps:[],nextInput});
  assert.deepEqual((await rpc('get',p)).classification,page);
  const second=await claim();assert.deepEqual(second.input,nextInput);assert.deepEqual(second.classification,page);
  assert.equal((await finish(first)).code,'ANALYSIS_STALE_ATTEMPT');
  await finish(second,{categories:[],items:[...page.items,{id:'two',categoryID:'meal'}]},[step('meal')]);
  assert.equal((await rpc('get',p)).classification.items.length,2);
  await finish(await claim());assert.equal((await quota('status',p)).used,1);
});

test('expired run recovery and quota status use compatible locks',async()=>{
  const p=identity();await start(p);const old=await claim();
  await db.admin.query("update ai_private.duration_steps set lease_until=now()-interval '1 second'");
  await db.admin.query("update ai_private.requests set expires_at=now()-interval '1 second' where principal=$1",[p.principal]);
  const [recovered]=await Promise.all([claim(),quota('status',p)]);
  assert.ok(recovered.key);assert.notEqual(recovered.attempt,old.attempt);
  await finish(recovered);assert.equal((await quota('status',p)).used,1);
});

test('queue consumers cannot claim another billing environment',async()=>{
  const p=identity();await start(p);
  assert.deepEqual(await rpc('claim',{billingEnvironment:'production',attempt:randomUUID()}),{});
  assert.ok((await claim()).key);
});
