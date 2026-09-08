create table sync_private.accounts (
  user_id uuid primary key references auth.users(id) on delete cascade,
  deletion_pending boolean not null default false,
  window_start timestamptz not null default clock_timestamp(),
  request_count integer not null default 0
);
create table sync_private.devices (
  user_id uuid references sync_private.accounts(user_id) on delete cascade,
  device_id uuid not null,
  session_id uuid not null unique,
  platform text not null check (platform in ('ios','android')),
  app_version text not null check (length(app_version) between 1 and 64),
  supported_schema_version integer not null check (supported_schema_version >= 1),
  generation bigint not null default 0,
  last_ack_revision bigint not null default 0,
  bound_at timestamptz not null default clock_timestamp(),
  last_seen_at timestamptz not null default clock_timestamp(),
  lease_expires_at timestamptz not null default clock_timestamp() + interval '90 days',
  revoked_at timestamptz,
  primary key (user_id,device_id)
);
create table sync_private.user_sync_state (
  user_id uuid primary key references sync_private.accounts(user_id) on delete cascade,
  sync_space_id uuid not null unique default gen_random_uuid(),
  generation bigint not null default 1 check (generation between 1 and 9007199254740991),
  revision bigint not null check (revision between 0 and 9007199254740991),
  schema_version integer not null default 1,
  state jsonb not null,
  state_hash text not null,
  updated_at timestamptz not null default clock_timestamp(),
  updated_by_device_id uuid not null
);
create table sync_private.sync_operations (
  user_id uuid references sync_private.accounts(user_id) on delete cascade,
  operation_id uuid not null,
  device_id uuid not null,
  generation bigint not null,
  result_revision bigint not null,
  kind text not null,
  request_hash text not null,
  result_state_hash text not null,
  accepted_at timestamptz not null default clock_timestamp(),
  primary key (user_id,operation_id)
);
create table sync_private.sync_control_requests (
  user_id uuid references sync_private.accounts(user_id) on delete cascade,
  request_id uuid not null,
  kind text not null,
  request_hash text not null,
  result jsonb not null,
  created_at timestamptz not null default clock_timestamp(),
  primary key (user_id,request_id)
);
create table sync_private.state_checkpoints (
  checkpoint_id uuid primary key default gen_random_uuid(),
  user_id uuid not null references sync_private.accounts(user_id) on delete cascade,
  kind text not null default 'protocolSafety' check (kind='protocolSafety'),
  sync_space_id uuid not null,
  generation bigint not null,
  revision bigint not null,
  state_hash text not null,
  cloud_state jsonb not null,
  reason text not null,
  source_control_request_id uuid not null,
  required_ack_device_ids uuid[] not null,
  release_generation bigint not null,
  release_revision bigint not null,
  size_bytes integer not null,
  created_at timestamptz not null default clock_timestamp(),
  delete_after timestamptz not null default clock_timestamp() + interval '7 days',
  cleanup_status text not null default 'retained' check (cleanup_status='retained'),
  foreign key (user_id,source_control_request_id) references sync_private.sync_control_requests(user_id,request_id) deferrable initially deferred
);
-- Only this content-free signal table is published to Realtime.
create table public.sync_changes (
  user_id uuid primary key references sync_private.accounts(user_id) on delete cascade,
  sync_space_id uuid not null,
  generation bigint not null,
  revision bigint not null,
  updated_at timestamptz not null
);

do $$ declare t text; begin
  foreach t in array array['accounts','devices','user_sync_state','sync_operations','sync_control_requests','state_checkpoints'] loop
    execute format('alter table sync_private.%I enable row level security',t);
    execute format('revoke all on sync_private.%I from public, anon, authenticated, service_role',t);
  end loop;
end $$;
alter table public.sync_changes enable row level security;
revoke all on public.sync_changes from public, anon, authenticated, service_role;

create function sync_private.current_user_id()
returns uuid language plpgsql stable set search_path = '' as $$
declare uid uuid := auth.uid(); sid uuid := (auth.jwt()->>'session_id')::uuid;
begin
  if uid is null or sid is null or not exists (
    select 1 from auth.users u join auth.sessions s on s.user_id=u.id
    where u.id=uid and s.id=sid and u.email_confirmed_at is not null and not coalesce(u.is_anonymous,false)
  ) then raise exception 'authRequired'; end if;
  return uid;
end $$;

