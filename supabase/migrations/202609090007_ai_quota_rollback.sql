-- Emergency rollback support for the account-aware AI cutover. These functions
-- are service_role only: clients can never export or close the ledger.

-- Read the guest ledger in the shape the legacy SQLite store uses. Account
-- principals are omitted because the legacy deployment only understands
-- installation tokens. Reserved calls are excluded from `used` because the new
-- API is stopped during rollback and those reservations can never complete.
create function public.ai_quota_export_legacy() returns jsonb
language sql security definer set search_path='' as $$
  select jsonb_build_object(
    'exportedAt', to_char(clock_timestamp() at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
    'principals', coalesce(jsonb_agg(entry order by id),'[]'::jsonb)
  )
  from (
    select p.id, jsonb_build_object(
      'principal', p.id,
      'supportCode', p.support_code,
      'developmentEnabled', p.development_enabled,
      'buckets', coalesce((
        select jsonb_agg(jsonb_build_object(
          'period', b.period,
          'used', greatest(0, b.used - coalesce((
            select count(*) from ai_private.requests r
            where r.bucket_id=b.id and r.state='reserved'),0)),
          'limit', case when b.period='free' then p.free_limit else 50 end
        ) order by b.period)
        from ai_private.buckets b where b.principal=p.id),'[]'::jsonb),
      'completedRequests', coalesce((
        select jsonb_agg(r.request_id order by r.request_id)
        from ai_private.requests r where r.principal=p.id and r.state='consumed'),'[]'::jsonb)
    ) as entry
    from ai_private.principals p
    where p.user_id is null
  ) s;
$$;
revoke all on function public.ai_quota_export_legacy() from public,anon,authenticated;
grant execute on function public.ai_quota_export_legacy() to service_role;

-- Refund every abandoned reservation and close the import gate so an accidental
-- overlay start fails readiness instead of serving stale quota next to the
-- restored legacy service. It never deletes ledger data.
create function public.ai_quota_rollback() returns jsonb
language plpgsql security definer set search_path='' as $$
declare refunded integer;
begin
  perform 1 from ai_private.runtime for update;
  with expired as (
    update ai_private.requests set state='refunded'
    where state='reserved' returning bucket_id
  ), totals as (
    select bucket_id, count(*)::integer n from expired group by bucket_id
  ), adjusted as (
    update ai_private.buckets b set used=greatest(0,b.used-t.n)
    from totals t where b.id=t.bucket_id returning 1
  )
  select coalesce(sum(n),0) into refunded from totals;
  update ai_private.runtime set legacy_import_complete=false;
  return jsonb_build_object('gateOpen',false,'refundedReservations',refunded);
end $$;
revoke all on function public.ai_quota_rollback() from public,anon,authenticated;
grant execute on function public.ai_quota_rollback() to service_role;

-- Run only after the SQLite reverse export succeeded. It clears imported guest
-- rows so a later re-cutover can import the updated legacy snapshot without an
-- import-hash clash. Account rows are preserved and the gate stays closed.
create function public.ai_quota_reset_import() returns jsonb
language plpgsql security definer set search_path='' as $$
declare removed integer;
begin
  perform 1 from ai_private.runtime for update;
  if (select legacy_import_complete from ai_private.runtime) then
    raise exception 'AI_ROLLBACK_REQUIRED';
  end if;
  delete from ai_private.principals where user_id is null;
  get diagnostics removed=row_count;
  return jsonb_build_object('removedGuestPrincipals',removed);
end $$;
revoke all on function public.ai_quota_reset_import() from public,anon,authenticated;
grant execute on function public.ai_quota_reset_import() to service_role;
