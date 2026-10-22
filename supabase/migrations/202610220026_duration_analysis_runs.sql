-- A run owns one quota receipt and immutable input; steps own recoverable model work.
create table ai_private.duration_runs (
  principal text not null references ai_private.principals(id) on delete cascade,
  environment text not null check(environment in ('sandbox','production')),
  id text not null,
  input_hash text not null,
  input jsonb not null,
  quota_data jsonb not null,
  quota_attempt uuid not null,
  points text not null default 'reserved' check(points in ('reserved','consumed','refunded')),
  state text not null default 'queued',
  classification jsonb,
  created_at timestamptz not null default clock_timestamp(),
  primary key(principal,environment,id)
);
create table ai_private.duration_steps (
  principal text not null,
  environment text not null,
  run_id text not null,
  key text not null,
  input jsonb not null,
  input_hash text not null,
  state text not null default 'queued' check(state in ('queued','running','ready','failed')),
  attempt uuid,
  lease_until timestamptz,
  result jsonb,
  error text,
  primary key(principal,environment,run_id,key),
  foreign key(principal,environment,run_id) references ai_private.duration_runs on delete cascade
);
alter table ai_private.duration_runs enable row level security;
alter table ai_private.duration_steps enable row level security;
revoke all on ai_private.duration_runs,ai_private.duration_steps from public,anon,authenticated;

create function ai_private.duration_status(r ai_private.duration_runs) returns jsonb
language sql set search_path='' as $$
  select jsonb_build_object('runID',r.id,'status',r.state,'startedAt',r.created_at,
    'classification',r.classification,'chargedPoints',case when r.points='consumed' then 1 else 0 end,
    'steps',coalesce((select jsonb_agg(jsonb_build_object('categoryID',s.key,'status',s.state,
      'inputHash',s.input_hash,'result',s.result,'error',s.error) order by s.key)
      from ai_private.duration_steps s where s.principal=r.principal and s.environment=r.environment
        and s.run_id=r.id and s.key<>'__classify__'),'[]'::jsonb))
$$;

create function ai_private.duration_settle(p text,e text,i text) returns void
language plpgsql set search_path='' as $$
declare r ai_private.duration_runs; q jsonb; useful boolean; unfinished boolean;
begin
  select * into strict r from ai_private.duration_runs where principal=p and environment=e and id=i for update;
  select exists(select 1 from ai_private.duration_steps where principal=p and environment=e and run_id=i
    and state='ready' and key<>'__classify__' and result->>'cached' is distinct from 'true'),
    exists(select 1 from ai_private.duration_steps where principal=p and environment=e and run_id=i
    and state in ('queued','running')) into useful,unfinished;
  -- Publish a usable saved result and its charge in the same transaction.
  if r.points='reserved' and (useful or not unfinished) then
    q := public.ai_quota_service('finish',r.quota_data || jsonb_build_object('attempt',r.quota_attempt,'consume',useful));
    if q ? 'code' then raise exception 'ANALYSIS_QUOTA_FINISH_FAILED'; end if;
    update ai_private.duration_runs set points=case when useful then 'consumed' else 'refunded' end
      where principal=p and environment=e and id=i;
  end if;
  update ai_private.duration_runs set state=case when unfinished then 'running'
    when exists(select 1 from ai_private.duration_steps where principal=p and environment=e and run_id=i and state='failed')
      then case when useful then 'partial' else 'failed' end else 'completed' end
    where principal=p and environment=e and id=i;
end $$;

