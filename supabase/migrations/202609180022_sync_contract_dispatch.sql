-- Contract-version dispatch for the sync write path (route 2 of
-- docs-adjacent decision record G-v2-write-path-options.md).
--
-- Why. The iOS client writes only v2 (SyncOperationEnvelope.currentSchemaVersion
-- = 2, CloudState.toJSON(schemaVersion: 2)), but every write path read the v1
-- contract rows: validate_state selected name='cloud-state', validate_operation
-- and commit_sync_state selected name='operation'. The v2 rows registered by
-- 202609110009 and re-registered by 202609170018 were therefore never read, so a
-- v2 operation AND a v2 cloud projection were both rejected as payloadInvalid.
-- That broke the very first sync, because initialize_sync_state validates through
-- validate_state before it writes anything.
--
-- What this changes -- four functions, functions only:
--   1. sync_private.contract_for(name, version) is the single contract-selection
--      point. An unregistered version raises schemaTooNew. It deliberately never
--      falls back to v1: an unknown version is "the client is newer than this
--      server", not "the payload is corrupt", and the client branches on those
--      two errors differently.
--   2. validate_state and validate_operation dispatch on the payload's
--      schemaVersion, behind an integer-shape guard. A missing or non-integer
--      versionKey takes the existing payloadInvalid path instead of raising
--      PostgreSQL's invalid input syntax for type integer, whose unstable message
--      would leak to PostgREST. The roughly thirty lines of semantic validation
--      below the contract check (set deduplication, repeatRule/repeatWeekdays
--      agreement, strong references, segment overlap, active-focus consistency,
--      normalize_state, preconditions and readSet) are unchanged word for word;
--      they are contract-version independent.
--   3. commit_sync_state's inline name='operation' duplicate check now goes through
--      the same dispatcher, so there is one selection point instead of two.
--   4. initialize_sync_state writes the accepted payload's schemaVersion into
--      user_sync_state.schema_version, and gains a device-capability gate. Without
--      the label, a v2 first sync would store a v2-shaped payload under
--      schema_version=1 and the client could never commit again (the schemaTooNew
--      gate compares against s.schema_version). Without the gate, a device
--      registered with supported_schema_version=1 could initialize a v2 cloud
--      state and then be locked out of reading it by pull_sync_state's read
--      protection. The dispatch key is a shape selector, not an authorization:
--      authorization stays with s.schema_version and the device capability, so
--      all five pre-existing schemaTooNew gates are preserved untouched.
-- No contract row, table, column, or data is added, changed, or backfilled.
-- replace_sync_state is unchanged: its s.schema_version <> payload schemaVersion
-- gate already expresses that a whole-state overwrite may not change the version,
-- which is why an existing v1 account cannot upgrade through replace.
--
-- Why no down migration is shipped. Reverting dispatch would not roll back the v2
-- data already written: a v2 account could still pull (pull_sync_state does not
-- validate the state) but every commit would fail payloadInvalid, stranding it
-- read-only -- the same lockout that 202609170018's guard exists to avoid. The
-- recommended recovery is forward-fix plus restoring the four pre-change function
-- bodies from git, as 202609170017_device_principal_billing_rollback.sql's header
-- documents ("the repository already restores function bodies from git this way").
-- Record the pre-change SHA and re-apply the create or replace statements for
-- sync_private.validate_state, sync_private.validate_operation,
-- public.commit_sync_state and public.initialize_sync_state from
-- supabase/migrations/202609080002_validation.sql and
-- supabase/migrations/202609080003_sync.sql at that SHA. Shipping a checked-in
-- down script would add a second file whose version sorts after every forward
-- migration and must never be applied by db push, doubling an operational trap
-- the README already warns about for 202609170099.

-- The one contract-selection point. p_version is already shape-checked by the
-- callers below; this function only maps it onto the registered row name.
create function sync_private.contract_for(p_name text, p_version integer)
returns jsonb language plpgsql stable set search_path = '' as $$
declare c jsonb;
begin
  select schema into c from sync_private.contracts
    where name = case when p_version = 1 then p_name else p_name || '-v' || p_version end;
  if c is null then raise exception 'schemaTooNew'; end if;
  return c;
end $$;

