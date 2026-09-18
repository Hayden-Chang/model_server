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
  // The manual reverse of 202609170017 also embeds generated SQL (the pre-017
  // function bodies and the pre-018 contract payloads); it must not drift either.
  execFileSync(process.execPath,['scripts/build-rollback-functions.mjs','--check']);
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
test('generated v2 contracts are registered and v2 golden vectors match JavaScript and PostgreSQL hashes',async()=>{
  const schema=JSON.parse(await readFile(new URL('../protocol/v2/cloud-state.schema.json',import.meta.url),'utf8'));
  const valid=new Ajv({strict:false,validateFormats:false}).compile(schema);
  const contract=(await db.admin.query("select schema from sync_private.contracts where name='cloud-state-v2'")).rows[0].schema;
  const vectors=JSON.parse(await readFile(new URL('../protocol/v2/golden.json',import.meta.url),'utf8'));
  for(const v of vectors) {
    assert.equal(valid(v.state),true,v.name+': '+JSON.stringify(valid.errors));
    assert.equal((await db.admin.query('select sync_private.matches_schema($1,$2,$2) as valid',[v.state,contract])).rows[0].valid,true,v.name);
    assert.equal(canonical(v.state),v.canonical);
    const row=(await db.admin.query('select sync_private.canonical_json($1) as canonical,sync_private.hash_json($1) as hash',[v.state])).rows[0];
    assert.equal(row.canonical,v.canonical); assert.equal(row.hash,v.hash); assert.equal(hash(v.state),v.hash);
  }
});
test('v2 state schema rejects missing or invalid v2 fields and the write path dispatches on schemaVersion',async()=>{
  const schema=JSON.parse(await readFile(new URL('../protocol/v2/cloud-state.schema.json',import.meta.url),'utf8'));
  const valid=new Ajv({strict:false,validateFormats:false}).compile(schema);
  const vectors=JSON.parse(await readFile(new URL('../protocol/v2/golden.json',import.meta.url),'utf8'));
  const base=structuredClone(vectors[1].state);
  // The vectors dropped puzzle, so the legacy edits below re-attach a valid object first.
  const legacy=()=>{const s=structuredClone(base);s.puzzle={fragments:7,rewardedTaskIDs:[s.tasks[0].id],
    unlockedArtworkIDs:['artwork-2026-001'],dayArtworkIDs:{'2026-09-08':'artwork-2026-001'},
    completedTaskIDs:[s.tasks[0].id],works:[],celebratedDays:[],dayFinishState:null,automaticRandom:false};return s;};
  const edits=[
    [()=>structuredClone(base),s=>s.schemaVersion=1],
    [legacy,s=>s.puzzle.extraField=true],
    [()=>structuredClone(base),s=>delete s.tasks[0].startReminder],
    [()=>structuredClone(base),s=>s.tasks[0].startReminder='3'],
    [()=>structuredClone(base),s=>s.tasks[0].customColorHex='#12AB3'],
    [()=>structuredClone(base),s=>delete s.occurrences[1].overrides.startReminder],
    [()=>structuredClone(base),s=>s.occurrences[1].overrides.customColorHex='#GGHHII'],
    [legacy,s=>s.puzzle.fragments=-1],
    [legacy,s=>s.puzzle.automaticRandom=null],
    [legacy,s=>s.puzzle.dayArtworkIDs['2026-09-08']=null],
  ];
  for(const [source,edit] of edits){const state=source();edit(state);assert.equal(valid(state),false,edit.toString());}
  // Contract dispatch (202609180022) selects the row from the payload's own
  // schemaVersion, so the v2 golden vector now validates through the write path.
  assert.deepEqual(await check(base),base);
  // An unregistered version is schemaTooNew -- never a v1 fallback, and never
  // payloadInvalid, which the client would report as data corruption.
  const tooNew=structuredClone(base);tooNew.schemaVersion=3;
  await assert.rejects(check(tooNew),/schemaTooNew/);
  // A v1 payload labelled 2 (and a v2 payload labelled 1) picks the other row and
  // fails its shape check, so a mislabelled payload cannot pass as the other version.
  const labelled2=withTask();labelled2.schemaVersion=2;
  await assert.rejects(check(labelled2),/payloadInvalid/);
  const labelled1=structuredClone(base);labelled1.schemaVersion=1;
  await assert.rejects(check(labelled1),/payloadInvalid/);
  // A missing, null, fractional, or non-numeric dispatch key takes the existing
  // payloadInvalid path rather than a raw integer-cast error.
  for(const bad of [undefined,null,1.5,'2']){
    const state=structuredClone(base);
    if(bad===undefined)delete state.schemaVersion;else state.schemaVersion=bad;
    await assert.rejects(check(state),/payloadInvalid/);
  }
});
test('v2 cloud-state accepts a state without puzzle, keeps accepting the legacy puzzle object, and the registered row no longer requires it',async()=>{
  const schema=JSON.parse(await readFile(new URL('../protocol/v2/cloud-state.schema.json',import.meta.url),'utf8'));
  const valid=new Ajv({strict:false,validateFormats:false}).compile(schema);
  const contract=(await db.admin.query("select schema from sync_private.contracts where name='cloud-state-v2'")).rows[0].schema;
  const sql=async value=>(await db.admin.query('select sync_private.matches_schema($1,$2,$2) as valid',[value,contract])).rows[0].valid;
  const vectors=JSON.parse(await readFile(new URL('../protocol/v2/golden.json',import.meta.url),'utf8'));
  assert.equal(contract.required.includes('puzzle'),false);
  for(const state of [vectors[0].state,vectors[1].state]) {
    assert.equal('puzzle' in state,false,'vector must not carry puzzle');
    assert.equal(valid(state),true,JSON.stringify(valid.errors));
    assert.equal(await sql(state),true);
  }
  const legacy=structuredClone(vectors[1].state);
  legacy.puzzle={fragments:7,rewardedTaskIDs:[legacy.tasks[0].id],unlockedArtworkIDs:['artwork-2026-001'],
    dayArtworkIDs:{'2026-09-08':'artwork-2026-001'},completedTaskIDs:[legacy.tasks[0].id],works:[],celebratedDays:[],
    dayFinishState:null,automaticRandom:false};
  assert.equal(valid(legacy),true,JSON.stringify(valid.errors));
  assert.equal(await sql(legacy),true);
});
test('v2 operation contract keeps all v1 kinds, adds puzzle.applyChanges, and rejects blind puzzle replacement',async()=>{
  const contract=JSON.parse(await readFile(new URL('../protocol/v2/operation.schema.json',import.meta.url),'utf8'));
  const ajv=new Ajv({strict:false,validateFormats:false}).compile(contract);
  assert.equal(contract.properties.kind.enum.length,21);
  const examples=JSON.parse(await readFile(new URL('../protocol/v2/operation-examples.json',import.meta.url),'utf8'));
  const contractRow=(await db.admin.query("select schema from sync_private.contracts where name='operation-v2'")).rows[0].schema;
  const sql=async value=>(await db.admin.query('select sync_private.matches_schema($1,$2,$2) as valid',[value,contractRow])).rows[0].valid;
  assert.deepEqual(examples.map(x=>x.kind).sort(),[...contract.properties.kind.enum].sort());
  for(const example of examples) {
    assert.equal(ajv(example),true,example.kind+': '+JSON.stringify(ajv.errors));
    assert.equal(await sql(example),true,example.kind);
  }
  const puzzle=examples.find(x=>x.kind==='puzzle.applyChanges');
  const invalids=[
    {...puzzle,payload:{}},
    {...puzzle,payload:{puzzle:{}}},
    {...puzzle,payload:{puzzle:{fragments:-1}}},
    {...puzzle,payload:{puzzle:{fragments:1,completePuzzle:{}}}},
    {...puzzle,schemaVersion:1},
    {...puzzle,kind:'puzzle.replace'},
  ];
  for(const invalid of invalids){assert.equal(ajv(invalid),false);assert.equal(await sql(invalid),false);}
});
test('validate_operation dispatches on schemaVersion and rejects a mislabelled envelope',async()=>{
  // Today the v2 operation examples only reach matches_schema with the contract row
  // passed by hand; this drives the real dispatch entry point instead.
  const examples=JSON.parse(await readFile(new URL('../protocol/v2/operation-examples.json',import.meta.url),'utf8'));
  const shape=async value=>(await db.admin.query(
    "select sync_private.matches_schema($1,sync_private.contract_for('operation',sync_private.require_schema_version($1)),"+
    "sync_private.contract_for('operation',sync_private.require_schema_version($1))) as valid",[value])).rows[0].valid;
  for(const example of examples){
    assert.equal(await shape(example),true,example.kind);
  }
  // Most v2 envelopes are shape-identical to v1 apart from schemaVersion, so
  // relabelling them legitimately selects the v1 row. The ones that carry v2-only
  // task DTO fields must not survive that relabelling.
  const v1contract=JSON.parse(await readFile(new URL('../protocol/v1/operation.schema.json',import.meta.url),'utf8'));
  const v1shape=async value=>(await db.admin.query('select sync_private.matches_schema($1,$2,$2) as valid',[value,v1contract])).rows[0].valid;
  const v2only=['task.create','schedule.createAndPlace'];
  for(const kind of v2only){
    const example=examples.find(x=>x.kind===kind);
    assert.equal(await v1shape({...example,schemaVersion:1}),false,kind);
  }
  // puzzle.applyChanges is v2-only and unknown to the v1 enum.
  const puzzle=examples.find(x=>x.kind==='puzzle.applyChanges');
  assert.equal(await v1shape({...puzzle,schemaVersion:1}),false,'puzzle.applyChanges');
  // An unregistered version is schemaTooNew; a missing or malformed key is payloadInvalid.
  await assert.rejects(db.admin.query("select sync_private.contract_for('operation',3)"),/schemaTooNew/);
  await assert.rejects(db.admin.query("select sync_private.contract_for('operation',0)"),/schemaTooNew/);
  await assert.rejects(db.admin.query('select sync_private.contract_for($1,1)',['nope']),/schemaTooNew/);
  for(const bad of [undefined,null,1.5,0,-1,'two']) {
    const op={...examples[0]};
    if(bad===undefined)delete op.schemaVersion;else op.schemaVersion=bad;
    await assert.rejects(db.admin.query('select sync_private.require_schema_version($1)',[op]),/payloadInvalid/,String(bad));
  }
  // A JSON string "2" is textually a valid integer to the guard, but the contract
  // row's {"const":2} does not equal the jsonb string, so it is still payloadInvalid.
  const asString={...examples[0],schemaVersion:'2'};
  assert.equal(await shape(asString),false,'string schemaVersion must not match the v2 row');
});
