-- Keep StoreKit sandbox purchases out of production device entitlements.
-- The API supplies its configured Apple environment; callers cannot select it.
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

-- Existing verification code calls the one-argument helper. The scoped RPC
-- wrapper sets this transaction-local value before invoking that code.
create or replace function billing_private.aggregate_entitlement(
  target_principal text
) returns void
language plpgsql security definer set search_path='' as $$
begin
  perform billing_private.aggregate_entitlement(target_principal,
    coalesce(nullif(current_setting('app.billing_environment',true),''),'sandbox'),true);
end $$;

-- Preserve the previously deployed RPC body and wrap it at the service-role
-- boundary so the existing claim, verification and event behavior stays intact.
alter function public.billing_service(text,jsonb) rename to billing_service_unscoped;
revoke all on function public.billing_service_unscoped(text,jsonb) from public,anon,authenticated,service_role;

create function public.billing_service(p_action text, p_data jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
  scope text := coalesce(p_data->>'billingEnvironment',p_data->>'environment','sandbox');
  actor text := p_data->>'principal';
  result jsonb;
begin
  if scope not in ('sandbox','production') then
    return jsonb_build_object('code','ENVIRONMENT_MISMATCH');
  end if;
  if p_action='apple_verify' and actor ~ '^guest_[a-f0-9]{24}$'
     and exists (select 1 from ai_private.principals where id=actor)
     and p_data->>'environment' is distinct from scope then
    return jsonb_build_object('code','ENVIRONMENT_MISMATCH');
  end if;
  -- The deployed verifier looks up a purchase by hash without its environment.
  -- Refuse a cross-environment hash collision before it can mutate that row.
  if p_action='apple_verify' and exists (
    select 1 from billing_private.store_purchases sp
    where sp.provider='apple'
      and sp.purchase_key_hash=encode(sha256(convert_to(
        'apple|' || (p_data->>'originalTransactionId'),'UTF8')),'hex')
      and sp.environment<>scope) then
    return jsonb_build_object('code','ENVIRONMENT_MISMATCH');
  end if;
  perform set_config('app.billing_environment',scope,true);
  if p_action='event_pending' then
    return jsonb_build_object('events',
      (select coalesce(jsonb_agg(jsonb_build_object('provider',provider,
        'environment',environment,'eventId',event_id,'attempts',attempts,
        'replayMaterialCiphertext',replay_material_ciphertext)),'[]'::jsonb)
       from (select * from billing_private.billing_events
             where environment=scope and status in ('received','failed')
               and attempts < coalesce((p_data->>'maxAttempts')::integer,8)
             order by received_at
             limit coalesce((p_data->>'limit')::integer,20)) pending));
  end if;
  if p_action='reconcile_list' then
    return jsonb_build_object('chains',
      (select coalesce(jsonb_agg(jsonb_build_object('principal',sp.principal,
        'storeReferenceCiphertext',sp.store_reference_ciphertext,
        'productId',sp.product_id,'environment',sp.environment,
        'purchaseAccountToken',ae.purchase_account_token)),'[]'::jsonb)
       from billing_private.store_purchases sp
       join billing_private.account_entitlements ae on ae.principal=sp.principal
       where sp.environment=scope and sp.principal is not null
         and sp.store_status in ('active','billing_retry')));
  end if;
  result := public.billing_service_unscoped(p_action,p_data);
  if result ? 'code' then return result; end if;

  if p_action='entitlement' then
    -- The old RPC returns a stored projection. Refresh it for this deployment
    -- before returning, including after a sandbox-to-production cutover.
    if exists (select 1 from billing_private.purchase_devices where principal=actor) then
      perform billing_private.aggregate_entitlement(actor,scope);
    end if;
    result := public.billing_service_unscoped(p_action,p_data);
  end if;
  if p_action in ('entitlement','apple_verify') then
    result := jsonb_set(result,'{billingSources}',
      (select coalesce(jsonb_agg(jsonb_build_object('provider',sp.provider,
          'productId',sp.product_id,'expiresAt',billing_private.iso(sp.expires_at))),
          '[]'::jsonb)
       from billing_private.store_purchases sp
       join billing_private.purchase_devices pd on pd.purchase_id=sp.id
       where pd.principal=actor and pd.revoked_at is null
         and sp.environment=scope
         and sp.store_status in ('active','billing_retry')));
  end if;
  return result;
end $$;
revoke all on function public.billing_service(text,jsonb) from public,anon,authenticated;
grant execute on function public.billing_service(text,jsonb) to service_role;

-- The AI path also reads the stored entitlement projection. Reconcile a known
-- guest principal before charging, so it cannot spend sandbox member quota in
-- the production service even before the App opens its membership page.
alter function public.ai_quota_service(text,jsonb) rename to ai_quota_service_unscoped;
revoke all on function public.ai_quota_service_unscoped(text,jsonb) from public,anon,authenticated,service_role;

create function public.ai_quota_service(p_action text, p_data jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
  scope text := coalesce(p_data->>'billingEnvironment','sandbox');
  actor text := p_data->>'principal';
begin
  if scope not in ('sandbox','production') then
    raise exception 'BILLING_INVALID_ENVIRONMENT';
  end if;
  perform set_config('app.billing_environment',scope,true);
  if p_action in ('status','reserve','finish')
     and actor ~ '^guest_[a-f0-9]{24}$'
     and exists (select 1 from billing_private.account_entitlements
                 where principal=actor)
     and exists (select 1 from billing_private.purchase_devices
                 where principal=actor) then
    perform billing_private.aggregate_entitlement(actor,scope);
  end if;
  return public.ai_quota_service_unscoped(p_action,p_data);
end $$;
revoke all on function public.ai_quota_service(text,jsonb) from public,anon,authenticated;
grant execute on function public.ai_quota_service(text,jsonb) to service_role;
