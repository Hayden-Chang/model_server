-- Receipts deliberately outlive auth.users. They contain no cloud state or email.
create table sync_private.account_deletions (
  request_id uuid primary key,
  user_id uuid unique,
  receipt_hash text not null unique,
  status text not null check(status in ('pending','dataDeleted','completed')),
  created_at timestamptz not null default clock_timestamp(),
  completed_at timestamptz
);
alter table sync_private.account_deletions enable row level security;
revoke all on sync_private.account_deletions from public,anon,authenticated,service_role;

create function public.request_account_deletion(p_deletion_request_id uuid,p_receipt text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare uid uuid := sync_private.current_user_id(); previous sync_private.account_deletions; receipt_hash text;
begin
  if p_deletion_request_id is null or p_receipt is null or p_receipt !~ '^[0-9a-f]{64}$' then raise exception 'payloadInvalid'; end if;
  receipt_hash := sync_private.hash_json(to_jsonb(p_receipt));
  insert into sync_private.accounts(user_id) values(uid) on conflict do nothing;
  perform 1 from sync_private.accounts where user_id=uid for update;
  select * into previous from sync_private.account_deletions where user_id=uid;
  if found then
    if previous.request_id<>p_deletion_request_id or previous.receipt_hash<>receipt_hash then raise exception 'deletionPending'; end if;
    return jsonb_build_object('status',previous.status);
  end if;
  if not exists(select 1 from jsonb_array_elements(coalesce(auth.jwt()->'amr','[]')) a
    where a->>'method'='otp' and (a->>'timestamp')::numeric between extract(epoch from clock_timestamp())-600 and extract(epoch from clock_timestamp())+30)
    then raise exception 'reauthRequired'; end if;
  perform sync_private.lock_account(uid);
  -- The account lock serializes competing deletion requests and all sync writes.
  insert into sync_private.account_deletions(request_id,user_id,receipt_hash,status) values(p_deletion_request_id,uid,receipt_hash,'pending');
  update sync_private.accounts set deletion_pending=true where user_id=uid;
  return '{"status":"pending"}';
end $$;

create function public.account_deletion_status(p_receipt text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare result jsonb;
begin
  if p_receipt is null or p_receipt !~ '^[0-9a-f]{64}$' then return '{"status":"unknown"}'; end if;
  select jsonb_build_object('status',status) into result from sync_private.account_deletions
    where receipt_hash=sync_private.hash_json(to_jsonb(p_receipt));
  return coalesce(result,'{"status":"unknown"}');
end $$;

create function public.pending_account_deletions()
returns jsonb language sql security definer set search_path = '' as $$
  select coalesce(jsonb_agg(jsonb_build_object('requestID',request_id,'userID',user_id)), '[]')
  from (select request_id,user_id from sync_private.account_deletions where status<>'completed' order by created_at limit 100) pending
$$;

create function public.prepare_account_deletion(p_request_id uuid)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare job sync_private.account_deletions;
begin
  select * into job from sync_private.account_deletions where request_id=p_request_id for update;
  if not found then raise exception 'deletionUnavailable'; end if;
  if job.status='completed' then return '{"status":"completed"}'; end if;
  perform 1 from sync_private.accounts where user_id=job.user_id for update;
  delete from sync_private.state_checkpoints where user_id=job.user_id;
  delete from sync_private.sync_control_requests where user_id=job.user_id;
  delete from sync_private.sync_operations where user_id=job.user_id;
  delete from sync_private.user_sync_state where user_id=job.user_id;
  delete from public.sync_changes where user_id=job.user_id;
  delete from sync_private.devices where user_id=job.user_id;
  update sync_private.account_deletions set status='dataDeleted' where request_id=p_request_id;
  return jsonb_build_object('status','dataDeleted','userID',job.user_id);
end $$;

create function public.complete_account_deletion(p_request_id uuid)
returns void language plpgsql security definer set search_path = '' as $$
declare job sync_private.account_deletions;
begin
  select * into job from sync_private.account_deletions where request_id=p_request_id for update;
  if not found then raise exception 'deletionUnavailable'; end if;
  if job.status='completed' then return; end if;
  if job.status<>'dataDeleted' or exists(select 1 from auth.users where id=job.user_id) then raise exception 'authDeletionIncomplete'; end if;
  update sync_private.account_deletions set status='completed',completed_at=clock_timestamp(),user_id=null where request_id=p_request_id;
end $$;

create function public.cleanup_deletion_receipts()
returns bigint language plpgsql security definer set search_path = '' as $$
declare n bigint;
begin
  delete from sync_private.account_deletions where status='completed' and completed_at<clock_timestamp()-interval '30 days';
  get diagnostics n=row_count;
  return n;
end $$;

revoke all on function public.request_account_deletion(uuid,text),public.account_deletion_status(text),
  public.pending_account_deletions(),public.prepare_account_deletion(uuid),public.complete_account_deletion(uuid),public.cleanup_deletion_receipts()
  from public,anon,authenticated,service_role;
grant execute on function public.request_account_deletion(uuid,text) to authenticated;
grant execute on function public.account_deletion_status(text) to anon,authenticated;
grant execute on function public.pending_account_deletions(),public.prepare_account_deletion(uuid),public.complete_account_deletion(uuid),public.cleanup_deletion_receipts()
  to service_role;
