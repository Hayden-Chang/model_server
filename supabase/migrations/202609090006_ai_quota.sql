-- Only the trusted business API can mutate the AI ledger. Client JWTs may
-- resolve their own live account identity, but cannot reserve or grant quota.
create schema ai_private;
revoke all on schema ai_private from public, anon, authenticated, service_role;
alter default privileges in schema ai_private revoke execute on functions from public;

create table ai_private.runtime (
  singleton boolean primary key default true check(singleton),
  legacy_import_complete boolean not null default false
);
insert into ai_private.runtime default values;
create table ai_private.principals (
  id text primary key,
  user_id uuid unique references auth.users(id) on delete cascade,
  support_code text not null unique,
  free_limit integer not null default 50 check(free_limit between 1 and 10000),
  development_enabled boolean not null default false,
  claimed boolean not null default false,
  claimed_by uuid references auth.users(id) on delete set null,
  import_hash text,
  check ((user_id is null and id ~ '^guest_[a-f0-9]{24}$') or (user_id is not null and id = 'account:' || user_id::text))
);
create table ai_private.buckets (
  id bigint generated always as identity primary key,
  principal text not null references ai_private.principals(id) on delete cascade,
  period text not null,
  used integer not null default 0 check(used >= 0),
  unique(principal,period)
);
create table ai_private.requests (
  principal text not null references ai_private.principals(id) on delete cascade,
  request_id text not null check(length(request_id) between 1 and 200),
  body_hash text,
  bucket_id bigint references ai_private.buckets(id) on delete cascade,
  attempt uuid not null,
  state text not null check(state in ('reserved','consumed','refunded')),
  expires_at timestamptz not null,
  primary key(principal,request_id)
);
create index ai_requests_expiry on ai_private.requests(principal,expires_at) where state='reserved';
alter table ai_private.runtime enable row level security;
alter table ai_private.principals enable row level security;
alter table ai_private.buckets enable row level security;
alter table ai_private.requests enable row level security;
revoke all on all tables in schema ai_private from public,anon,authenticated,service_role;
revoke all on all sequences in schema ai_private from public,anon,authenticated,service_role;

create function public.ai_account_identity() returns jsonb
language plpgsql security definer set search_path='' as $$
declare uid uuid;
begin
  begin uid := sync_private.current_user_id();
  exception when others then raise exception using errcode='42501',message='ACCOUNT_UNAVAILABLE'; end;
  if exists(select 1 from sync_private.accounts where user_id=uid and deletion_pending) then
    raise exception using errcode='42501',message='ACCOUNT_UNAVAILABLE';
  end if;
  return jsonb_build_object('userID',uid,'sessionID',auth.jwt()->>'session_id');
end $$;
revoke all on function public.ai_account_identity() from public,anon,service_role;
grant execute on function public.ai_account_identity() to authenticated;

create function ai_private.expire_reservations(actor text) returns void
language plpgsql set search_path='' as $$
begin
  with expired as (
    update ai_private.requests set state='refunded'
    where principal=actor and state='reserved' and expires_at <= clock_timestamp()
    returning bucket_id
  ), totals as (select bucket_id,count(*)::integer n from expired group by bucket_id)
  update ai_private.buckets b set used=greatest(0,b.used-t.n) from totals t where b.id=t.bucket_id;
end $$;

create function ai_private.quota_status(actor text, dev_allowed boolean) returns jsonb
language plpgsql set search_path='' as $$
declare p ai_private.principals; period_key text; quota_limit integer; used_count integer;
  resets timestamptz; instant timestamptz := clock_timestamp();
begin
  select * into strict p from ai_private.principals where id=actor;
  period_key := 'free'; quota_limit := p.free_limit;
  if dev_allowed and p.development_enabled and p.user_id is null then
    period_key := 'member:' || (instant at time zone 'Asia/Shanghai')::date::text;
    quota_limit := 50;
    resets := ((instant at time zone 'Asia/Shanghai')::date + 1)::timestamp at time zone 'Asia/Shanghai';
  end if;
  select used into used_count from ai_private.buckets where principal=actor and period=period_key;
  return jsonb_build_object('supportCode',p.support_code,'limit',quota_limit,
    'used',coalesce(used_count,0),'remaining',greatest(0,quota_limit-coalesce(used_count,0)),
    'enabled',dev_allowed and p.development_enabled and p.user_id is null,
    'resetsAt',to_char(resets at time zone 'Asia/Shanghai','YYYY-MM-DD"T"HH24:MI:SS') || '+08:00','period',period_key);
end $$;

