-- Rollback of 202609110009_contract_v2.sql: removes the registered schema v2
-- contracts so the server serves the pre-v2 (schema v1) contract set again.
--
-- Run manually against the production database (psql) when a deployment fails
-- and the server itself must be reverted. This is the exact inverse of the
-- v2 migration, which only inserted two rows into sync_private.contracts.
--
-- Safety gate: refuses to run once any device has durably written a v2 state
-- (user_sync_state.schema_version = 2). Rolling back with v2 data present
-- would strand those devices: their pulls return v2 states that v1 clients
-- cannot decode, and their v2 pushes would be rejected for a missing
-- contract. If v2 data exists, roll forward (fix forward) or migrate the
-- stored states first instead.
--
-- Effects after rollback:
--   * v1 clients: unaffected.
--   * v2 clients (the app built from this milestone): sync is rejected until
--     the contracts are re-registered (202609110009_contract_v2_reregister.sql).
--     The app itself keeps working offline; local data is preserved.

do $$
declare
    v2_state_count bigint;
begin
    select count(*) into v2_state_count
    from sync_private.user_sync_state
    where schema_version = 2;

    if v2_state_count > 0 then
        raise exception 'rollback refused: % sync space(s) already hold schema v2 states; roll forward or migrate states first', v2_state_count;
    end if;
end
$$;

delete from sync_private.contracts where name in ('cloud-state-v2', 'operation-v2');

-- Verification: only the v1 rows remain.
-- select name from sync_private.contracts order by name;
