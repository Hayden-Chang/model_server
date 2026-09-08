import { test } from 'node:test';
import assert from 'node:assert/strict';
import { processDeletions, transport } from '../scripts/deletion-worker.mjs';

test('Auth failure preserves a deletion job for a later retry',async () => {
  const calls=[];
  const rpc=async name=>{ calls.push(name); return name==='pending_account_deletions'?[{requestID:'request'}]:{status:'dataDeleted',userID:'user'}; };
  assert.deepEqual(await processDeletions({rpc,deleteUser:async()=>{throw Error('temporary');}}),{completed:0,pending:1});
  assert.equal(calls.includes('complete_account_deletion'),false);
  assert.deepEqual(await processDeletions({rpc,deleteUser:async()=>{}}),{completed:1,pending:0});
  assert.equal(calls.at(-1),'complete_account_deletion');
});
test('lost Auth delete response is recoverable and transport never leaks a body',async () => {
  const client=transport('https://project.example','test-key',async()=>new Response('private response',{status:404}));
  await client.deleteUser('test-user');
  await assert.rejects(client.rpc('test',{}),error=>error.message==='Service request failed: 404');
  assert.throws(()=>transport('http://remote.example','test-key'),/HTTPS/);
});
