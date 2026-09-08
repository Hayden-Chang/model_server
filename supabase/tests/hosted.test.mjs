import { test } from 'node:test';
import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { operation, withTask } from './fixtures.mjs';
import { processDeletions, transport } from '../scripts/deletion-worker.mjs';

function realtime(url, key, session) {
  const endpoint=new URL('/realtime/v1/websocket',url);
  endpoint.protocol=endpoint.protocol==='https:'?'wss:':'ws:';
  endpoint.search=new URLSearchParams({apikey:key,vsn:'2.0.0'}).toString();
  const socket=new WebSocket(endpoint),messages=[];
  const topic='realtime:sync-test-'+randomUUID();
  socket.addEventListener('message',event=>messages.push(JSON.parse(event.data)));
  socket.addEventListener('open',()=>socket.send(JSON.stringify(['1','1',topic,'phx_join',{
    config:{postgres_changes:[{event:'UPDATE',schema:'public',table:'sync_changes',filter:'user_id=eq.'+session.user.id}]},
    access_token:session.access_token,
  }])));
  function waitFor(predicate,label) {
    return new Promise((resolve,reject)=>{
      function finish(error,value) {
        clearTimeout(timer);socket.removeEventListener('message',check);
        socket.removeEventListener('error',failed);socket.removeEventListener('close',failed);
        error?reject(error):resolve(value);
      }
      const failed=()=>finish(new Error('Realtime connection failed: '+label));
      const check=()=>{
        const found=messages.find(predicate);
        if(found) finish(null,found[4]);
        else if(messages.some(m=>m[3]==='phx_error' || m[4]?.status==='error')) failed();
      };
      const timer=setTimeout(()=>finish(new Error('Realtime timed out: '+label)),15000);
      socket.addEventListener('message',check);socket.addEventListener('error',failed);socket.addEventListener('close',failed);
      check();
    });
  }
  return {
    ready:()=>waitFor(m=>m[3]==='system' && m[4]?.extension==='postgres_changes' && m[4]?.status==='ok','subscription'),
    revision:value=>waitFor(m=>m[3]==='postgres_changes' && m[4]?.data?.record?.revision===value,'revision notification'),
    close:()=>socket.close(),
  };
}

