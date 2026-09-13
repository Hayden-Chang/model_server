-- Billing ledger for Time Fragment Plus (account/cloud design §9.3). Only
-- trusted backend RPCs added by later migrations may read or mutate these
-- tables; every role is revoked here so no client path and no direct
-- service_role path can touch them, matching the ai_private convention.

create schema billing_private;
revoke all on schema billing_private from public, anon, authenticated, service_role;
alter default privileges in schema billing_private revoke execute on functions from public;

-- Verified store purchases. One row per (provider, environment, purchase
-- chain). user_id is the uniquely bound Time Fragment account. Account
-- deletion keeps the row with user_id null so the same purchase chain can
-- never silently rebind to another account; re-binding requires the trusted
-- customer-support path.
create table billing_private.store_purchases (
  id bigint generated always as identity primary key,
  provider text not null check(provider in ('apple','google')),
  environment text not null check(environment in ('production','sandbox')),
  purchase_key_hash text not null check(length(purchase_key_hash) between 32 and 128),
  store_reference_ciphertext text not null check(length(store_reference_ciphertext) between 1 and 4096),
  user_id uuid references auth.users(id) on delete set null,
  product_id text not null check(length(product_id) between 1 and 200),
  store_status text not null check(length(store_status) between 1 and 64),
  expires_at timestamptz,
  last_store_event_at timestamptz,
  acknowledgement_state text not null default 'not_required'
    check(acknowledgement_state in ('not_required','pending','acknowledged')),
  ack_deadline_at timestamptz,
  ack_attempts integer not null default 0 check(ack_attempts >= 0),
  last_ack_attempt_at timestamptz,
  last_ack_error_code text,
  updated_at timestamptz not null default now(),
  unique(provider, environment, purchase_key_hash)
);

-- Idempotent purchase claims registered before the store sheet is presented.
-- request_hash binds the claim to one account, product and flow so a claim
-- cannot be replayed for a different account, product or transaction.
create table billing_private.billing_claims (
  claim_id uuid primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null check(provider in ('apple','google')),
  product_id text not null check(length(product_id) between 1 and 200),
  expected_account_identifier_hash text not null check(length(expected_account_identifier_hash) between 32 and 128),
  request_hash text not null check(length(request_hash) between 32 and 128),
  status text not null default 'pending' check(status in ('pending','verified','rejected')),
  purchase_key_hash text check(length(purchase_key_hash) between 32 and 128),
  result_entitlement_revision bigint,
  created_at timestamptz not null default now(),
  completed_at timestamptz
);
create index billing_claims_user on billing_private.billing_claims(user_id, created_at);

-- Store notifications, deduplicated by (provider, environment, event_id).
-- Replay material is stored encrypted under a separate key with the shortest
-- viable retention and never enters logs or analytics.
create table billing_private.billing_events (
  id bigint generated always as identity primary key,
  provider text not null check(provider in ('apple','google')),
  environment text not null check(environment in ('production','sandbox')),
  event_id text not null check(length(event_id) between 1 and 200),
  payload_hash text not null check(length(payload_hash) between 32 and 128),
  replay_material_ciphertext text not null check(length(replay_material_ciphertext) between 1 and 8192),
  purchase_key_hash text check(length(purchase_key_hash) between 32 and 128),
  store_event_at timestamptz,
  status text not null default 'received' check(status in ('received','processing','processed','failed')),
  attempts integer not null default 0 check(attempts >= 0),
  last_error_code text,
  received_at timestamptz not null default now(),
  processed_at timestamptz,
  unique(provider, environment, event_id)
);
create index billing_events_pending on billing_private.billing_events(status, received_at);

-- Derived account-level projection of all bound purchase chains. Never a
-- single store source of truth: every change re-aggregates inside an account
-- lock (aggregation function arrives with the verify migration).
create table billing_private.account_entitlements (
  user_id uuid primary key references auth.users(id) on delete cascade,
  plan text not null default 'free' check(plan in ('free','plus')),
  status text not null default 'expired' check(status in ('active','grace','expired','revoked')),
  valid_until timestamptz,
  service_end_at timestamptz,
  premium_backup_retention_until timestamptz,
  entitlement_revision bigint not null default 0,
  updated_at timestamptz not null default now()
);

alter table billing_private.store_purchases enable row level security;
alter table billing_private.billing_claims enable row level security;
alter table billing_private.billing_events enable row level security;
alter table billing_private.account_entitlements enable row level security;
revoke all on all tables in schema billing_private from public,anon,authenticated,service_role;
revoke all on all sequences in schema billing_private from public,anon,authenticated,service_role;