create function sync_private.lock_account(uid uuid)
returns void language plpgsql set search_path = '' as $$
declare a sync_private.accounts;
begin
  insert into sync_private.accounts(user_id) values(uid) on conflict do nothing;
  select * into a from sync_private.accounts where user_id=uid for update;
  if a.deletion_pending then raise exception 'deletionPending'; end if;
  if a.window_start < clock_timestamp() - interval '1 minute' then
    update sync_private.accounts set window_start=clock_timestamp(),request_count=1 where user_id=uid;
  elsif a.request_count >= 120 then raise exception 'rateLimited';
  else update sync_private.accounts set request_count=request_count+1 where user_id=uid;
  end if;
end $$;

create function sync_private.require_device(uid uuid, did uuid)
returns sync_private.devices language plpgsql set search_path = '' as $$
declare d sync_private.devices;
begin
  select * into d from sync_private.devices where user_id=uid and device_id=did
    and session_id=(auth.jwt()->>'session_id')::uuid and revoked_at is null and lease_expires_at>clock_timestamp();
  if not found then raise exception 'deviceRequired'; end if;
  update sync_private.devices set last_seen_at=clock_timestamp(),lease_expires_at=clock_timestamp()+interval '90 days'
    where user_id=uid and device_id=did;
  return d;
end $$;

create function sync_private.state_result(s sync_private.user_sync_state)
returns jsonb language sql immutable set search_path = '' as $$
  select jsonb_build_object('syncSpaceID',s.sync_space_id,'generation',s.generation,'revision',s.revision,'schemaVersion',s.schema_version,'stateHash',s.state_hash)
$$;
create function sync_private.publish_state()
returns trigger language plpgsql set search_path = '' as $$
begin
  insert into public.sync_changes values(new.user_id,new.sync_space_id,new.generation,new.revision,new.updated_at)
  on conflict(user_id) do update set sync_space_id=excluded.sync_space_id,generation=excluded.generation,revision=excluded.revision,updated_at=excluded.updated_at;
  return new;
end $$;
create trigger sync_state_changed after insert or update on sync_private.user_sync_state for each row execute function sync_private.publish_state();

create function public.sync_access_allowed()
returns boolean language plpgsql stable security definer set search_path = '' as $$
declare uid uuid;
begin
  uid := sync_private.current_user_id();
  return exists(select 1 from sync_private.devices d join sync_private.accounts a using(user_id)
    where d.user_id=uid and d.session_id=(auth.jwt()->>'session_id')::uuid and d.revoked_at is null
    and d.lease_expires_at>statement_timestamp() and not a.deletion_pending);
exception when others then return false;
end $$;
create policy sync_signal_own_active_session on public.sync_changes for select to authenticated
  using (user_id=(select auth.uid()) and (select public.sync_access_allowed()));
grant select on public.sync_changes to authenticated;
do $$ begin
  if exists(select 1 from pg_publication where pubname='supabase_realtime') then
    alter publication supabase_realtime add table public.sync_changes;
  end if;
end $$;

create function public.sync_account_status()
returns jsonb language plpgsql security definer set search_path = '' as $$
declare uid uuid := sync_private.current_user_id(); result jsonb;
begin
  if exists(select 1 from sync_private.accounts where user_id=uid and deletion_pending) then return '{"status":"deletionPending"}'; end if;
  select sync_private.state_result(s) || jsonb_build_object('status','ready','hasCloudData',revision>0) into result
    from sync_private.user_sync_state s where user_id=uid;
  return coalesce(result,'{"status":"uninitialized","hasCloudData":false}'::jsonb);
end $$;

