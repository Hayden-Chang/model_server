import { before, after, test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import Ajv from 'ajv/dist/2020.js';
import { database } from './database.mjs';
import { withTask, canonical, hash, operation } from './fixtures.mjs';
import { randomUUID } from 'node:crypto';
let db, schema, valid;
before(async()=>{
  db=await database();
  schema=JSON.parse(await readFile(new URL('../protocol/v1/cloud-state.schema.json',import.meta.url),'utf8'));
  // Regex in the schema fixes wire format; PostgreSQL separately checks calendar validity.
  valid=new Ajv({strict:false,validateFormats:false}).compile(schema);
});
after(async()=>{ await db?.close(); });
const check=async state=>(await db.admin.query('select sync_private.validate_state($1) as state',[state])).rows[0].state;
test('generated SQL embeds the exact versioned contracts',()=>{
  execFileSync(process.execPath,['scripts/build-contract.mjs','--check']);
});
test('shared golden vectors have identical JavaScript and PostgreSQL canonical hashes',async()=>{
  const vectors=JSON.parse(await readFile(new URL('../protocol/v1/golden.json',import.meta.url),'utf8'));
  for(const v of vectors) {
    assert.equal(valid(v.state),true,JSON.stringify(valid.errors));
    assert.deepEqual(await check(v.state),v.state);
    const row=(await db.admin.query('select sync_private.canonical_json($1) as canonical,sync_private.hash_json($1) as hash',[v.state])).rows[0];
    assert.equal(row.canonical,v.canonical); assert.equal(canonical(v.state),v.canonical);
    assert.equal(row.hash,v.hash); assert.equal(hash(v.state),v.hash);
  }
});
test('array order does not change canonical cloud state',async()=>{
  const state=withTask(); state.tasks[0].repeatRule='weekly';state.tasks[0].repeatWeekdays=[5,1,3];
  const normalized=await check(state);
  assert.deepEqual(normalized.tasks[0].repeatWeekdays,[1,3,5]);
  state.tasks[0].repeatWeekdays.reverse();
  assert.deepEqual(await check(state),normalized);
});
test('invalid dates, recurrence, UUIDs and device-private fields are rejected',async()=>{
  const edits=[
    s=>s.occurrences[0].occurrenceDate='2026-02-30',
    s=>s.tasks[0].createdAt='2026-02-30T10:00:00.000Z',
    s=>s.tasks[0].repeatRule='weekly',
    s=>s.tasks[0].repeatForWeeks=3,
    s=>s.tasks[0].repeatWeekdays=[1],
    s=>{s.tasks[0].repeatRule='weekly';s.tasks[0].repeatWeekdays=[1,1];},
    s=>s.tasks[0].id='NOT-A-UUID',
    s=>s.tasks[0].note='a'.repeat(20001),
    s=>s.importedCalendarOnce=true,
    s=>s.tasks[0].sourceCalendarID='private',
    s=>s.occurrences[0].status='unknown',
    s=>s.tasks.push(structuredClone(s.tasks[0])),
    s=>s.occurrences[0].taskID=randomUUID(),
    s=>s.schemaVersion=2,
  ];
  for(const edit of edits){const state=withTask();edit(state);await assert.rejects(check(state),/payloadInvalid/);}
  const state=withTask();
  const text=JSON.stringify(state).replace('"schemaVersion":1','"schemaVersion":1.0');
  await assert.rejects(db.admin.query('select sync_private.validate_state($1::jsonb)',[text]),/payloadInvalid/);
});
test('within-task overlaps and inconsistent active focus are rejected, weak history references survive',async()=>{
  const vectors=JSON.parse(await readFile(new URL('../protocol/v1/golden.json',import.meta.url),'utf8'));
  const state=structuredClone(vectors[1].state);
  const conflicting=structuredClone(state);
  conflicting.scheduledTasks[0].segments[1].startSlot=0;
  await assert.rejects(check(conflicting),/payloadInvalid/);
  const active=structuredClone(state);active.activeFocusSessionID=null;
  await assert.rejects(check(active),/payloadInvalid/);
  const wrongParent=structuredClone(state);wrongParent.scheduledTasks[0].segments[0].scheduledTaskID=randomUUID();
  await assert.rejects(check(wrongParent),/payloadInvalid/);
  assert.deepEqual(await check(state),state);
});
test('all operation kinds have payload schemas and reject omitted payload fields',async()=>{
  const contract=JSON.parse(await readFile(new URL('../protocol/v1/operation.schema.json',import.meta.url),'utf8'));
  const ajv=new Ajv({strict:false,validateFormats:false}).compile(contract);
  const state=withTask();
  const op=operation({id:randomUUID()},{syncSpaceID:randomUUID(),generation:1,revision:1,stateHash:hash(state),state},state);
  assert.equal(ajv(op),true,JSON.stringify(ajv.errors));
  const sql=async value=>(await db.admin.query('select sync_private.matches_schema($1,$2,$2) as valid',[value,contract])).rows[0].valid;
  assert.equal(await sql(op),true);
  const examples=JSON.parse(await readFile(new URL('../protocol/v1/operation-examples.json',import.meta.url),'utf8'));
  assert.deepEqual(examples.map(x=>x.kind).sort(),[...contract.properties.kind.enum].sort());
  for(const example of examples) {
    assert.equal(ajv(example),true,example.kind+': '+JSON.stringify(ajv.errors));
    assert.equal(await sql(example),true,example.kind);
  }
  for(const kind of contract.properties.kind.enum){
    const invalid={...op,kind,payload:{}};
    assert.equal(ajv(invalid),false,kind);assert.equal(await sql(invalid),false,kind);
  }
  assert.equal(await sql({...op,kind:'task.toggle'}),false);
});