-- Guard the dispatch key before casting it. Any payload whose schemaVersion is
-- absent, null, fractional, non-numeric, or negative is payloadInvalid -- never a
-- raw invalid input syntax for type integer. A missing key and a JSON null both
-- make ->' ... ' yield SQL NULL, and a bare !~ over NULL is NULL rather than true,
-- so the null test must be explicit; otherwise the null version reaches
-- contract_for and surfaces as schemaTooNew, misreporting a malformed payload as
-- a too-new client.
create function sync_private.require_schema_version(v jsonb)
returns integer language plpgsql immutable set search_path = '' as $$
declare version text;
begin
  if jsonb_typeof(v) <> 'object' then raise exception 'payloadInvalid'; end if;
  version := v->>'schemaVersion';
  if version is null or version !~ '^[1-9][0-9]{0,8}$' then raise exception 'payloadInvalid'; end if;
  return version::int;
end $$;

create or replace function sync_private.validate_state(v jsonb)
returns jsonb language plpgsql stable set search_path = '' as $$
declare contract jsonb; collection text; e jsonb; fields jsonb; active_ids jsonb; total integer := 0;
begin
  if v is null or octet_length(v::text) > 4194304 then raise exception 'payloadInvalid'; end if;
  contract := sync_private.contract_for('cloud-state', sync_private.require_schema_version(v));
  if not sync_private.matches_schema(v, contract, contract) then raise exception 'payloadInvalid'; end if;
  foreach collection in array array['tasks','occurrences','scheduledTasks','externalEvents','focusSessions'] loop
    total := total + jsonb_array_length(v->collection);
    if (select count(distinct x->>'id') from jsonb_array_elements(v->collection) x) <> jsonb_array_length(v->collection) then raise exception 'payloadInvalid'; end if;
  end loop;
  if total > 20000 then raise exception 'payloadInvalid'; end if;
  for fields in select x from jsonb_array_elements(v->'tasks') x union all select x->'overrides' from jsonb_array_elements(v->'occurrences') x where jsonb_typeof(x->'overrides') = 'object' loop
    if (fields->>'repeatRule' = 'weekly') <> (jsonb_array_length(fields->'repeatWeekdays') > 0) then raise exception 'payloadInvalid'; end if;
  end loop;
  if exists (select 1 from jsonb_array_elements(v->'occurrences') o where not exists (select 1 from jsonb_array_elements(v->'tasks') t where t->>'id'=o->>'taskID')) then raise exception 'payloadInvalid'; end if;
  if exists (select 1 from jsonb_array_elements(v->'scheduledTasks') s where not exists (select 1 from jsonb_array_elements(v->'occurrences') o where o->>'id'=s->>'taskOccurrenceID')) then raise exception 'payloadInvalid'; end if;
  if (select count(distinct s->>'taskOccurrenceID') from jsonb_array_elements(v->'scheduledTasks') s) <> jsonb_array_length(v->'scheduledTasks') then raise exception 'payloadInvalid'; end if;
  foreach collection in array array['scheduledTasks','externalEvents'] loop
    if (select count(*) from jsonb_array_elements(v->collection) parent, jsonb_array_elements(parent->'segments') seg) <>
      (select count(distinct seg->>'id') from jsonb_array_elements(v->collection) parent, jsonb_array_elements(parent->'segments') seg) then raise exception 'payloadInvalid'; end if;
    for e in select value from jsonb_array_elements(v->collection) loop
      if collection = 'externalEvents' and (e->>'sourceStartSlot')::int >= (e->>'sourceEndSlot')::int then raise exception 'payloadInvalid'; end if;
      if (select count(distinct s->>'id') from jsonb_array_elements(e->'segments') s) <> jsonb_array_length(e->'segments') then raise exception 'payloadInvalid'; end if;
      if exists (select 1 from jsonb_array_elements(e->'segments') s where s->>(case when collection='scheduledTasks' then 'scheduledTaskID' else 'externalEventID' end) <> e->>'id'
        or (s->>'endSlot')::int <= (s->>'startSlot')::int or (s->>'endSlot')::int - (s->>'startSlot')::int > 96) then raise exception 'payloadInvalid'; end if;
      if exists (select 1 from jsonb_array_elements(e->'segments') a, jsonb_array_elements(e->'segments') b
        where a->>'id' < b->>'id' and (a->>'startSlot')::int < (b->>'endSlot')::int and (b->>'startSlot')::int < (a->>'endSlot')::int) then raise exception 'payloadInvalid'; end if;
    end loop;
  end loop;
  select coalesce(jsonb_agg(s->'id'), '[]') into active_ids from jsonb_array_elements(v->'focusSessions') s where s->>'status' in ('running','paused');
  if jsonb_array_length(active_ids) > 1 or (jsonb_array_length(active_ids) = 0 and v->'activeFocusSessionID' <> 'null') or (jsonb_array_length(active_ids)=1 and v->'activeFocusSessionID' <> active_ids->0) then raise exception 'payloadInvalid'; end if;
  return sync_private.normalize_state(v);