create function public.register_sync_device(p_device_id uuid,p_platform text,p_app_version text,p_supported_schema_version integer)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare uid uuid := sync_private.current_user_id(); sid uuid := (auth.jwt()->>'session_id')::uuid; d sync_private.devices;
begin
  perform sync_private.lock_account(uid);
  if p_device_id is null or p_platform not in ('ios','android') or p_platform is null or p_app_version is null
    or length(p_app_version) not between 1 and 64 or p_supported_schema_version is null or p_supported_schema_version<1 then raise exception 'payloadInvalid'; end if;
  if exists(select 1 from sync_private.devices where session_id=sid and device_id<>p_device_id) then raise exception 'sessionAlreadyBound'; end if;
  select * into d from sync_private.devices where user_id=uid and device_id=p_device_id;
  if d.device_id is null or d.revoked_at is not null or d.lease_expires_at<=clock_timestamp() then
    if (select count(*) from sync_private.devices where user_id=uid and revoked_at is null and lease_expires_at>clock_timestamp())>=5 then raise exception 'deviceLimitReached'; end if;
  end if;
  insert into sync_private.devices(user_id,device_id,session_id,platform,app_version,supported_schema_version)
  values(uid,p_device_id,sid,p_platform,p_app_version,p_supported_schema_version)
  on conflict(user_id,device_id) do update set
    session_id=excluded.session_id,platform=excluded.platform,app_version=excluded.app_version,supported_schema_version=excluded.supported_schema_version,
    generation=case when sync_private.devices.session_id=sid and sync_private.devices.revoked_at is null and sync_private.devices.lease_expires_at>clock_timestamp() then sync_private.devices.generation else 0 end,
    last_ack_revision=case when sync_private.devices.session_id=sid and sync_private.devices.revoked_at is null and sync_private.devices.lease_expires_at>clock_timestamp() then sync_private.devices.last_ack_revision else 0 end,
    bound_at=case when sync_private.devices.session_id=sid and sync_private.devices.revoked_at is null and sync_private.devices.lease_expires_at>clock_timestamp() then sync_private.devices.bound_at else clock_timestamp() end,
    revoked_at=null,last_seen_at=clock_timestamp(),lease_expires_at=clock_timestamp()+interval '90 days';
  return jsonb_build_object('deviceID',p_device_id,'status','registered');
end $$;

create function public.list_sync_devices()
returns jsonb language plpgsql security definer set search_path = '' as $$
declare uid uuid := sync_private.current_user_id();
begin
  if exists(select 1 from sync_private.accounts where user_id=uid and deletion_pending) then raise exception 'deletionPending'; end if;
  return (select coalesce(jsonb_agg(jsonb_build_object('deviceID',device_id,'platform',platform,'appVersion',app_version,
    'lastSeenAt',last_seen_at,'leaseExpiresAt',lease_expires_at,'revokedAt',revoked_at)), '[]') from sync_private.devices where user_id=uid);
end $$;
create function public.revoke_sync_device(p_device_id uuid)
returns void language plpgsql security definer set search_path = '' as $$
declare uid uuid := sync_private.current_user_id();
begin
  perform sync_private.lock_account(uid);
  update sync_private.devices set revoked_at=clock_timestamp() where user_id=uid and device_id=p_device_id;
end $$;

