-- Lower the lifetime free AI pool to 30 without resetting consumption.
-- Existing principals keep their used count; anyone already at or above 30
-- receives zero remaining calls.

alter table ai_private.principals alter column free_limit set default 30;

update ai_private.principals
set free_limit = 30
where free_limit is distinct from 30;

create function ai_private.enforce_free_limit_30() returns trigger
language plpgsql set search_path='' as $$
begin
  new.free_limit := 30;
  return new;
end $$;

create trigger enforce_free_limit_30
before insert or update of free_limit on ai_private.principals
for each row execute function ai_private.enforce_free_limit_30();

revoke all on function ai_private.enforce_free_limit_30() from public,anon,authenticated,service_role;

-- Billing may be queried before the account has made its first AI request.
-- Ensure that path creates the same 30-use quota principal instead of falling
-- back to the legacy value embedded in the billing response function.
create or replace function billing_private.ensure_account(target_user uuid)
returns billing_private.account_entitlements
language sql security definer set search_path='' as $$
  insert into billing_private.account_entitlements(user_id, purchase_account_token)
    values(target_user, gen_random_uuid())
    on conflict (user_id) do nothing;
  insert into ai_private.principals(id, user_id, support_code, free_limit)
    values(
      'account:' || target_user::text,
      target_user,
      'TF-' || upper(substr(encode(sha256(convert_to(target_user::text, 'UTF8')), 'hex'), 1, 4))
        || '-' || upper(substr(encode(sha256(convert_to(target_user::text, 'UTF8')), 'hex'), 5, 4)),
      30
    )
    on conflict (id) do nothing;
  select * from billing_private.account_entitlements where user_id = target_user;
$$;