end $$;

create or replace function sync_private.validate_operation(op jsonb, current_state jsonb, candidate jsonb)
returns void language plpgsql stable set search_path = '' as $$
declare contract jsonb; k text; expected jsonb; actual jsonb; collection text; entity_id text; state_entities jsonb := '{}';
begin
  contract := sync_private.contract_for('operation', sync_private.require_schema_version(op));
  if op is null or octet_length(op::text)>1048576 or not sync_private.matches_schema(op, contract, contract) then raise exception 'payloadInvalid'; end if;
  if op->>'clientResultStateHash' <> sync_private.hash_json(candidate) then raise exception 'stateHashMismatch'; end if;
  -- A caller cannot omit the before fingerprint for an entity it changes.
  -- Domain replay remains client-owned; this validates the complete write set.
  foreach collection in array array['tasks','occurrences','scheduledTasks','externalEvents','focusSessions'] loop
    state_entities := state_entities || (select coalesce(jsonb_object_agg(collection || '/' || (value->>'id'),value),'{}') from jsonb_array_elements(current_state->collection));
    for entity_id in select coalesce(a.entity->>'id',b.entity->>'id')
      from jsonb_array_elements(current_state->collection) a(entity)
      full join jsonb_array_elements(candidate->collection) b(entity) on a.entity->>'id'=b.entity->>'id'
      where a.entity is distinct from b.entity
    loop
      k := collection || '/' || entity_id;
      if not (op#>'{preconditions,entityFingerprints}') ? k then raise exception 'preconditionRequired'; end if;
    end loop;
  end loop;
  for k, expected in select * from jsonb_each(op#>'{preconditions,entityFingerprints}') loop
    collection := split_part(k, '/', 1);
    if collection not in ('tasks','occurrences','scheduledTasks','externalEvents','focusSessions') or k !~ '^[a-zA-Z]+/[0-9a-f-]{36}$' then raise exception 'payloadInvalid'; end if;
    actual := state_entities->k;
    if coalesce(to_jsonb(sync_private.hash_json(actual)), 'null') <> expected then raise exception 'preconditionFailed'; end if;
  end loop;
  if sync_private.hash_json(sync_private.read_set(current_state, op#>'{preconditions,readSet}')) <> op#>>'{preconditions,readSetFingerprint}' then raise exception 'preconditionFailed'; end if;
end $$;

create or replace function public.commit_sync_state(p_operation jsonb,p_result_state jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare uid uuid := sync_private.current_user_id(); d sync_private.devices; s sync_private.user_sync_state; saved sync_private.sync_operations;
  opid uuid; did uuid; v jsonb; h text; r jsonb; contract jsonb;
begin
  perform sync_private.lock_account(uid);
  contract := sync_private.contract_for('operation',sync_private.require_schema_version(p_operation));
  if p_operation is null or octet_length(p_operation::text)>1048576 or not sync_private.matches_schema(p_operation,contract,contract) then raise exception 'payloadInvalid'; end if;
  opid := (p_operation->>'operationID')::uuid; did := (p_operation->>'deviceID')::uuid;
  d := sync_private.require_device(uid,did);
  v := sync_private.validate_state(p_result_state);
  h := sync_private.hash_json(jsonb_build_object('operation',p_operation,'resultStateHash',sync_private.hash_json(v)));
  select * into s from sync_private.user_sync_state where user_id=uid;
  select * into saved from sync_private.sync_operations where user_id=uid and operation_id=opid;
  if found then
    if saved.request_hash<>h then raise exception 'operationIDReused'; end if;
    return jsonb_build_object('status','duplicate','resultRevision',saved.result_revision,'resultGeneration',saved.generation,
      'resultStateHash',saved.result_state_hash,'currentRevision',s.revision,'currentGeneration',s.generation);
  end if;
  if s.user_id is null then raise exception 'firstSyncRequired'; end if;
  if p_operation->>'syncSpaceID' <> s.sync_space_id::text or (p_operation->>'generation')::bigint<>s.generation or d.generation<>s.generation then raise exception 'generationConflict'; end if;
  if (p_operation->>'schemaVersion')::int<>s.schema_version or d.supported_schema_version<s.schema_version then raise exception 'schemaTooNew'; end if;
  if (p_operation->>'baseRevision')::bigint<>s.revision or p_operation->>'baseStateHash'<>s.state_hash then raise exception 'revisionConflict'; end if;
  perform sync_private.validate_operation(p_operation,s.state,v);
  if (select count(*) from sync_private.sync_operations where user_id=uid)>=50000 then raise exception 'resourceLimit'; end if;
  update sync_private.user_sync_state set state=v,state_hash=sync_private.hash_json(v),revision=revision+1,updated_at=clock_timestamp(),updated_by_device_id=did
    where user_id=uid returning * into s;
  insert into sync_private.sync_operations values(uid,opid,did,s.generation,s.revision,p_operation->>'kind',h,s.state_hash,clock_timestamp());
  return sync_private.state_result(s) || jsonb_build_object('status','accepted','resultRevision',s.revision);
end $$;

create or replace function public.initialize_sync_state(p_device_id uuid,p_initialization_id uuid,p_state jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare uid uuid := sync_private.current_user_id(); d sync_private.devices; s sync_private.user_sync_state; v jsonb; h text; saved sync_private.sync_control_requests; r jsonb;
begin
  perform sync_private.lock_account(uid); d := sync_private.require_device(uid,p_device_id);
  if p_initialization_id is null then raise exception 'payloadInvalid'; end if;
  v := sync_private.validate_state(p_state);
  -- A device may not create a cloud state it cannot read back.
  if (v->>'schemaVersion')::int > d.supported_schema_version then raise exception 'schemaTooNew'; end if;
  h := sync_private.hash_json(jsonb_build_object('kind','initialize','deviceID',p_device_id,'state',v));
  select * into saved from sync_private.sync_control_requests where user_id=uid and request_id=p_initialization_id;
  if found then
    if saved.request_hash<>h then raise exception 'requestIDReused'; end if;
    return saved.result || '{"status":"duplicate"}';
  end if;
  if exists(select 1 from sync_private.user_sync_state where user_id=uid) then raise exception 'firstSyncRequired'; end if;
  insert into sync_private.user_sync_state(user_id,state,state_hash,schema_version,updated_by_device_id,revision)
    values(uid,v,sync_private.hash_json(v),(v->>'schemaVersion')::int,p_device_id,case when v @> '{"tasks":[],"occurrences":[],"scheduledTasks":[],"externalEvents":[],"focusSessions":[]}' and
    jsonb_array_length(v->'tasks')+jsonb_array_length(v->'occurrences')+jsonb_array_length(v->'scheduledTasks')+jsonb_array_length(v->'externalEvents')+jsonb_array_length(v->'focusSessions')=0 then 0 else 1 end) returning * into s;
  update sync_private.devices set generation=s.generation,last_ack_revision=s.revision where user_id=uid and device_id=p_device_id;
  r := sync_private.state_result(s) || '{"status":"accepted"}';
  insert into sync_private.sync_control_requests values(uid,p_initialization_id,'initialize',h,r,clock_timestamp());
  return r;
end $$;

-- create or replace preserves the existing owner and ACL, but replay the grants
-- explicitly so this migration is self-contained, matching 202609170017/019/020.
revoke all on function sync_private.contract_for(text,integer) from public,anon,authenticated,service_role;
revoke all on function sync_private.require_schema_version(jsonb) from public,anon,authenticated,service_role;
revoke all on function sync_private.validate_state(jsonb) from public,anon,authenticated,service_role;
revoke all on function sync_private.validate_operation(jsonb,jsonb,jsonb) from public,anon,authenticated,service_role;
do $$ declare f regprocedure; begin
  for f in select oid::regprocedure from pg_proc where pronamespace='public'::regnamespace
    and proname in ('initialize_sync_state','commit_sync_state')
  loop
    execute format('revoke all on function %s from public,anon,authenticated,service_role',f);
    execute format('grant execute on function %s to authenticated',f);
  end loop;
end $$;