// Explicitly opt in: this creates and deletes fresh test Auth users on the selected
// local/staging project. Admin-generated codes do not send real email.
test('real Supabase OTP, two-device sync, Realtime, isolation and deletion', {
  skip:process.env.SYNC_TEST_ALLOW_WRITE !== '1',
}, async () => {
  const url=process.env.SUPABASE_URL,key=process.env.SUPABASE_PUBLISHABLE_KEY,admin=process.env.SUPABASE_SERVICE_ROLE_KEY;
  assert.ok(url && key && admin,'Set the selected test project environment');
  const created=new Set();
  let notifications;
  async function request(path,body,bearer=admin,apiKey=admin,method='POST') {
    const response=await fetch(new URL(path,url),{
      method,headers:{apikey:apiKey,Authorization:'Bearer '+bearer,'Content-Type':'application/json'},
      body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(15000),
    });
    const value=await response.json().catch(()=>null);
    return {status:response.status,ok:response.ok,value};
  }
  async function login(email='daymosaic-sync-'+randomUUID()+'@example.com') {
    const generated=await request('/auth/v1/admin/generate_link',{type:'magiclink',email});
    assert.ok(generated.ok,'Admin test-code generation must succeed');
    const id=generated.value.user?.id ?? generated.value.id;
    assert.ok(id,'Admin response must identify the generated user');
    created.add(id);
    const otp=generated.value.properties?.email_otp ?? generated.value.email_otp;
    assert.match(otp,/^[0-9]{6}$/,'Configured OTP must have six digits');
    const verified=await request('/auth/v1/verify',{email,token:otp,type:'email'},key,key);
    assert.ok(verified.ok && verified.value.access_token,'Real OTP verification must establish a session');
    assert.equal(verified.value.user.id,id);
    return verified.value;
  }
  const rpc=(session,name,body)=>request('/rest/v1/rpc/'+name,body,session.access_token,key);
  try {
    const first=await login(),second=await login(),replica=await login(first.user.email);
    const a=randomUUID(),b=randomUUID(),c=randomUUID(),initial=withTask('Hosted sync test');
    for(const [session,id] of [[first,a],[second,b],[replica,c]]) {
      const registration=await rpc(session,'register_sync_device',{p_device_id:id,p_platform:'ios',p_app_version:'integration-test',p_supported_schema_version:1});
      assert.equal(registration.value?.status,'registered');
    }
    const init=await rpc(first,'initialize_sync_state',{p_device_id:a,p_initialization_id:randomUUID(),p_state:initial});
    assert.equal(init.value?.status,'accepted');
    assert.equal((await rpc(second,'pull_sync_state',{p_device_id:a})).ok,false);
    const signals=await request('/rest/v1/sync_changes?select=*',undefined,second.access_token,key,'GET');
    assert.equal(signals.ok,true);assert.deepEqual(signals.value,[]);
    const refreshed=await request('/auth/v1/token?grant_type=refresh_token',{refresh_token:first.refresh_token},key,key);
    assert.ok(refreshed.ok && refreshed.value.access_token,'Refresh must succeed');
    const pulled=await rpc(refreshed.value,'pull_sync_state',{p_device_id:a});
    assert.equal(pulled.value?.syncSpaceID,init.value.syncSpaceID);
    assert.deepEqual(pulled.value.state,initial);
    const forged=await request('/rest/v1/rpc/sync_account_status',{},'not-a-jwt',key);
    assert.equal(forged.ok,false);

    const replicaState=await rpc(replica,'pull_sync_state',{p_device_id:c});
    assert.deepEqual(replicaState.value.state,initial);
    assert.equal((await rpc(replica,'acknowledge_sync_state',{p_device_id:c,p_generation:replicaState.value.generation,
      p_revision:replicaState.value.revision,p_state_hash:replicaState.value.stateHash})).ok,true);
    notifications=realtime(url,key,replica);
    await notifications.ready();
    const updated=structuredClone(initial);
    updated.occurrences[0].status='completed';updated.occurrences[0].completedAt='2026-09-08T10:00:00.000Z';
    const op=operation({id:a},pulled.value,updated);
    const accepted=await rpc(refreshed.value,'commit_sync_state',{p_operation:op,p_result_state:updated});
    assert.equal(accepted.value?.status,'accepted');
    const signal=await notifications.revision(accepted.value.revision);
    assert.deepEqual(Object.keys(signal.data.record).sort(),['generation','revision','sync_space_id','updated_at','user_id']);
    assert.deepEqual((await rpc(replica,'pull_sync_state',{p_device_id:c})).value.state,updated);
    assert.equal((await rpc(refreshed.value,'commit_sync_state',{p_operation:op,p_result_state:updated})).value?.status,'duplicate');
    const stale=await rpc(replica,'commit_sync_state',{p_operation:{...op,deviceID:c,operationID:randomUUID()},p_result_state:updated});
    assert.equal(stale.value?.message,'revisionConflict');
    notifications.close();notifications=undefined;

    const requestID=randomUUID(),receipt=randomBytes(32).toString('hex');
    const deletion=await rpc(refreshed.value,'request_account_deletion',{p_deletion_request_id:requestID,p_receipt:receipt});
    assert.equal(deletion.value?.status,'pending');
    assert.equal((await rpc(replica,'pull_sync_state',{p_device_id:c})).value?.message,'deletionPending');
    const client=transport(url,admin);
    const result=await processDeletions({deleteUser:client.deleteUser,async rpc(name,body) {
      const value=await client.rpc(name,body);
      return name==='pending_account_deletions'?value.filter(job=>job.requestID===requestID):value;
    }});
    assert.deepEqual(result,{completed:1,pending:0});
    const status=await request('/rest/v1/rpc/account_deletion_status',{p_receipt:receipt},key,key);
    assert.equal(status.value?.status,'completed');
    assert.equal((await rpc(replica,'sync_account_status',{})).ok,false);
    assert.equal((await request('/auth/v1/admin/users/'+first.user.id,undefined,admin,admin,'GET')).status,404);
  } finally {
    notifications?.close();
    const failures=[];
    for(const id of created) {
      const deleted=await request('/auth/v1/admin/users/'+encodeURIComponent(id),undefined,admin,admin,'DELETE');
      if(!deleted.ok && deleted.status!==404) failures.push(deleted.status);
    }
    assert.equal(failures.length,0,'Temporary Auth users must be cleaned up');
  }
});
