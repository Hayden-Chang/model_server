import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { main, runWorker } from '../scripts/background-worker.mjs';

test('scheduled deletion preserves pending work and retries it on the next invocation',async()=>{
  let failing=true;
  const client={rpc:async name=>name==='pending_account_deletions'?[{requestID:'test'}]:{status:'dataDeleted',userID:'test'},
    deleteUser:async()=>{if(failing) throw Error('private diagnostic');}};
  assert.deepEqual(await runWorker('deletion',client),{kind:'deletion',ok:false,completed:0,pending:1});
  failing=false;
  assert.deepEqual(await runWorker('deletion',client),{kind:'deletion',ok:true,completed:1,pending:0});
  assert.deepEqual(await runWorker('deletion',{rpc:async()=>[]}),{kind:'deletion',ok:true,completed:0,pending:0});
});

test('maintenance runs one bounded batch and exposes partial failure to the supervisor',async()=>{
  const summary={accountsMaintained:99,accountsFailed:1,operationsDeleted:2,checkpointsDeleted:3,receiptsDeleted:4};
  const client={rpc:async(name,body)=>{
    assert.equal(name,'maintain_sync_batch');assert.deepEqual(body,{p_limit:100});return summary;
  }};
  assert.deepEqual(await runWorker('maintenance',client),{kind:'maintenance',ok:false,...summary});
  summary.accountsFailed=0;
  assert.equal((await runWorker('maintenance',client)).ok,true);
  await assert.rejects(runWorker('unknown',client),/Unknown worker/);
});

test('fatal setup errors never print credentials, paths or transport messages',async()=>{
  let text='';const output={write:value=>{text+=value;}};
  assert.equal(await main('deletion',{SUPABASE_URL:'secret-invalid-url',SUPABASE_SERVICE_ROLE_KEY:'private-key'},output),1);
  assert.equal(text,'{"ok":false,"error":"workerFailed"}\n');
  text='';
  assert.equal(await main('private-argument',{},output),1);
  assert.equal(text,'{"ok":false,"error":"workerFailed"}\n');
});

test('systemd credentials take precedence and malformed credentials fail closed',async()=>{
  const directory=await mkdtemp(join(tmpdir(),'sync-worker-credentials-'));
  let text='';const output={write:value=>{text+=value;}};
  try {
    await writeFile(join(directory,'supabase.json'),'{private-invalid-json',{mode:0o600});
    assert.equal(await main('maintenance',{CREDENTIALS_DIRECTORY:directory,SUPABASE_URL:'https://unused.example',SUPABASE_SERVICE_ROLE_KEY:'unused'},output),1);
    assert.equal(text,'{"ok":false,"error":"workerFailed"}\n');
  } finally {await rm(directory,{recursive:true,force:true});}
});
