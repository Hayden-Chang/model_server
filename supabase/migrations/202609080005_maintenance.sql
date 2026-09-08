-- Bounded, fair batches reuse the existing per-account cleanup transaction.
alter table sync_private.accounts add column maintenance_attempted_at timestamptz not null default '-infinity';
create index accounts_maintenance_queue on sync_private.accounts(maintenance_attempted_at,user_id) where not deletion_pending;

create function public.maintain_sync_batch(p_limit integer default 100)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare account record; outcome jsonb; maintained integer := 0; failed integer := 0;
  operations bigint := 0; checkpoints bigint := 0; receipts bigint;
begin
  if p_limit is null or p_limit not between 1 and 100 then raise exception 'payloadInvalid'; end if;
  for account in select user_id from sync_private.accounts where not deletion_pending
    order by maintenance_attempted_at,user_id limit p_limit for update skip locked
  loop
    begin
      outcome := public.maintain_sync_account(account.user_id);
      operations := operations + (outcome->>'operationsDeleted')::bigint;
      checkpoints := checkpoints + (outcome->>'checkpointsDeleted')::bigint;
      maintained := maintained + 1;
    exception when others then
      -- Roll back this account's cleanup, then let other accounts make progress.
      failed := failed + 1;
    end;
    update sync_private.accounts set maintenance_attempted_at=clock_timestamp() where user_id=account.user_id;
  end loop;
  receipts := public.cleanup_deletion_receipts();
  return jsonb_build_object('accountsMaintained',maintained,'accountsFailed',failed,
    'operationsDeleted',operations,'checkpointsDeleted',checkpoints,'receiptsDeleted',receipts);
end $$;

revoke all on function public.maintain_sync_batch(integer) from public,anon,authenticated,service_role;
grant execute on function public.maintain_sync_batch(integer) to service_role;