create function ai_private.duration_add_steps(p text,e text,i text,steps jsonb) returns void
language plpgsql set search_path='' as $$
declare step jsonb; cached_result jsonb;
begin
  for step in select * from jsonb_array_elements(steps) loop
    -- Only a complete server-owned snapshot can satisfy a cache hit. Client
    -- hints cannot create a ready result or bypass scoring.
    select result into cached_result from ai_private.duration_steps where principal=p and environment=e
      and input_hash=step->>'inputHash' and state='ready' and key<>'__classify__'
      and result ? 'items' and step->>'key'<>'__classify__' limit 1;
    insert into ai_private.duration_steps(principal,environment,run_id,key,input,input_hash,state,result)
      values(p,e,i,step->>'key',step->'input',step->>'inputHash',
        case when cached_result is not null then 'ready' else 'queued' end,
        case when cached_result is not null then cached_result || jsonb_build_object('cached',true) else null end);
  end loop;
end $$;

create function public.duration_analysis_run_service(p_action text,p_data jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
  actor text:=p_data->>'principal'; env text:=p_data->>'billingEnvironment'; run_key text:=p_data->>'runID';
  r ai_private.duration_runs; s ai_private.duration_steps; step jsonb; q jsonb; qdata jsonb;
  attempt_id uuid:=(p_data->>'attempt')::uuid;
begin
  if env not in ('sandbox','production') or env is null then raise exception 'BILLING_INVALID_ENVIRONMENT'; end if;
  if p_action='claim' then
    -- Service-only queue. Lock run before step; every process shares this limit.
    delete from ai_private.duration_runs where created_at < clock_timestamp()-interval '30 days';
    select runs.* into r from ai_private.duration_runs runs
      where runs.environment=env and runs.state in ('queued','running')
        and (select count(*) from ai_private.duration_steps x where x.principal=runs.principal
          and x.environment=runs.environment and x.run_id=runs.id and x.state='running'
          and x.lease_until>clock_timestamp()) < 20
        and exists(select 1 from ai_private.duration_steps x where x.principal=runs.principal
          and x.environment=runs.environment and x.run_id=runs.id
          and (x.state='queued' or x.state='running' and x.lease_until<=clock_timestamp()))
      order by runs.created_at for update skip locked limit 1;
    if not found then return '{}'::jsonb; end if;
    -- Re-read after acquiring the run lock: the candidate query may have
    -- observed a snapshot from before a concurrent claimant committed.
    if (select count(*) from ai_private.duration_steps x where x.principal=r.principal
        and x.environment=r.environment and x.run_id=r.id and x.state='running'
        and x.lease_until>clock_timestamp()) >= 20 then return '{}'::jsonb; end if;
    if r.points='reserved' then
      -- Reserve in the existing quota lock order, then extend that same hold.
      -- An expiry sweep may have refunded it; the receipt still charges at most one.
      q:=public.ai_quota_service('reserve',r.quota_data || jsonb_build_object('attempt',r.quota_attempt));
      if q ? 'code' then return q; end if;
      update ai_private.requests set expires_at=clock_timestamp()+interval '10 minutes'
        where principal=r.principal and request_id=r.quota_data->>'requestID'
          and attempt=r.quota_attempt and state='reserved';
    end if;
    select * into strict s from ai_private.duration_steps where principal=r.principal
      and environment=r.environment and run_id=r.id
      and (state='queued' or state='running' and lease_until<=clock_timestamp())
      order by key for update limit 1;
    update ai_private.duration_steps set state='running',attempt=attempt_id,
      lease_until=clock_timestamp()+interval '120 seconds',error=null
      where principal=s.principal and environment=s.environment and run_id=s.run_id and key=s.key;
    update ai_private.duration_runs set state='running' where principal=r.principal and environment=r.environment and id=r.id;
    return jsonb_build_object('principal',r.principal,'billingEnvironment',r.environment,'runID',r.id,
      'key',s.key,'input',s.input,'runInput',r.input,'classification',r.classification,'inputHash',s.input_hash,'attempt',attempt_id);
  end if;
  if actor !~ '^guest_[a-f0-9]{24}$' or env not in ('sandbox','production') or run_key is null then
    return jsonb_build_object('code','DEVICE_REQUIRED');
  end if;
  perform pg_advisory_xact_lock(hashtextextended(actor || ':' || env || ':' || run_key,0));
  select * into r from ai_private.duration_runs where principal=actor and environment=env and id=run_key for update;
  if p_action='start' then
    if found then
      if r.input_hash<>p_data->>'inputHash' then return jsonb_build_object('code','AI_REQUEST_ID_CONFLICT'); end if;
      return ai_private.duration_status(r);
    end if;
    qdata:=(p_data->'quotaData') || jsonb_build_object('principal',actor,'billingEnvironment',env,
      'requestID','duration:' || env || ':' || run_key,'bodyHash',p_data->>'inputHash');
    q:=public.ai_quota_service('reserve',qdata || jsonb_build_object('attempt',attempt_id));
    if q ? 'code' then return q; end if;
    insert into ai_private.duration_runs(principal,environment,id,input_hash,input,quota_data,quota_attempt)
      values(actor,env,run_key,p_data->>'inputHash',p_data->'input',qdata,attempt_id) returning * into r;
    perform ai_private.duration_add_steps(actor,env,run_key,p_data->'steps');
    perform ai_private.duration_settle(actor,env,run_key);
  else
    if not found then return jsonb_build_object('code','ANALYSIS_RUN_NOT_FOUND'); end if;
    if p_action='retry' then
      if r.state not in ('partial','failed') then return ai_private.duration_status(r); end if;
      if r.points='refunded' then
        q:=public.ai_quota_service('reserve',r.quota_data || jsonb_build_object('attempt',attempt_id));
        if q ? 'code' then return q; end if;
        update ai_private.duration_runs set quota_attempt=attempt_id,points='reserved'
          where principal=actor and environment=env and id=run_key;
      end if;
      update ai_private.duration_steps set state='queued',error=null where principal=actor
        and environment=env and run_id=run_key and state='failed';
      update ai_private.duration_runs set state='queued' where principal=actor and environment=env and id=run_key;
    elsif p_action in ('complete','fail') then
      select * into s from ai_private.duration_steps where principal=actor and environment=env
        and run_id=run_key and key=p_data->>'key' for update;
      if not found or s.attempt is distinct from attempt_id then return jsonb_build_object('code','ANALYSIS_STALE_ATTEMPT'); end if;
      if s.state='running' then
        if p_action='complete' then
          update ai_private.duration_steps set state='ready',result=p_data->'result',lease_until=null
            where principal=actor and environment=env and run_id=run_key and key=s.key;
          if s.key='__classify__' then
            update ai_private.duration_runs set classification=p_data->'result'
              where principal=actor and environment=env and id=run_key;
            if p_data->'nextInput' is not null and p_data->'nextInput'<>'null'::jsonb then
              update ai_private.duration_steps set state='queued',input=p_data->'nextInput'
                where principal=actor and environment=env and run_id=run_key and key=s.key;
            else
              perform ai_private.duration_add_steps(actor,env,run_key,p_data->'steps');
            end if;
          end if;
        else
          update ai_private.duration_steps set state='failed',error='ANALYSIS_STEP_FAILED',lease_until=null
            where principal=actor and environment=env and run_id=run_key and key=s.key;
        end if;
        perform ai_private.duration_settle(actor,env,run_key);
      end if;
    elsif p_action<>'get' then raise exception 'ANALYSIS_UNKNOWN_ACTION'; end if;
  end if;
  select * into strict r from ai_private.duration_runs where principal=actor and environment=env and id=run_key;
  return ai_private.duration_status(r);
end $$;
revoke all on function ai_private.duration_status(ai_private.duration_runs),ai_private.duration_settle(text,text,text),
  ai_private.duration_add_steps(text,text,text,jsonb)
  from public,anon,authenticated,service_role;
revoke all on function public.duration_analysis_run_service(text,jsonb) from public,anon,authenticated;
grant execute on function public.duration_analysis_run_service(text,jsonb) to service_role;