create function public.initialize_sync_state(p_device_id uuid,p_initialization_id uuid,p_state jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare uid uuid := sync_private.current_user_id(); s sync_private.user_sync_state; v jsonb; h text; saved sync_private.sync_control_requests; r jsonb;
begin
  perform sync_private.lock_account(uid); perform sync_private.require_device(uid,p_device_id);
  if p_initialization_id is null then raise exception 'payloadInvalid'; end if;
  v := sync_private.validate_state(p_state);
  h := sync_private.hash_json(jsonb_build_object('kind','initialize','deviceID',p_device_id,'state',v));
  select * into saved from sync_private.sync_control_requests where user_id=uid and request_id=p_initialization_id;
  if found then
    if saved.request_hash<>h then raise exception 'requestIDReused'; end if;
    return saved.result || '{"status":"duplicate"}';
  end if;
  if exists(select 1 from sync_private.user_sync_state where user_id=uid) then raise exception 'firstSyncRequired'; end if;
  insert into sync_private.user_sync_state(user_id,state,state_hash,updated_by_device_id,revision)
    values(uid,v,sync_private.hash_json(v),p_device_id,case when v @> '{"tasks":[],"occurrences":[],"scheduledTasks":[],"externalEvents":[],"focusSessions":[]}' and
    jsonb_array_length(v->'tasks')+jsonb_array_length(v->'occurrences')+jsonb_array_length(v->'scheduledTasks')+jsonb_array_length(v->'externalEvents')+jsonb_array_length(v->'focusSessions')=0 then 0 else 1 end) returning * into s;
  update sync_private.devices set generation=s.generation,last_ack_revision=s.revision where user_id=uid and device_id=p_device_id;
  r := sync_private.state_result(s) || '{"status":"accepted"}';
  insert into sync_private.sync_control_requests values(uid,p_initialization_id,'initialize',h,r,clock_timestamp());
  return r;
end $$;

create function public.pull_sync_state(p_device_id uuid,p_operation_ids uuid[] default '{}',p_control_request_ids uuid[] default '{}')
returns jsonb language plpgsql security definer set search_path = '' as $$
declare uid uuid := sync_private.current_user_id(); d sync_private.devices; s sync_private.user_sync_state; ops jsonb; controls jsonb;
begin
  perform sync_private.lock_account(uid); d := sync_private.require_device(uid,p_device_id);
  if p_operation_ids is null or p_control_request_ids is null or cardinality(p_operation_ids)>256 or cardinality(p_control_request_ids)>64 then raise exception 'payloadInvalid'; end if;
  select * into s from sync_private.user_sync_state where user_id=uid;
  if not found then return '{"status":"uninitialized"}'; end if;
  if d.supported_schema_version<s.schema_version then raise exception 'schemaTooNew'; end if;
  select coalesce(jsonb_agg(jsonb_build_object('operationID',operation_id,'resultRevision',result_revision,'generation',generation,'stateHash',result_state_hash)), '[]') into ops
    from sync_private.sync_operations where user_id=uid and operation_id=any(p_operation_ids);
  select coalesce(jsonb_agg(jsonb_build_object('requestID',request_id,'kind',kind,'result',result)), '[]') into controls
    from sync_private.sync_control_requests where user_id=uid and request_id=any(p_control_request_ids);
  return sync_private.state_result(s) || jsonb_build_object('status','ready','state',s.state,'acceptedOperations',ops,'controlRequests',controls);
end $$;

create function public.acknowledge_sync_state(p_device_id uuid,p_generation bigint,p_revision bigint,p_state_hash text)
returns void language plpgsql security definer set search_path = '' as $$
declare uid uuid := sync_private.current_user_id(); s sync_private.user_sync_state;
begin
  perform sync_private.lock_account(uid); perform sync_private.require_device(uid,p_device_id);
  select * into s from sync_private.user_sync_state where user_id=uid;
  if s.user_id is null or p_generation is distinct from s.generation or p_revision is distinct from s.revision or p_state_hash is distinct from s.state_hash then raise exception 'revisionConflict'; end if;
  update sync_private.devices set generation=s.generation,last_ack_revision=s.revision where user_id=uid and device_id=p_device_id;
end $$;

create function public.commit_sync_state(p_operation jsonb,p_result_state jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare uid uuid := sync_private.current_user_id(); d sync_private.devices; s sync_private.user_sync_state; saved sync_private.sync_operations;
  opid uuid; did uuid; v jsonb; h text; r jsonb; contract jsonb;
begin
  perform sync_private.lock_account(uid);
  select schema into contract from sync_private.contracts where name='operation';
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

create function sync_private.replace_state(uid uuid,did uuid,rid uuid,request_hash text,v jsonb,reason text,s sync_private.user_sync_state)
returns jsonb language plpgsql set search_path = '' as $$
declare result jsonb; checkpoint uuid;
begin
  if (select count(*) from sync_private.state_checkpoints where user_id=uid)>=32 or (select count(*) from sync_private.sync_control_requests where user_id=uid)>=10000 then raise exception 'resourceLimit'; end if;
  insert into sync_private.state_checkpoints(user_id,sync_space_id,generation,revision,state_hash,cloud_state,reason,source_control_request_id,
    required_ack_device_ids,release_generation,release_revision,size_bytes)
  values(uid,s.sync_space_id,s.generation,s.revision,s.state_hash,s.state,reason,rid,
    array(select device_id from sync_private.devices where user_id=uid and revoked_at is null and lease_expires_at>clock_timestamp()),
    s.generation+1,s.revision+1,octet_length(s.state::text)) returning checkpoint_id into checkpoint;
  update sync_private.user_sync_state set state=v,state_hash=sync_private.hash_json(v),generation=generation+1,revision=revision+1,
    updated_at=clock_timestamp(),updated_by_device_id=did where user_id=uid returning * into s;
  result := sync_private.state_result(s) || jsonb_build_object('status','accepted','checkpointID',checkpoint);
  insert into sync_private.sync_control_requests values(uid,rid,reason,request_hash,result,clock_timestamp());
  return result;
end $$;

create function public.replace_sync_state(p_device_id uuid,p_replace_id uuid,p_expected_generation bigint,p_expected_revision bigint,p_state jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare uid uuid := sync_private.current_user_id(); s sync_private.user_sync_state; v jsonb; h text; saved sync_private.sync_control_requests;
begin
  perform sync_private.lock_account(uid); perform sync_private.require_device(uid,p_device_id);
  if p_replace_id is null then raise exception 'payloadInvalid'; end if;
  v := sync_private.validate_state(p_state);
  h := sync_private.hash_json(jsonb_build_object('kind','replace','deviceID',p_device_id,'generation',p_expected_generation,'revision',p_expected_revision,'state',v));
  select * into saved from sync_private.sync_control_requests where user_id=uid and request_id=p_replace_id;
  if found then
    if saved.request_hash<>h then raise exception 'requestIDReused'; end if;
    return saved.result || '{"status":"duplicate"}';
  end if;
  select * into s from sync_private.user_sync_state where user_id=uid;
  if s.user_id is null then raise exception 'firstSyncRequired'; end if;
  if p_expected_generation is distinct from s.generation or p_expected_revision is distinct from s.revision then raise exception 'revisionConflict'; end if;
  if s.schema_version<>(v->>'schemaVersion')::int then raise exception 'schemaTooNew'; end if;
  return sync_private.replace_state(uid,p_device_id,p_replace_id,h,v,'replace',s);
end $$;

create function public.export_sync_checkpoint(p_device_id uuid,p_checkpoint_id uuid)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare uid uuid := sync_private.current_user_id(); result jsonb; d sync_private.devices;
begin
  perform sync_private.lock_account(uid); d := sync_private.require_device(uid,p_device_id);
  select jsonb_build_object('state',cloud_state,'stateHash',state_hash,'generation',generation,'revision',revision)
    into result from sync_private.state_checkpoints where user_id=uid and checkpoint_id=p_checkpoint_id
    and (cloud_state->>'schemaVersion')::int<=d.supported_schema_version;
  if result is null then raise exception 'checkpointUnavailable'; end if;
  return result;
end $$;

create function public.maintain_sync_account(p_user_id uuid)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare deleted_ops integer; deleted_checkpoints integer;
begin
  perform 1 from sync_private.accounts where user_id=p_user_id for update;
  delete from sync_private.state_checkpoints c where c.user_id=p_user_id and c.delete_after<clock_timestamp()
    and exists(select 1 from sync_private.sync_control_requests r where r.user_id=c.user_id and r.request_id=c.source_control_request_id)
    and not exists(select 1 from sync_private.devices d where d.user_id=c.user_id and d.device_id=any(c.required_ack_device_ids)
      and d.revoked_at is null and d.lease_expires_at>clock_timestamp() and
      (d.generation<c.release_generation or (d.generation=c.release_generation and d.last_ack_revision<c.release_revision)));
  get diagnostics deleted_checkpoints=row_count;
  -- Keep accepted-ID receipts until every active device has moved past them, and
  -- their state is represented by a still-retained safety checkpoint.
  delete from sync_private.sync_operations o where o.user_id=p_user_id and o.accepted_at<clock_timestamp()-interval '30 days'
    and exists(select 1 from sync_private.state_checkpoints c where c.user_id=o.user_id and c.generation=o.generation and c.revision>=o.result_revision)
    and not exists(select 1 from sync_private.devices d where d.user_id=o.user_id and d.revoked_at is null and d.lease_expires_at>clock_timestamp()
      and (d.generation<o.generation or (d.generation=o.generation and d.last_ack_revision<=o.result_revision)));
  get diagnostics deleted_ops=row_count;
  return jsonb_build_object('operationsDeleted',deleted_ops,'checkpointsDeleted',deleted_checkpoints);
end $$;

revoke all on all functions in schema sync_private from public,anon,authenticated,service_role;
do $$ declare f regprocedure; begin
  for f in select oid::regprocedure from pg_proc where pronamespace='public'::regnamespace
    and proname in ('sync_access_allowed','sync_account_status','register_sync_device','list_sync_devices','revoke_sync_device',
      'initialize_sync_state','pull_sync_state','acknowledge_sync_state','commit_sync_state','replace_sync_state','export_sync_checkpoint','maintain_sync_account')
  loop
    execute format('revoke all on function %s from public,anon,authenticated,service_role',f);
    if f::text like '%maintain_sync_account%' then execute format('grant execute on function %s to service_role',f);
    else execute format('grant execute on function %s to authenticated',f); end if;
  end loop;
end $$;
