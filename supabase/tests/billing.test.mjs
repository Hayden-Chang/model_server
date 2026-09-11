import test, {before, after} from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {database} from './database.mjs';

let db;
before(async () => { db=await database(); });
after(async () => { await db?.close(); });

const TABLES = ['store_purchases','billing_claims','billing_events','account_entitlements'];

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
  const key='h'.repeat(64);
  const insert=(environment,provider,product)=>db.admin.query(
    `insert into billing_private.store_purchases
       (provider,environment,purchase_key_hash,store_reference_ciphertext,user_id,product_id,store_status)
     values($1,$2,$3,'cipher',$4,$5,'active')`,[provider,environment,key,a.id,product]);
  await insert('sandbox','apple','com.hayden.daymosaic.plus.monthly');
  await assert.rejects(insert('sandbox','apple','com.hayden.daymosaic.plus.monthly'),/duplicate key/);
  await insert('production','apple','com.hayden.daymosaic.plus.yearly');
  await insert('sandbox','google','com.hayden.daymosaic.plus.yearly');
  await db.admin.query('delete from auth.users where id=$1',[a.id]);
  const rows=(await db.admin.query(
    `select user_id from billing_private.store_purchases
      where purchase_key_hash=$1 and environment='sandbox' and provider='apple'`,[key])).rows;
  assert.equal(rows.length,1);
  assert.equal(rows[0].user_id,null);
});

test('claim defaults to pending, status is constrained, entitlement defaults follow the contract',async()=>{
  const a=await db.account();
  const claim=randomUUID();
  await db.admin.query(
    `insert into billing_private.billing_claims
       (claim_id,user_id,provider,product_id,expected_account_identifier_hash,request_hash)
     values($1,$2,'apple','com.hayden.daymosaic.plus.monthly',$3,$4)`,
    [claim,a.id,'e'.repeat(64),'r'.repeat(64)]);
  const rows=(await db.admin.query(
    'select status from billing_private.billing_claims where claim_id=$1',[claim])).rows;
  assert.equal(rows[0].status,'pending');
  await assert.rejects(db.admin.query(
    `update billing_private.billing_claims set status='weird' where claim_id=$1`,[claim]),/check constraint/);
  await assert.rejects(db.admin.query(
    `insert into billing_private.store_purchases
       (provider,environment,purchase_key_hash,store_reference_ciphertext,product_id,store_status,ack_attempts)
     values('apple','production',$1,'cipher','com.hayden.daymosaic.plus.monthly','active',-1)`,
    ['a'.repeat(64)]),/check constraint/);
  const ent=(await db.admin.query(
    'insert into billing_private.account_entitlements(user_id) values($1) returning *',[a.id])).rows[0];
  assert.equal(ent.plan,'free');
  assert.equal(ent.status,'expired');
  assert.equal(String(ent.entitlement_revision),'0');
  await assert.rejects(db.admin.query(
    `insert into billing_private.account_entitlements(user_id,plan) values($1,'gold')`,[a.id]),/check constraint/);
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
