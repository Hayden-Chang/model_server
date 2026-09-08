import { before, after, test } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { randomBytes } from 'node:crypto';
import { database } from './database.mjs';
import { empty, withTask, operation, hash } from './fixtures.mjs';
let db;
before(async () => { db = await database(); });
after(async () => { await db?.close(); });
async function registered(account) {
  const d = await account.device();
  await db.rpc(d,'register_sync_device',[d.id,'ios','1.0',1]);
  return d;
}
async function initialized(state=empty()) {
  const a=await db.account(),d=await registered(a);
  const cloud=await db.rpc(d,'initialize_sync_state',[d.id,randomUUID(),state]);
  cloud.state=state;
  return {a,d,cloud};
}
test('initialize and retry return the original control receipt',async () => {
  const a=await db.account(),d=await registered(a),id=randomUUID();
  assert.equal((await db.rpc(d,'sync_account_status')).status,'uninitialized');
  const one=await db.rpc(d,'initialize_sync_state',[d.id,id,empty()]);
  const two=await db.rpc(d,'initialize_sync_state',[d.id,id,empty()]);
  assert.equal(one.revision,0); assert.equal(two.status,'duplicate'); assert.equal(two.syncSpaceID,one.syncSpaceID);
  await assert.rejects(db.rpc(d,'initialize_sync_state',[d.id,id,withTask()]),/requestIDReused/);
});
test('concurrent initialization preserves exactly one candidate',async () => {
  const a=await db.account(),d1=await registered(a),d2=await registered(a);
  const result=await Promise.allSettled([d1,d2].map(d=>db.rpc(d,'initialize_sync_state',[d.id,randomUUID(),withTask(d.id)])));
  assert.equal(result.filter(r=>r.status==='fulfilled').length,1);
  assert.match(result.find(r=>r.status==='rejected').reason.message,/firstSyncRequired/);
});
test('the fifth device slot is atomic and unregistered sessions cannot impersonate it',async () => {
  const a=await db.account(); const devices=[];
  for(let i=0;i<4;i++) devices.push(await registered(a));
  const d5=await a.device(),d6=await a.device();
  const result=await Promise.allSettled([d5,d6].map(d=>db.rpc(d,'register_sync_device',[d.id,'android','1.0',1])));
  assert.equal(result.filter(r=>r.status==='fulfilled').length,1);
  const denied=result[0].status==='rejected'?d5:d6;
  assert.match(result.find(r=>r.status==='rejected').reason.message,/deviceLimitReached/);
  assert.equal((await db.rpc(denied,'list_sync_devices')).length,5);
  await assert.rejects(db.rpc(denied,'pull_sync_state',[devices[0].id]),/deviceRequired/);
  await db.rpc(denied,'revoke_sync_device',[devices[0].id]);
  await assert.rejects(db.rpc(devices[0],'pull_sync_state',[devices[0].id]),/deviceRequired/);
  await db.rpc(denied,'register_sync_device',[denied.id,'android','1.0',1]);
});
test('CAS has one winner and accepted retries precede stale revision checks',async () => {
  const base=withTask(),{a,d,cloud}=await initialized(base),peer=await registered(a);
  await db.rpc(peer,'acknowledge_sync_state',[peer.id,cloud.generation,cloud.revision,cloud.stateHash]);
  const result=structuredClone(base); result.tasks[0].title='updated';
  const op=operation(d,cloud,result),other=operation(peer,cloud,result);
  const outcomes=await Promise.allSettled([[d,op],[peer,other]].map(([x,o])=>db.rpc(x,'commit_sync_state',[o,result])));
  assert.equal(outcomes.filter(r=>r.status==='fulfilled').length,1);
  assert.match(outcomes.find(r=>r.status==='rejected').reason.message,/revisionConflict/);
  const winner=outcomes[0].status==='fulfilled'?d:peer, intent=winner===d?op:other;
  const retry=await db.rpc(winner,'commit_sync_state',[intent,result]);
  assert.equal(retry.status,'duplicate'); assert.equal(retry.resultRevision,2);
  await assert.rejects(db.rpc(winner,'commit_sync_state',[{...intent,effectiveAt:'2026-09-08T11:00:00.000Z'},result]),/operationIDReused/);
  const pulled=await db.rpc(d,'pull_sync_state',[d.id,[intent.operationID],[]]);
  assert.equal(pulled.acceptedOperations.length,1); assert.equal(pulled.revision,2);
});
test('replacement records a safety checkpoint and cannot race a later write',async () => {
  const base=withTask(),{a,d,cloud}=await initialized(base),peer=await registered(a);
  await db.rpc(peer,'acknowledge_sync_state',[peer.id,cloud.generation,cloud.revision,cloud.stateHash]);
  const id=randomUUID(),candidate=empty();
  const replaced=await db.rpc(d,'replace_sync_state',[d.id,id,cloud.generation,cloud.revision,candidate]);
  assert.equal(replaced.generation,2); assert.equal(replaced.revision,2);
  assert.equal((await db.rpc(d,'replace_sync_state',[d.id,id,1,1,candidate])).status,'duplicate');
  const checkpoint=await db.rpc(d,'export_sync_checkpoint',[d.id,replaced.checkpointID]);
  assert.deepEqual(checkpoint.state,base);
  await assert.rejects(db.rpc(peer,'commit_sync_state',[operation(peer,cloud,base),base]),/generationConflict/);
  await assert.rejects(db.rpc(peer,'replace_sync_state',[peer.id,randomUUID(),1,1,base]),/revisionConflict/);
  const pulled=await db.rpc(d,'pull_sync_state',[d.id,[],[id]]);
  assert.equal(pulled.controlRequests.length,1);
});
test('table grants and RLS isolate accounts and revision signals',async () => {
  const {d,cloud}=await initialized(withTask()),other=await initialized();
  for(const table of ['accounts','devices','user_sync_state','sync_operations','sync_control_requests','state_checkpoints']) {
    await assert.rejects(db.call(d,'select * from sync_private.'+table),/permission denied/);
    await assert.rejects(db.call(d,'delete from sync_private.'+table),/permission denied/);
  }
  await assert.rejects(db.call(d,'update public.sync_changes set revision=999'),/permission denied/);
  const rows=await db.call(d,'select * from public.sync_changes');
  assert.equal(rows.length,1); assert.equal(rows[0].sync_space_id,cloud.syncSpaceID); assert.equal('state' in rows[0],false);
  await assert.rejects(db.rpc(other.d,'pull_sync_state',[d.id]),/deviceRequired/);
  await assert.rejects(db.rpc(null,'sync_account_status',[],'anon'),/permission denied/);
  await db.rpc(d,'revoke_sync_device',[d.id]);
  assert.equal((await db.call(d,'select * from public.sync_changes')).length,0);
});
test('preconditions and invalid DTO rejection do not advance revision',async () => {
  const state=withTask(),{d,cloud}=await initialized(state);
  const op=operation(d,cloud,state,{preconditions:{entityFingerprints:{['tasks/'+state.tasks[0].id]:hash({})},readSet:[],readSetFingerprint:hash({})}});
  await assert.rejects(db.rpc(d,'commit_sync_state',[op,state]),/preconditionFailed/);
  const broken=structuredClone(state); broken.tasks=[];
  await assert.rejects(db.rpc(d,'commit_sync_state',[operation(d,cloud,broken),broken]),/payloadInvalid/);
  assert.equal((await db.rpc(d,'pull_sync_state',[d.id])).revision,1);
});
test('expired sessions and leases stop cloud access',async () => {
  const {d}=await initialized();
  await db.admin.query("update sync_private.devices set lease_expires_at=now()-interval '1 second' where user_id=$1",[d.user]);
  await assert.rejects(db.rpc(d,'pull_sync_state',[d.id]),/deviceRequired/);
  assert.equal((await db.call(d,'select * from public.sync_changes')).length,0);
  await db.admin.query('delete from auth.sessions where id=$1',[d.session]);
  await assert.rejects(db.rpc(d,'sync_account_status'),/authRequired/);
});
test('read set changes and omitted write fingerprints stop an operation',async()=>{
  const state=withTask(),{d,cloud}=await initialized(state);
  const candidate=structuredClone(state);candidate.tasks[0].title='changed';
  const op=operation(d,cloud,candidate);
  op.preconditions.entityFingerprints={};
  await assert.rejects(db.rpc(d,'commit_sync_state',[op,candidate]),/preconditionRequired/);
  const other=operation(d,cloud,candidate);
  other.preconditions.readSet=[{collection:'tasks'}];
  await assert.rejects(db.rpc(d,'commit_sync_state',[other,candidate]),/preconditionFailed/);
  assert.equal((await db.rpc(d,'pull_sync_state',[d.id])).revision,1);
});
test('unconfirmed and anonymous Auth identities cannot register',async()=>{
  const a=await db.account(),d=await a.device();
  await db.admin.query('update auth.users set email_confirmed_at=null where id=$1',[a.id]);
  await assert.rejects(db.rpc(d,'register_sync_device',[d.id,'ios','1.0',1]),/authRequired/);
  await db.admin.query('update auth.users set email_confirmed_at=now(),is_anonymous=true where id=$1',[a.id]);
  await assert.rejects(db.rpc(d,'register_sync_device',[d.id,'ios','1.0',1]),/authRequired/);
});
test('a new login binding invalidates the old session and requires synchronization again',async()=>{
  const {a,d}=await initialized();
  const newer={...await a.device(),id:d.id};
  await db.rpc(newer,'register_sync_device',[newer.id,'ios','1.1',1]);
  await assert.rejects(db.rpc(d,'pull_sync_state',[d.id]),/deviceRequired/);
  assert.equal((await db.rpc(newer,'pull_sync_state',[newer.id])).status,'ready');
  await assert.rejects(db.rpc(newer,'register_sync_device',[randomUUID(),'ios','1.1',1]),/sessionAlreadyBound/);
});
test('old schema clients cannot read or overwrite a newer cloud schema',async()=>{
  const state=withTask(),{d,cloud}=await initialized(state);
  await db.admin.query('update sync_private.user_sync_state set schema_version=2 where user_id=$1',[d.user]);
  await assert.rejects(db.rpc(d,'pull_sync_state',[d.id]),/schemaTooNew/);
  await assert.rejects(db.rpc(d,'commit_sync_state',[operation(d,cloud,state),state]),/schemaTooNew/);
  await assert.rejects(db.rpc(d,'replace_sync_state',[d.id,randomUUID(),cloud.generation,cloud.revision,state]),/schemaTooNew/);
});
test('safety checkpoints survive until their window and every required device acknowledgement',async()=>{
  const {a,d,cloud}=await initialized(withTask()),peer=await registered(a),id=randomUUID();
  const replaced=await db.rpc(d,'replace_sync_state',[d.id,id,cloud.generation,cloud.revision,empty()]);
  await db.admin.query("update sync_private.state_checkpoints set delete_after=now()-interval '1 second' where user_id=$1",[d.user]);
  assert.equal((await db.rpc(null,'maintain_sync_account',[d.user],'service_role')).checkpointsDeleted,0);
  await db.rpc(d,'acknowledge_sync_state',[d.id,replaced.generation,replaced.revision,replaced.stateHash]);
  assert.equal((await db.rpc(null,'maintain_sync_account',[d.user],'service_role')).checkpointsDeleted,0);
  await db.rpc(peer,'acknowledge_sync_state',[peer.id,replaced.generation,replaced.revision,replaced.stateHash]);
  assert.equal((await db.rpc(null,'maintain_sync_account',[d.user],'service_role')).checkpointsDeleted,1);
  await assert.rejects(db.rpc(d,'maintain_sync_account',[d.user]),/permission denied/);
});
test('deletion is idempotent, blocks reinitialization, and waits for real Auth deletion',async()=>{
  const {d}=await initialized(withTask()),id=randomUUID(),receipt=randomBytes(32).toString('hex');
  const request=()=>db.rpc(d,'request_account_deletion',[id,receipt]);
  const outcomes=await Promise.all([request(),request()]);
  assert.deepEqual(outcomes,[{status:'pending'},{status:'pending'}]);
  await assert.rejects(db.rpc(d,'pull_sync_state',[d.id]),/deletionPending/);
  assert.equal((await db.rpc(d,'sync_account_status')).status,'deletionPending');
  assert.equal((await db.call(d,'select * from public.sync_changes')).length,0);
  await assert.rejects(db.rpc(d,'prepare_account_deletion',[id]),/permission denied/);
  assert.equal((await db.rpc(null,'prepare_account_deletion',[id],'service_role')).status,'dataDeleted');
  await assert.rejects(db.rpc(null,'complete_account_deletion',[id],'service_role'),/authDeletionIncomplete/);
  await assert.rejects(db.rpc(d,'initialize_sync_state',[d.id,randomUUID(),empty()]),/deletionPending/);
  assert.equal((await db.rpc(null,'account_deletion_status',[receipt],'anon')).status,'dataDeleted');
  await db.admin.query('delete from auth.users where id=$1',[d.user]);
  await db.rpc(null,'complete_account_deletion',[id],'service_role');
  await db.rpc(null,'complete_account_deletion',[id],'service_role');
  assert.equal((await db.rpc(null,'account_deletion_status',[receipt],'anon')).status,'completed');
  const job=(await db.admin.query('select user_id from sync_private.account_deletions where request_id=$1',[id])).rows[0];
  assert.equal(job.user_id,null);
  assert.equal((await db.rpc(null,'account_deletion_status',[randomBytes(32).toString('hex')],'anon')).status,'unknown');
});
test('account deletion requires recent OTP authentication',async()=>{
  const {d}=await initialized(),id=randomUUID(),receipt=randomBytes(32).toString('hex');
  // Use a single statement to change claims, then invoke the RPC under those claims.
  await assert.rejects(db.call(d,`with claims as materialized (select set_config('request.jwt.claims',$1,true))
    select public.request_account_deletion($2,$3) from claims`,[
    JSON.stringify({sub:d.user,session_id:d.session,amr:[{method:'otp',timestamp:1}]}),id,receipt]),/reauthRequired/);
});
