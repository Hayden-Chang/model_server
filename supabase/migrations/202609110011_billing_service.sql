-- Billing service entry point (account/cloud design §9.3). Mirrors the
-- ai_quota_service convention: one security definer RPC, service_role only,
-- a live account/session recheck on every call, and no caller-supplied
-- identity is ever trusted.

alter table billing_private.account_entitlements
  add column purchase_account_token uuid unique;

-- Stable per-account rows: the entitlement projection and the secret
-- purchaseAccountToken (§9.2) are created on first need and never rotated.
create function billing_private.ensure_account(target_user uuid)
returns billing_private.account_entitlements
language sql security definer set search_path='' as $$
  insert into billing_private.account_entitlements(user_id, purchase_account_token)
    values(target_user, gen_random_uuid())
    on conflict (user_id) do nothing;
  select * from billing_private.account_entitlements where user_id = target_user;
$$;

create function billing_private.iso(moment timestamptz) returns text
language sql immutable as $$
  select case when moment is null then null
    else to_char(moment at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') end
$$;

create function public.billing_service(p_action text, p_data jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
  actor text := p_data->>'principal'; uid uuid; sid uuid;
  row billing_private.account_entitlements; claim billing_private.billing_claims;
  token uuid; inserted integer; pool_limit integer; used_count integer;
begin
  if actor !~ '^account:[0-9a-fA-F-]{36}$' then
    return jsonb_build_object('code','ACCOUNT_REQUIRED');
  end if;
  uid := substring(actor from 9)::uuid;
  sid := (p_data->>'sessionID')::uuid;
  if not exists(select 1 from auth.users u join auth.sessions s on s.user_id=u.id
    where u.id=uid and s.id=sid and u.email_confirmed_at is not null and not coalesce(u.is_anonymous,false))
    or exists(select 1 from sync_private.accounts where user_id=uid and deletion_pending) then
    return jsonb_build_object('code','ACCOUNT_UNAVAILABLE');
  end if;

  if p_action='claim_register' then
    row := billing_private.ensure_account(uid);
    token := row.purchase_account_token;
    insert into billing_private.billing_claims(claim_id,user_id,provider,product_id,
        expected_account_identifier_hash,request_hash)
      values((p_data->>'claimId')::uuid,uid,p_data->>'provider',p_data->>'productId',
        encode(sha256(convert_to(token::text,'UTF8')),'hex'),
        encode(sha256(convert_to((p_data->>'claimId') || uid::text || (p_data->>'provider')
          || (p_data->>'productId'),'UTF8')),'hex'))
      on conflict (claim_id) do nothing;
    get diagnostics inserted = row_count;
    select * into claim from billing_private.billing_claims where claim_id=(p_data->>'claimId')::uuid;
    if inserted = 0 and (claim.user_id is distinct from uid
        or claim.provider is distinct from p_data->>'provider'
        or claim.product_id is distinct from p_data->>'productId') then
      return jsonb_build_object('code','CLAIM_CONFLICT');
    end if;
    return jsonb_build_object('claimId',claim.claim_id,'appAccountToken',token,
      'expiresAt',billing_private.iso(claim.created_at + interval '1 hour'));
  end if;

  if p_action='claim_get' then
    select * into claim from billing_private.billing_claims where claim_id=(p_data->>'claimId')::uuid;
    if not found then return jsonb_build_object('code','CLAIM_NOT_FOUND'); end if;
    if claim.user_id is distinct from uid then
      return jsonb_build_object('code','CLAIM_CONFLICT');
    end if;
    return jsonb_build_object('claimId',claim.claim_id,'status',claim.status,
      'provider',claim.provider,'productId',claim.product_id,
      'purchaseKeyHash',claim.purchase_key_hash,
      'entitlementRevision',claim.result_entitlement_revision);
  end if;

  if p_action='entitlement' then
    row := billing_private.ensure_account(uid);
    -- Free-pool remainder lives in the shared AI ledger for the account principal.
    select coalesce(free_limit,50) into pool_limit from ai_private.principals where id=actor;
    select coalesce(used,0) into used_count from ai_private.buckets where principal=actor and period='free';
    return jsonb_build_object('plan',row.plan,'status',row.status,
      'validUntil',billing_private.iso(row.valid_until),
      'serviceEndAt',billing_private.iso(row.service_end_at),
      'entitlementRevision',row.entitlement_revision,
      'aiQuota',jsonb_build_object('limit',coalesce(pool_limit,50),
        'used',coalesce(used_count,0),
        'remaining',greatest(0,coalesce(pool_limit,50)-coalesce(used_count,0)),
        'resetsAt',jsonb 'null'),
      'billingSources',jsonb '[]');
  end if;

  raise exception 'BILLING_UNKNOWN_ACTION';
end $$;
revoke all on function public.billing_service(text,jsonb) from public,anon,authenticated;
grant execute on function public.billing_service(text,jsonb) to service_role;
revoke all on all functions in schema billing_private from public,anon,authenticated,service_role;
