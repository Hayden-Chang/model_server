-- Rollback of 202609170017_device_principal_billing.sql: restores the
-- auth.users-keyed billing tables.
--
-- Run manually against the production database (psql / Supabase SQL editor) when
-- the device-principal rollout must be reverted. This is the inverse of the
-- forward migration's data mapping: every account-owned principal is
-- 'account:' || user_id::text, so the reverse is substring(principal from 9).
--
-- Safety gate (fail-closed): once a device principal exists the reverse mapping
-- is undefined -- there is no user_id to recover from 'guest_<24hex>' -- so the
-- rollback refuses to run instead of silently discarding or corrupting those
-- rows. If device principals exist, roll forward (fix forward), or convert the
-- device-owned rows to account-owned ones first.
--
-- Scope: this script reverts the schema and the data mapping only. The replaced
-- function bodies are NOT duplicated here (copying ~200 lines of SQL across
-- files guarantees drift, and the repository already restores function bodies
-- from git this way -- see scripts/billing-rollback.sh). After this script
-- succeeds, restore the pre-M4 definitions of these three functions and apply
-- the result:
--   public.billing_service(text,jsonb) and
--   ai_private.quota_status(text,boolean,integer)   -> from 202609140016_guest_free_pool.sql
--   billing_private.ensure_account(uuid)            -> from 202609130015_free_quota_30.sql
--   billing_private.aggregate_entitlement(uuid)     -> from 202609110012_billing_verify.sql
-- e.g. `git show <pre-migration-sha>:supabase/migrations/<file>` and apply the
-- relevant function bodies. Until they are restored the RPC still references the
-- renamed columns and fails at call time, so the code rollback in
-- scripts/billing-rollback.sh must be applied in the same window.
--
-- NOT reverted: the A4 unification to free_limit 30 (column default, backfill and
-- the enforce_free_limit_30 trigger). That is a product decision which applies
-- regardless of whether membership is account- or device-keyed, so the rollback
-- deliberately leaves the 30 limit in place instead of restoring the live 50.

do $$
begin
    if exists (select 1 from billing_private.purchase_devices where principal !~ '^account:')
      or exists (select 1 from billing_private.billing_claims where principal !~ '^account:')
      or exists (select 1 from billing_private.account_entitlements where principal !~ '^account:')
    then
        raise exception 'DEVICE_PRINCIPAL_DATA_PRESENT: rollback refused, guest_* principals already own billing rows';
    end if;
end
$$;

-- Dropped rather than replaced: their text parameter types cannot be changed in
-- place, and their pre-M4 uuid bodies are restored from git after this script.
drop function billing_private.ensure_account(text);
drop function billing_private.aggregate_entitlement(text);

drop table billing_private.purchase_devices;

alter table billing_private.store_purchases rename column principal to user_id;
alter table billing_private.store_purchases alter column user_id type uuid
  using case when user_id is null then null else substring(user_id from 9)::uuid end;
alter table billing_private.store_purchases
  add constraint store_purchases_user_id_fkey foreign key (user_id) references auth.users(id) on delete set null;

alter table billing_private.billing_claims rename column principal to user_id;
alter table billing_private.billing_claims alter column user_id type uuid
  using substring(user_id from 9)::uuid;
alter index billing_private.billing_claims_principal rename to billing_claims_user;
alter table billing_private.billing_claims
  add constraint billing_claims_user_id_fkey foreign key (user_id) references auth.users(id) on delete cascade;

alter table billing_private.account_entitlements rename column principal to user_id;
alter table billing_private.account_entitlements alter column user_id type uuid
  using case when user_id is null then null else substring(user_id from 9)::uuid end;
alter table billing_private.account_entitlements
  add constraint account_entitlements_user_id_fkey foreign key (user_id) references auth.users(id) on delete cascade;

-- Verification: every row maps back to a real account again.
-- select count(*) from billing_private.store_purchases sp
--   left join auth.users u on u.id = sp.user_id where sp.user_id is not null and u.id is null;
