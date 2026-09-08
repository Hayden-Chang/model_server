import { before, beforeEach, after, test } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { database } from './database.mjs';
import { empty, withTask, operation } from './fixtures.mjs';
let db;
before(async()=>{db=await database();});
after(async()=>{await db?.close();});
beforeEach(async()=>{
  await db.admin.query('delete from auth.users; delete from sync_private.account_deletions');
});
async function initialized() {
  const account=await db.account(),device=await account.device(),state=withTask();
  await db.rpc(device,'register_sync_device',[device.id,'ios','worker-test',1]);
  const cloud=await db.rpc(device,'initialize_sync_state',[device.id,randomUUID(),state]);
  cloud.state=state;
  return {account,device,cloud};
}
const batch=(limit=100,role='service_role')=>db.rpc(null,'maintain_sync_batch',[limit],role);

test('maintenance batches are bounded, fair, private and skip accounts awaiting deletion',async()=>{
  const users=await Promise.all([initialized(),initialized(),initialized(),initialized()]);
  await db.admin.query('update sync_private.accounts set deletion_pending=true where user_id=$1',[users[3].device.user]);
  for(const role of ['anon','authenticated']) await assert.rejects(batch(2,role),/permission denied/);
  for(const limit of [null,0,101]) await assert.rejects(batch(limit),/payloadInvalid/);
  assert.deepEqual(await batch(2),{accountsMaintained:2,accountsFailed:0,operationsDeleted:0,checkpointsDeleted:0,receiptsDeleted:0});
  assert.equal((await db.admin.query("select count(*)::int n from sync_private.accounts where maintenance_attempted_at<>'-infinity'")).rows[0].n,2);
  assert.equal((await batch(1)).accountsMaintained,1);
  assert.equal((await db.admin.query("select count(*)::int n from sync_private.accounts where maintenance_attempted_at<>'-infinity'")).rows[0].n,3);
  assert.equal((await db.rpc(users[3].device,'sync_account_status')).status,'deletionPending');
});

test('busy account locks are skipped without blocking the remaining batch',async()=>{
  const a=await initialized();await initialized();
  await db.admin.query('begin');
  try {
    await db.admin.query('select 1 from sync_private.accounts where user_id=$1 for update',[a.device.user]);
    assert.equal((await batch()).accountsMaintained,1);
  } finally {await db.admin.query('rollback');}
  assert.equal((await batch(1)).accountsMaintained,1);
});

test('scheduled maintenance preserves checkpoints until the safety window and device acknowledgement',async()=>{
  const {account,device,cloud}=await initialized(),peer=await account.device();
  await db.rpc(peer,'register_sync_device',[peer.id,'android','worker-test',1]);
  const replaced=await db.rpc(device,'replace_sync_state',[device.id,randomUUID(),cloud.generation,cloud.revision,empty()]);
  for(const d of [device,peer]) await db.rpc(d,'acknowledge_sync_state',[d.id,replaced.generation,replaced.revision,replaced.stateHash]);
  assert.equal((await batch()).checkpointsDeleted,0);
  await db.admin.query("update sync_private.state_checkpoints set delete_after=now()-interval '1 second'");
  await db.admin.query('update sync_private.devices set generation=1 where device_id=$1',[peer.id]);
  assert.equal((await batch()).checkpointsDeleted,0);
  await db.rpc(peer,'acknowledge_sync_state',[peer.id,replaced.generation,replaced.revision,replaced.stateHash]);
  assert.equal((await batch()).checkpointsDeleted,1);
  assert.deepEqual((await db.rpc(device,'pull_sync_state',[device.id])).state,empty());
});

test('scheduled maintenance retains unsafe operation receipts and only expires completed deletion receipts',async()=>{
  const {device,cloud}=await initialized(),state=structuredClone(cloud.state);
  state.tasks[0].title='updated';
  const op=operation(device,cloud,state);
  const accepted=await db.rpc(device,'commit_sync_state',[op,state]);
  await db.admin.query("update sync_private.sync_operations set accepted_at=now()-interval '31 days'");
  assert.equal((await batch()).operationsDeleted,0);
  const replaced=await db.rpc(device,'replace_sync_state',[device.id,randomUUID(),accepted.generation,accepted.revision,empty()]);
  assert.equal((await batch()).operationsDeleted,0);
  await db.rpc(device,'acknowledge_sync_state',[device.id,replaced.generation,replaced.revision,replaced.stateHash]);
  for(const [status,age] of [['completed',31],['completed',1],['pending',31],['dataDeleted',31]]) {
    await db.admin.query(`insert into sync_private.account_deletions(request_id,receipt_hash,status,completed_at)
      values($1,$2,$3,now()-($4::int*interval '1 day'))`,[randomUUID(),randomUUID(),status,age]);
  }
  const result=await batch();assert.equal(result.operationsDeleted,1);assert.equal(result.receiptsDeleted,1);
  assert.equal((await db.admin.query('select count(*)::int n from sync_private.account_deletions')).rows[0].n,3);
  assert.deepEqual((await db.rpc(device,'pull_sync_state',[device.id])).state,empty());
});

test('one failing cleanup rolls back its account and does not starve another account',async()=>{
  const a=await initialized(),b=await initialized();
  for(const {device,cloud} of [a,b]) {
    await db.rpc(device,'replace_sync_state',[device.id,randomUUID(),cloud.generation,cloud.revision,empty()]);
  }
  await db.admin.query("update sync_private.state_checkpoints set delete_after=now()-interval '1 second'; update sync_private.devices set revoked_at=now()");
  await db.admin.query(`create function sync_private.test_reject_cleanup() returns trigger language plpgsql as $$
    begin if old.user_id='${a.device.user}'::uuid then raise exception 'test failure'; end if; return old; end $$;
    create trigger test_reject_cleanup before delete on sync_private.state_checkpoints
    for each row execute function sync_private.test_reject_cleanup()`);
  try {
    const result=await batch();
    assert.equal(result.accountsFailed,1);assert.equal(result.accountsMaintained,1);assert.equal(result.checkpointsDeleted,1);
    assert.deepEqual((await db.admin.query('select user_id from sync_private.state_checkpoints')).rows,[{user_id:a.device.user}]);
    assert.equal((await db.admin.query("select count(*)::int n from sync_private.accounts where maintenance_attempted_at<>'-infinity'")).rows[0].n,2);
  } finally {
    await db.admin.query('drop trigger test_reject_cleanup on sync_private.state_checkpoints; drop function sync_private.test_reject_cleanup()');
  }
  assert.equal((await batch()).checkpointsDeleted,1);
});