-- Caller is service_role, never an App-supplied user ID. The API first resolves
-- account IDs with ai_account_identity, then passes both IDs for a live recheck.
create function public.ai_quota_service(p_action text,p_data jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
  actor text := p_data->>'principal'; p ai_private.principals; guest ai_private.principals;
  q jsonb; r ai_private.requests; b_id bigint; item jsonb; new_actor text;
  dev_allowed boolean := coalesce((p_data->>'developmentAllowed')::boolean,false);
  request_key text := p_data->>'requestID'; attempt_id uuid := (p_data->>'attempt')::uuid;
  uid uuid; sid uuid; total integer; guest_used integer;
begin
  if p_action in ('import','finish_import') then
    perform 1 from ai_private.runtime for update;
    if p_action='finish_import' then
      update ai_private.runtime set legacy_import_complete=true;
      return jsonb_build_object('ok',true);
    end if;
    if (select legacy_import_complete from ai_private.runtime) then
      raise exception 'AI_IMPORT_ALREADY_CLOSED';
    end if;
    if actor !~ '^guest_[a-f0-9]{24}$' or actor is null then raise exception 'AI_INVALID_PRINCIPAL'; end if;
    insert into ai_private.principals(id,support_code,free_limit,development_enabled,import_hash)
    values(actor,p_data->>'supportCode',(p_data->>'limit')::integer,
      (p_data->>'developmentEnabled')::boolean,p_data->>'importHash') on conflict do nothing;
    get diagnostics total=row_count;
    if total=0 then
      if (select import_hash from ai_private.principals where id=actor) is distinct from p_data->>'importHash' then
        raise exception 'AI_IMPORT_CHANGED';
      end if;
      return jsonb_build_object('ok',true);
    end if;
    for item in select value from jsonb_array_elements(p_data->'buckets') loop
      insert into ai_private.buckets(principal,period,used)
        values(actor,item->>'period',(item->>'used')::integer);
    end loop;
    for item in select value from jsonb_array_elements(p_data->'completedRequests') loop
      insert into ai_private.requests(principal,request_id,attempt,state,expires_at)
        values(actor,item#>>'{}',gen_random_uuid(),'consumed',clock_timestamp()) on conflict do nothing;
    end loop;
    return jsonb_build_object('ok',true);
  end if;

  perform 1 from ai_private.runtime for share;
  if not (select legacy_import_complete from ai_private.runtime) then raise exception 'AI_IMPORT_REQUIRED'; end if;
  if p_action='ready' then return jsonb_build_object('ok',true); end if;
  if p_action in ('admin_status','admin_reset') then
    select id into actor from ai_private.principals where support_code=p_data->>'supportCode';
    if actor is null then return jsonb_build_object('code','SUPPORT_CODE_NOT_FOUND'); end if;
    -- A claimed installation keeps its legacy support code for support, but the
    -- live quota now belongs to the claiming account.
    select * into p from ai_private.principals where id=actor;
    if not found then return jsonb_build_object('code','SUPPORT_CODE_NOT_FOUND'); end if;
    if p.claimed and p.claimed_by is not null then actor := 'account:'||p.claimed_by::text; end if;
  elsif p_action='admin_reset_all' then
    -- Lock all actors in the same order as claims; outstanding attempts must settle first.
    perform 1 from ai_private.principals order by id for update;
    for new_actor in select id from ai_private.principals loop
      perform ai_private.expire_reservations(new_actor);
    end loop;
    if exists(select 1 from ai_private.requests where state='reserved') then
      return jsonb_build_object('code','AI_REQUEST_IN_PROGRESS');
    end if;
    update ai_private.buckets set used=0;
    select count(*) into total from ai_private.principals where not claimed;
    return jsonb_build_object('refreshedInstallations',total);
  end if;

  if p_action not in ('admin_status','admin_reset') then
    if actor ~ '^account:' then
      uid := substring(actor from 9)::uuid; sid := (p_data->>'sessionID')::uuid;
      if not exists(select 1 from auth.users u join auth.sessions s on s.user_id=u.id
        where u.id=uid and s.id=sid and u.email_confirmed_at is not null and not coalesce(u.is_anonymous,false))
        or exists(select 1 from sync_private.accounts where user_id=uid and deletion_pending) then
        return jsonb_build_object('code','ACCOUNT_UNAVAILABLE');
      end if;
    elsif actor !~ '^guest_[a-f0-9]{24}$' or actor is null then
      raise exception 'AI_INVALID_PRINCIPAL';
    end if;
    insert into ai_private.principals(id,user_id,support_code,free_limit)
      values(actor,uid,p_data->>'supportCode',case when uid is null then (p_data->>'freeLimit')::integer else 50 end)
      on conflict do nothing;
  end if;

  -- Claim and reserve serialize on the same rows. Claims cannot straddle an
  -- in-flight model request or resurrect a guest after account deletion.
  if p_action='claim' then
    if uid is null or (p_data->>'guest') !~ '^guest_[a-f0-9]{24}$' then raise exception 'AI_INVALID_CLAIM'; end if;
    insert into ai_private.principals(id,support_code,free_limit)
      values(p_data->>'guest',p_data->>'guestSupportCode',(p_data->>'freeLimit')::integer) on conflict do nothing;
    perform 1 from ai_private.principals where id in (actor,p_data->>'guest') order by id for update;
    select * into guest from ai_private.principals where id=p_data->>'guest';
    if not found then return jsonb_build_object('code','AI_GUEST_NOT_FOUND'); end if;
    if guest.claimed then
      if guest.claimed_by=uid then return ai_private.quota_status(actor,false); end if;
      return jsonb_build_object('code','AI_GUEST_ALREADY_CLAIMED');
    end if;
    perform ai_private.expire_reservations(actor);
    perform ai_private.expire_reservations(guest.id);
    if exists(select 1 from ai_private.requests where principal in (actor,guest.id) and state='reserved') then
      return jsonb_build_object('code','AI_REQUEST_IN_PROGRESS');
    end if;
    select coalesce(max(used),0) into guest_used from ai_private.buckets where principal=guest.id and period='free';
    insert into ai_private.buckets(principal,period,used) values(actor,'free',guest_used)
      on conflict(principal,period) do update set used=greatest(ai_private.buckets.used,excluded.used);
    insert into ai_private.requests(principal,request_id,body_hash,attempt,state,expires_at)
      select actor,request_id,body_hash,gen_random_uuid(),'consumed',expires_at
      from ai_private.requests where principal=guest.id and state='consumed' on conflict do nothing;
    update ai_private.principals set claimed=true,claimed_by=uid,development_enabled=false where id=guest.id;
    return ai_private.quota_status(actor,false);
  end if;

  select * into p from ai_private.principals where id=actor for update;
  if not found then return jsonb_build_object('code','ACCOUNT_SERVICE_UNAVAILABLE'); end if;
  if p.claimed then return jsonb_build_object('code','AI_ACCOUNT_REQUIRED'); end if;
  perform ai_private.expire_reservations(actor);
  if p_action='membership' then
    if not dev_allowed or p.user_id is not null then return jsonb_build_object('code','DEVELOPMENT_MEMBERSHIP_DISABLED'); end if;
    update ai_private.principals set development_enabled=(p_data->>'enabled')::boolean where id=actor;
  end if;
  q := ai_private.quota_status(actor,dev_allowed);
  if p_action in ('status','membership','admin_status') then return q; end if;
  if p_action='admin_reset' then
    if exists(select 1 from ai_private.requests where principal=actor and state='reserved') then
      return jsonb_build_object('code','AI_REQUEST_IN_PROGRESS');
    end if;
    update ai_private.buckets set used=0 where principal=actor;
    return ai_private.quota_status(actor,dev_allowed);
  end if;
  if p_action='reserve' then
    if request_key is null or length(request_key) not between 1 and 200 or attempt_id is null
      or coalesce(p_data->>'bodyHash','') !~ '^[a-f0-9]{64}$' then raise exception 'AI_INVALID_REQUEST'; end if;
    select * into r from ai_private.requests where principal=actor and request_id=request_key;
    if found then
      if r.body_hash is not null and r.body_hash <> p_data->>'bodyHash' then
        return jsonb_build_object('code','AI_REQUEST_ID_CONFLICT');
      end if;
      if r.state='consumed' then return jsonb_build_object('code','AI_REQUEST_ALREADY_COMPLETED'); end if;
      if r.state='reserved' then
        if r.attempt=attempt_id then return q || jsonb_build_object('attempt',attempt_id); end if;
        return jsonb_build_object('code','AI_REQUEST_IN_PROGRESS');
      end if;
    end if;
    if (q->>'remaining')::integer=0 then
      return q || jsonb_build_object('code',case when q->>'period'='free' then 'AI_QUOTA_EXHAUSTED' else 'AI_DAILY_QUOTA_EXHAUSTED' end);
    end if;
    insert into ai_private.buckets(principal,period,used) values(actor,q->>'period',1)
      on conflict(principal,period) do update set used=ai_private.buckets.used+1 returning id into b_id;
    insert into ai_private.requests(principal,request_id,body_hash,bucket_id,attempt,state,expires_at)
      values(actor,request_key,p_data->>'bodyHash',b_id,attempt_id,'reserved',clock_timestamp()+interval '10 minutes')
      on conflict(principal,request_id) do update set bucket_id=excluded.bucket_id,attempt=excluded.attempt,
        state='reserved',expires_at=excluded.expires_at,body_hash=excluded.body_hash;
    return ai_private.quota_status(actor,dev_allowed) || jsonb_build_object('attempt',attempt_id);
  end if;
  if p_action='finish' then
    select * into r from ai_private.requests where principal=actor and request_id=request_key;
    if not found or r.attempt is distinct from attempt_id then return jsonb_build_object('code','AI_RESERVATION_EXPIRED'); end if;
    new_actor := case when (p_data->>'consume')::boolean then 'consumed' else 'refunded' end;
    if r.state=new_actor then return jsonb_build_object('ok',true); end if;
    if r.state <> 'reserved' then return jsonb_build_object('code','AI_RESERVATION_EXPIRED'); end if;
    update ai_private.requests set state=new_actor where principal=actor and request_id=request_key;
    if new_actor='refunded' then update ai_private.buckets set used=greatest(0,used-1) where id=r.bucket_id; end if;
    return jsonb_build_object('ok',true);
  end if;
  raise exception 'AI_UNKNOWN_ACTION';
end $$;
revoke all on function public.ai_quota_service(text,jsonb) from public,anon,authenticated;
grant execute on function public.ai_quota_service(text,jsonb) to service_role;
revoke all on all functions in schema ai_private from public,anon,authenticated,service_role;
