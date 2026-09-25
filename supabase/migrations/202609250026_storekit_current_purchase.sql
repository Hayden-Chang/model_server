begin;
-- A snapshot selects the current StoreKit purchase for this device/environment.
-- No snapshot preserves compatibility with released clients. An explicit empty
-- snapshot grants nothing, even if old unfinished transactions or workers arrive.
create table billing_private.storekit_selections (
  principal text not null references ai_private.principals(id) on delete cascade,
  environment text not null check(environment in ('sandbox','production')),
  purchase_key_hash text,
  primary key(principal,environment)
);
alter table billing_private.storekit_selections enable row level security;
revoke all on billing_private.storekit_selections from public,anon,authenticated,service_role;

create function billing_private.storekit_allows(actor text, scope text, key_hash text)
returns boolean language sql stable set search_path='' as $$
  select not exists(select 1 from billing_private.storekit_selections
                     where principal=actor and environment=scope)
      or exists(select 1 from billing_private.storekit_selections
                 where principal=actor and environment=scope and purchase_key_hash=key_hash);
$$;
revoke all on function billing_private.storekit_allows(text,text,text)
  from public,anon,authenticated,service_role;

create or replace function billing_private.aggregate_entitlement(
  target_principal text, target_environment text, force_revision boolean default false
) returns void
language plpgsql security definer set search_path='' as $$
declare best record; next_plan text; next_status text;
begin
  perform 1 from billing_private.account_entitlements
    where principal=target_principal for update;
  select * into best from billing_private.store_purchases sp
    where sp.environment=target_environment
      and billing_private.storekit_allows(target_principal,sp.environment,sp.purchase_key_hash)
      and exists (select 1 from billing_private.purchase_devices pd
                  where pd.purchase_id=sp.id and pd.principal=target_principal
                    and pd.revoked_at is null)
    order by (case sp.store_status when 'active' then 0
              when 'billing_retry' then 1 else 2 end),
             sp.expires_at desc nulls last
    limit 1;
  next_plan := case when best.store_status in ('active','billing_retry')
    then 'plus' else 'free' end;
  next_status := case best.store_status when 'active' then 'active'
    when 'billing_retry' then 'grace' when 'revoked' then 'revoked'
    else 'expired' end;
  update billing_private.account_entitlements set
    plan=next_plan, status=next_status, valid_until=best.expires_at,
    entitlement_revision=entitlement_revision+1, updated_at=now()
    where principal=target_principal
      and (force_revision or plan is distinct from next_plan
           or status is distinct from next_status
           or valid_until is distinct from best.expires_at);
end $$;
revoke all on function billing_private.aggregate_entitlement(text,text,boolean)
  from public,anon,authenticated,service_role;

-- The shared daily member meter must use the same purchase environment as
-- the entitlement projection; otherwise a production member with a sandbox
-- chain could consume the wrong chain's allowance.
create or replace function billing_private.plus_source(p_actor text)
returns table(account_timezone text, scope text)
language sql security definer set search_path='' as $$
  select coalesce(ae.account_timezone,'Asia/Shanghai'),
         coalesce((
           select sp.principal
           from billing_private.purchase_devices pd
           join billing_private.store_purchases sp on sp.id = pd.purchase_id
           where pd.principal = p_actor and pd.revoked_at is null
             and billing_private.storekit_allows(p_actor,sp.environment,sp.purchase_key_hash)
             and sp.environment = coalesce(
               nullif(current_setting('app.billing_environment',true),''),'sandbox')
             and sp.principal is not null
             and exists (select 1 from ai_private.principals pr where pr.id = sp.principal)
           order by (case sp.store_status when 'active' then 0
                          when 'billing_retry' then 1 else 2 end),
                    sp.expires_at desc nulls last, sp.id
           limit 1), p_actor)
  from billing_private.account_entitlements ae
  where ae.principal = p_actor and ae.plan = 'plus' and ae.status in ('active','grace');
$$;
revoke all on function billing_private.plus_source(text) from public,anon,authenticated,service_role;


alter function public.billing_service(text,jsonb) rename to billing_service_before_storekit;
revoke all on function public.billing_service_before_storekit(text,jsonb)
  from public,anon,authenticated,service_role;
create function public.billing_service(p_action text,p_data jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
  actor text := p_data->>'principal';
  scope text := coalesce(p_data->>'billingEnvironment',p_data->>'environment','sandbox');
  result jsonb;
  key_hash text;
begin
  if p_action='apple_sync' then
    if actor is null or actor !~ '^guest_[a-f0-9]{24}$' then
      return jsonb_build_object('code','DEVICE_REQUIRED');
    end if;
    if not exists(select 1 from ai_private.principals where id=actor) then
      return jsonb_build_object('code','UNAUTHORIZED');
    end if;
    if scope not in ('sandbox','production') then
      return jsonb_build_object('code','ENVIRONMENT_MISMATCH');
    end if;
    -- Only the API supplies a verified originalTransactionId; public clients
    -- send a signed JWS to /billing/apple/sync. Empty means no current purchase.
    perform billing_private.ensure_account(actor);
    if p_data ? 'originalTransactionId' then
      result := public.billing_service_before_storekit('apple_verify',p_data);
      if result ? 'code' then return result; end if;
      key_hash := encode(sha256(convert_to('apple|' || (p_data->>'originalTransactionId'),'UTF8')),'hex');
    end if;
    insert into billing_private.storekit_selections values(actor,scope,key_hash)
      on conflict(principal,environment) do update set purchase_key_hash=excluded.purchase_key_hash;
    perform billing_private.aggregate_entitlement(actor,scope,true);
    result := public.billing_service_before_storekit('entitlement',p_data);
  else
    result := public.billing_service_before_storekit(p_action,p_data);
  end if;
  if result ? 'code' then return result; end if;
  if p_action in ('apple_sync','apple_verify','entitlement') then
    result := jsonb_set(result,'{billingSources}',
      (select coalesce(jsonb_agg(jsonb_build_object('provider',sp.provider,
        'productId',sp.product_id,'expiresAt',billing_private.iso(sp.expires_at))),'[]'::jsonb)
       from billing_private.store_purchases sp
       join billing_private.purchase_devices pd on pd.purchase_id=sp.id
       where pd.principal=actor and pd.revoked_at is null and sp.environment=scope
         and sp.store_status in ('active','billing_retry')
         and billing_private.storekit_allows(actor,scope,sp.purchase_key_hash)));
  end if;
  return result;
end $$;
revoke all on function public.billing_service(text,jsonb) from public,anon,authenticated;
grant execute on function public.billing_service(text,jsonb) to service_role;
create or replace function public.billing_environment_schema() returns integer
language sql set search_path='' as $$ select 26 $$;
commit;
