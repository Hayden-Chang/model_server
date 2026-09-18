# AI quota scope: what "one quota shared across a user's devices" can mean here

**Status:** design and change-impact analysis. **No implementation, no migration, no test was
written for this document.** Every claim below was read out of the repository at the cited
`file:line`; anything I could not check is marked **[unverified]** in §6.

**Status update (2026-09-18) — the status line above is superseded by later implementation.** This
was a pure design when it was written; §2–§5 below stay as the 2026-09-17 record.

- **Landed.** E1/E2/E4 shipped in `supabase/migrations/202609170020_shared_member_quota.sql`: it adds
  `billing_private.plus_source`, and both `ai_private.quota_status` and the `reserve` path of
  `public.ai_quota_service` now read and write the chain owner's member bucket (`ai_private.buckets`,
  keyed by the resolved `scope` with `period = 'member:YYYY-MM-DD'`, row-locked before the
  remaining-check). The free layer is unchanged and still meters `ai_private.free_pools`.
- **Still open.** E5 remains recorded but not fixed (§5.0.1); E3 remains open.
- **The artifact names in §3/§4 are design codenames, not what shipped.** The proposed
  `202609180018_shared_member_quota.sql` shipped as `202609170020_shared_member_quota.sql`; no
  `202609180018_…_rollback.sql` was ever created (there is no `202609170020` rollback in
  `supabase/migrations/`); and `supabase/tests/member-shared-quota.test.mjs` does not exist — those
  cases landed in the existing `supabase/tests/ai-quota.test.mjs`, `supabase/tests/billing.test.mjs`
  and `supabase/tests/guest-signout-quota.test.mjs`.

- Repository: `model_server` (Supabase Postgres + FastAPI), worktree
  `/Volumes/mac2/codex-worktrees/model-server-quota-analysis-20260917`
- Branch `codex/quota-shared-account-analysis-20260917`, based on `origin/main` @ `d65825c`
  (worktree was created at `4dcf9cc`; `origin/main` advanced to `d65825c` — PR #54, "encrypt the
  stored reference on the webhook path and repair the retry mapping" — while this analysis was in
  progress. That commit adds one `SAFE_ERROR_CODES` entry and reworks `billing_worker.py`'s webhook
  retry path; it touches no migration, no quota function, and no bucket key. All `file:line`
  citations below were re-verified against `d65825c`.)
- iOS client read read-only from the primary checkout `/Volumes/mac2/projects/git_repo/run_self/time_fragment`
  at `origin/main` = `986619da` (`docs/architecture-module-ownership.md` lives in **that** repo, not here)
- Prod statements are quoted from repository documents and migration comments. **I ran no query
  against production** (no configured read-only path in this session — see §6).

---

## 0. Corrections to the brief's premises

Read this first; four premises in the brief are stale or wrong.

| # | Brief said | Actually |
| --- | --- | --- |
| C1 | `time_fragment_guest_quota_limit` defaults to **50** at `account_backend.py:76` | Default is **30**, at `account_backend.py:79` and `settings.py:20`. The only literal `50` left is the hardcoded *member* limit in the legacy SQLite store (`quota_store.py:134`, `:240`, `:266`) and stale user-facing strings (`account_backend.py:119`, `factory.py:672`). |
| C2 | `202609140016` has **never been applied** to production | Inverted. `202609140016` **is** the last applied migration; the one never applied is **`202609130015_free_quota_30`** (`202609170017…:31-40`). Live `free_limit` is therefore still 50. |
| C3 | `docs/architecture-module-ownership.md` exists in `model_server` | It does not. `git ls-tree -r origin/main` in `model_server` lists only `docs/architecture.md`. The ownership doc is `time_fragment:docs/architecture-module-ownership.md`. |
| C4 | The product documentation describes the quota as tracked **per account and shared across devices** | True only of the wording **before** iOS commit `986619da` (2026-09-17). Current docs say the opposite and explicitly retract it: `membership-plan.md:99` calls "所有设备共享" the *old* wording and records account/purchase-chain sharing as **待产品确认**. See §1.4 for both versions quoted. |

Two further findings change the shape of the decision rather than the premises:

- **C5 — the two quota code paths are mutually exclusive, and only one is live at a time.** The iOS
  app's only AI call is `/api/plan/parse` (§1.3). In the shipping Compose topology that route is
  served by `time-fragment-api` → Supabase RPC, while `business_api/app/quota_store.py` (the
  SQLite store) is bypassed by `PLANNING_INTERNAL_ONLY=true`. **The Supabase path is the only one
  worth changing**; `quota_store.py` matters only during an emergency rollback (§3.4).
- **C6 — a signed-in Plus member is metered as *free*.** `/api/plan/parse` prefers the Supabase
  account JWT over the guest token when the user is signed in (`AIPlanningClient.swift:185-212`),
  which resolves to principal `account:<uuid>`; but purchases can only bind to `guest_*`
  (`202609170017…:222-227`), so `account_entitlements` has no row for that principal and
  `quota_status` falls through to the free period. The Plus daily pool is only ever reached by a
  *signed-out* device. **Any sharing scheme must fix this or it will not be observable to the
  signed-in users it is meant for.** See §1.2 (path B step 4) and decision **E2**.

---

## 1. Verified current state

### 1.1 Which request path is live

| | Legacy path (A) | Account path (B) — **live** |
| --- | --- | --- |
| ASGI app | `business_api/app/main.py:5` → `factory.create_app` | `business_api/app/account_main.py:4` → `account_api.create_account_api` |
| Container | `business-api` | `time-fragment-api` (`docker-compose.accounts.yml:13-16`) |
| Quota authority | local SQLite `quota_store.py` | Supabase RPC `public.ai_quota_service` |
| Public routing | `Caddyfile:17` (no overlay) | `Caddyfile.accounts:18-26`: `/api/*`, `/billing/*`, `/webhooks/apple`, `/admin/time-fragment/*` → `time-fragment-api`; `/internal*` → 404 |

`docker-compose.accounts.yml:11` sets `PLANNING_INTERNAL_ONLY: "true"` on `business-api`, and
`factory.py:101-102` makes that instance answer **404 for every `/api/*` request**. The overlay is
opt-in and both files are required for cutover (`docs/account-api.md:77-83`), and
`scripts/billing-deploy.sh:22-25` starts both files together. So in the deployed shape the legacy
`/api/plan/parse` handler (`factory.py:266-450`) and its `QuotaStore` are dead code for public
traffic. `Caddyfile.accounts:14-17` additionally blocks `/internal*` from the internet, so the only
caller of `factory.py:452` is `backend.plan` (`account_backend.py:233`).

### 1.2 How quota is keyed and metered on every consuming path

**Path A — legacy SQLite (`quota_store.py`).** Tables created inline at `quota_store.py:70-103`.
Bucket key is `(principal, period_key)` with a partial unique index
`idx_quota_active_period … ON quota_buckets(principal, period_key) WHERE active = 1`
(`quota_store.py:108-111`); `period_key` is `'free'` or `'member:YYYY-MM-DD'` in Asia/Shanghai
(`quota_store.py:318-321`). Limit comes from `50 if self.membership_enabled(principal) else
self._default_limit` at **`quota_store.py:134`** (and again at `:240`, `:266`, `:313`); the gate is
`if used >= quota_limit` at **`quota_store.py:179`**. Membership is **development-only**: it returns
`False` unless the principal is in the `TIME_FRAGMENT_DEVELOPMENT_DEVICE_IDS` allowlist
(`quota_store.py:289-296`, seeded at `factory.py:77-85`). No purchase, no entitlement, no account
is ever consulted on this path. Default limit is injected from
`settings.time_fragment_guest_quota_limit` (`factory.py:81-85`, default 30 at `settings.py:20`).
Note the legacy `Settings` has **no** member-limit setting at all — the member limit is a literal
`50` inside the store (`quota_store.py:134`); `time_fragment_member_quota_limit` exists only in
`AccountAPISettings` (`account_backend.py:80`).
Referenced only by paths A and its tests (§1.6).

**Path B — Supabase ledger (live).** DDL:

- `ai_private.principals(id text primary key, user_id uuid unique, support_code, free_limit default 50,
  development_enabled, claimed, claimed_by, import_hash)` with
  `check ((user_id is null and id ~ '^guest_[a-f0-9]{24}$') or (user_id is not null and id = 'account:' || user_id::text))`
  — `202609090006_ai_quota.sql:12-22`. **A principal is either a device or an account, never both.**
- `ai_private.buckets(id, principal, period, used, unique(principal, period))` — `…:23-29`. **This is
  the meter; the key is `(principal, period)`.**
- `ai_private.requests(principal, request_id, body_hash, bucket_id, attempt, state, expires_at,
  primary key(principal, request_id))` — `…:30-39`. Receipts stay per principal even when the meter is shared.
- `ai_private.free_pools(id uuid primary key, used)` + `principals.free_pool_id` —
  `202609140016_guest_free_pool.sql:4-19`. **The free tier is already a shared counter**: many
  principals may point at one pool row, and `ai_private.update_free_pool` derives it from
  `buckets.used` deltas for `period='free'` (`…:32-43`). `claim` re-points a guest's `free_pool_id`
  at the account's pool and merges by maximum (`…:181-196`).
- `billing_private.purchase_devices(purchase_id, principal, bound_at, revoked_at,
  primary key(purchase_id, principal))`, index `purchase_devices_principal` —
  `202609170017_device_principal_billing.sql:98-105`. This is the purchase-chain device group.

The ledger is consumed through two service-role RPCs; the app never calls them directly
(`docs/account-api.md:33-34`, grants revoked at `202609140016:264-265` and `:471-472`).

*Step 1 — identity.* `account_api.py:133-140` (`actor` dependency): one dot in the token ⇒ guest HMAC
token ⇒ `GuestTokenCodec.verify` returns `sub` = `guest_<sha256(device_id)[:24]>`
(`guest_auth.py:57-58`); two dots ⇒ Supabase JWT ⇒ `backend.account` ⇒ `Actor("account:" + userID)`
(`account_backend.py:174-179`). `/billing/*` is deliberately the other way round:
`billing_device` (`account_api.py:181-195`) **rejects** any non-guest token with `DEVICE_REQUIRED`.

*Step 2 — limit.* `quota_status` (`202609170017…:171-201`) starts at `period_key := 'free';
quota_limit := p.free_limit` (`:178`), resolves membership by identity
`from billing_private.account_entitlements where principal = actor` (`:181-184`), and if
`ent_plan='plus' and ent_status in ('active','grace')` switches to
`period_key := 'member:' || ((instant at time zone ent_tz)::date)::text` with
`quota_limit := coalesce(member_limit,30)` (`:185-189`) and a `resetsAt` at the next local midnight
(`:188`, `:198-199`).

*Step 3 — used count.* `if period_key='free' then … from ai_private.free_pools where id=p.free_pool_id
else … from ai_private.buckets where principal=actor and period=period_key`
(`202609170017…:190-194`). **The free branch is shared; the member branch is per principal.** That
single predicate is the contradiction.

*Step 4 — consume.* `public.ai_quota_service` action `reserve`
(`202609140016…:222-251`): gate at `if (q->>'remaining')::integer=0` (`:241-242`, returning
`AI_DAILY_QUOTA_EXHAUSTED` for a member period) then
`insert into ai_private.buckets(principal,period,used) values(actor,q->>'period',1)
on conflict(principal,period) do update set used=ai_private.buckets.used+1` (`:244-245`).
Serialization for this check-then-write comes from
`select * into p from ai_private.principals where id=actor for update` (`:201`) plus the free-pool
row lock (`:199-200`). `finish` consumes or refunds through `r.bucket_id` (`:252-261`), and
`ai_private.expire_reservations` reclaims abandoned holds (`202609090006…:62-71`).
`member_limit` is supplied by the caller: `account_backend.py:185-186` sends
`freeLimit`/`memberLimit` from settings, both defaulting to **30** (`:78-79`).

**Downstream effects of C6.** Because the AI call presents `account:<uuid>` for a signed-in user,
step 2 finds no entitlement row and the request is metered against the *lifetime free* pool. The
free pool for that account is genuinely shared by all its signed-in devices — that is asserted
today by `supabase/tests/ai-quota.test.mjs:60` (`different sessions share 30 account calls while
another account stays independent`) — so the shipped state is: **free quota shared per account,
Plus quota per device, and a signed-in member never reaching the Plus quota at all.**

*Free-tier principal linking.* A guest joins an account's pool only through
`POST /api/account/claim-guest` (`account_api.py:171-179`), which sets `claimed`/`claimed_by` and
re-points `free_pool_id` (`202609140016…:170-197`). `principals.claimed_by` is the only
guest→account edge in the schema, and it exists only for the free pool.

### 1.3 What the iOS client actually sends

- **Guest auth.** `POST /api/auth/guest`, body `{"device_id": "<value>"}`, no `Authorization`
  header (`AIPlanningClient.swift:145-147`, `:221-225`, `:241-248`). `device_id` is
  `AIPlanningDeviceIdentity.current()` = `UserDefaults` key `ai-planning.guest-device-id`, value
  `time-fragment-ios-<lowercase-uuid>`, minted on first use and **not stable across reinstall**
  (`AIPlanningClient.swift:12-23`; injected at `TimeFragmentApp.swift:352`/`:355`). It is *not*
  `UIDevice.identifierForVendor` and *not* the Keychain `SyncDeviceIdentity`, which is a separate
  identity used only by sync (`CloudSyncAccountServices.swift:542-587`).
- **Planning.** `POST /api/plan/parse`, body keys exactly
  `text, requestID, baseFingerprint, currentPlan, now` (+ optional `earliestStartSlot`)
  (`AIPlanningV2ParseRequest`, `AIPlanningModels.swift:193-200`). **No device id in the body and no
  `X-Device-ID` header** — identity rides only in `Authorization: Bearer <token>`
  (`AIPlanningClient.swift:245-247`). The server derives the principal from the token.
- **Which token.** `authenticatedSend` tries the Supabase session access token first and only falls
  back to the guest token (`AIPlanningClient.swift:185-212`), wired at `TimeFragmentApp.swift:473-479`
  to `sessionCoordinator.validAccessToken()`. This is the mechanism behind C6.
- **Billing.** `/billing/claims`, `/billing/entitlement`, `/billing/apple/verify` always carry the
  **guest** device token, never the account JWT (`EntitlementStore.swift:29-33`;
  `TimeFragmentApp.swift:493-495`). `appAccountToken` is minted server-side per device principal and
  handed to StoreKit (`MembershipEntitlementModels.swift:36-40` → `EntitlementStore.swift:100-101`).
- **Legacy route.** The app has **no** call to `/v1/pipelines/…:run` and no `X-Device-ID`
  (explicit negative grep over the whole `origin/main` tree). This is what makes C5 safe.
- **Counter.** The membership page renders `entitlement.aiQuota.{limit,remaining}` with
  `"今日剩余 %d/%d"` for Plus and `"免费剩余 %d/%d"` for free
  (`SettingsPage.swift:294-301`, `MembershipPage.swift:60-65`), with an in-memory fallback of
  `30/30` when the fetch has not succeeded (`MembershipEntitlementModels.swift:17-20`). The only
  refresh trigger is the settings surface appearing (`SettingsFeatureHost.swift:70`, `:108-114`).

### 1.4 Where the "per account / shared across devices" wording lives, verbatim

**Current text in the iOS repo (`origin/main`, `986619da`) — per device, and explicitly retracting "shared":**

`docs/membership-plan.md:99`:
> - 每个会员每天 30 次。⚠ **计量口径按设备主体**：设备主体模型下 `ai_private.buckets` 以 `principal` 为键，同一会员的多台设备各自 30 次/天，**不是**本文旧口径的「所有设备共享」；是否改为按账号/购买链维度共享属**待产品确认**项（见 [订阅链路实现设计](subscription-client-design.md) §5）。

`docs/subscription-client-design.md:216`:
> - 会员账本：**设备主体**每日 30 次，按该主体权益行上的 `account_entitlements.account_timezone`（默认 `Asia/Shanghai`）自然日 `00:00` 重置，服务端唯一权威。⚠ **额度按主体（= 按设备）计量**：`ai_private.quota_status` 用 `select used from ai_private.buckets where principal=actor`，因此同一会员的多台设备各自拥有 30 次/天，**不再跨设备共享**（membership-plan §3.3 写的「所有设备共享」是旧账号模型的口径，属**待产品确认**项，见下）。

Also current: `docs/membership-plan.md:42` ("**会员权益绑定本机设备主体，与 Time Fragment 账号无关**"),
`docs/account-cloud-sync-design.md:492` ("会员权益主体是**设备主体**…不是 Time Fragment 账号"),
`docs/membership-plan.md:91-93` (free = lifetime 30, claim takes max).

**The "per account / shared" wording the brief quotes — removed in the same commit.**

```
$ git show 986619da^:docs/membership-plan.md | sed -n '92p'
- 每个会员账号每天 30 次，所有设备共享。

$ git show 986619da^:docs/subscription-client-design.md | sed -n '201p'
- 会员账本：`user principal` 每日 30 次，按账号时区自然日 `00:00` 重置、跨设备共享、服务端唯一权威（membership-plan §3.3/§3.4）。
```

Two surviving "shared" claims that contradict the current rule, for completeness:

- `docs/account-cloud-sync-design.md:18` — "Time Fragment 账号同时承载会员权益…向该账号授予同一份跨设备权益"，
  and `:447` — "同一账号的其他设备从 API 读取相同 entitlement". These sit in the account-model
  sections; §9.1 of the same file (`:492`) supersedes them for the entitlement subject.
- User-visible copy: `MembershipPaywallView.swift:235` —
  "每日 30 次 AI 规划已在全部设备生效，拼图碎片奖励对所有用户相同。"

**Consequence for this task:** the product owner's decision resolves a question the documentation
deliberately left open (`待产品确认`); it does **not** merely re-align code with already-settled
text. No document in either repo records the resolution yet (§3.2, docs items).

### 1.5 Account-dimensioned data that could serve as a sharing key

| Candidate | DDL / location | Can it be "the user"? |
| --- | --- | --- |
| Supabase auth user | `auth.users(id)`; referenced by `principals.user_id` (`202609090006…:14`) | Yes as a referent, but no device can *reach* it as a billing subject: the `check` at `:21` and the gate at `202609170017…:222-227` mean an `account:<uuid>` principal can never own a purchase. |
| `ai_private.principals` | `202609090006…:12-22` | Holds both shapes (`guest_*`, `account:*`). `claimed_by` (`:19`) is the only guest→account edge, and only the free pool uses it. |
| `ai_private.free_pools` + `free_pool_id` | `202609140016…:4-19` | **Already a working sharing mechanism** for the free tier, membership defined by `claim`, not by a device list. |
| `billing_private.purchase_devices` | `202609170017…:98-105` | **The only server-authoritative device group for a purchase**, login-free, bounded at 3 active rows per chain (`:311-317`). |
| `billing_private.store_purchases` | `202609110010…:12-29`, renamed `user_id`→`principal` at `202609170017…:79-82` | Chain identity: `purchase_key_hash = sha256('apple|'||originalTransactionId)`, unique per `(provider, environment, hash)`. Chain owner = `principal` fixed at first INSERT (`:319-321`). |
| `billing_private.account_entitlements` | `202609110010…:68-77` + `account_timezone` (`202609110014…:7-9`) + `purchase_account_token` (`202609110011…:6-7`); `user_id`→`principal` at `202609170017…:90-93` | Per-principal projection: `plan`, `status`, `account_timezone`, `entitlement_revision`. Membership is read from here. |
| Any `account_id` column in the quota ledger | — | Does not exist. `ai_private.buckets` and `ai_private.requests` have no account column. |

### 1.6 Documentation vs. code

- `model_server:docs/architecture.md:258-260` says "持久配额…均未实现" and `:436-437` lists
  "按调用方持久化的配额" as a non-goal. **Both are stale**: path A implements a persistent SQLite
  quota (`quota_store.py`), path B implements the Postgres ledger. `:350` ("Time Fragment V2 没有
  …每设备配额") is likewise stale. The doc predates the quota work and was not updated with it.
- `model_server:docs/architecture.md:53` puts "付费额度" out of scope for the observability layer;
  that still holds (`usage_store.py` has no quota columns).
- `docs/development-membership.md:5` ("Members receive 50 successful AI planning requests per
  calendar day") describes **path A's development membership only**, consistent with
  `quota_store.py:134`. It does not describe Plus, and its 50 is the number the brief mis-attributed
  to the guest limit.
- `docs/account-api.md:17` likewise scopes the 50/day to the development toggle, "not a paid subscription".
- `docs/account-api.md:20-24` ("Accounts and guests have 30 lifetime free calls… links both
  identities to one free pool… does not… expose the account's Plus entitlement") matches the code,
  and is the clearest existing statement of the free-tier sharing rule.
- `docs/account-api.md:33-36` and `supabase/README.md:50` ("Once a migration is deployed, add a new
  migration") are consistent with what I found.

---

## 2. What "shared across a user's devices" can mean — options and honest costs

**Two constraints apply to every option** and are stated once here rather than repeated:

1. **A `device_id` is client-asserted, and a guest JWS is a copyable bearer credential.** The
   server hashes whatever the client declares (`guest_auth.py:57-58`) and never authenticates device
   ownership; the migration says so itself (`202609170017…:6-10`), and the iOS identity is a
   `UserDefaults` string with no hardware binding (`AIPlanningClient.swift:12-23`). The Keychain
   identity is `ThisDeviceOnly`, which prevents *copying between devices*, not forging
   (`CloudSyncAccountServices.swift:542-556`). **No option below is secure or tamper-proof; every
   one is a best-effort product control.**
2. **Sharing converts an entitlement-theft problem into an availability problem.** Today a forged or
   borrowed `device_id` lets an attacker *use* someone's Plus without reducing the owner's
   allowance. With a shared counter, the same forgery lets the attacker *drain the whole group's
   daily 30* and lock the legitimate devices out. This is an inherent, unavoidable consequence of
   the product semantics, not a defect of any particular option.

### 2.1 Comparison

| | (i) Supabase account | (ii) Purchase chain — **recommended** | (iii) Client-asserted group | (iv) Do nothing, re-label docs |
| --- | --- | --- | --- | --- |
| **Referent for "the user"** | `account:<uuid>` (or `claimed_by`) | the active purchase chain | a `group_id` the client sends | none |
| **Sharing key physically** | `ai_private.principals.id` | chain owner principal = `store_purchases.principal` | new `principals.group_id text` | — |
| **Storage** | none new — `buckets(principal, period)` as-is | none new if the meter is re-keyed to the chain owner; a dedicated `member_pools` table is the alternative | new column (+ backfill) and a new field in `/api/auth/guest` | none |
| **Signed-out devices** | each is its own `guest_*` principal ⇒ **not shared** | shared, if bound to the chain | shared by assertion | not shared |
| **Free tier** | already shared this way today (verified: `ai-quota.test.mjs:60`) | **not covered** — no purchase exists; stays per `free_pool_id`/claim | could cover both tiers | not shared for a device that never claimed |
| **Unbind / refund / revoke** | nothing to revoke; account deletion cascades the principal | falls out of `purchase_devices.revoked_at`; revoked device drops back to its own free pool | attacker keeps the group id; no revocation surface | — |
| **3-device cap** | independent axis — a chain is chain-scoped, an account is not; the two can disagree | **the cap becomes the sharing boundary**, so quota breadth is bounded by an already-enforced limit | unrelated; group size unbounded | unrelated |
| **Abuse surface** | account takeover / shared passwords drains the group | forged `device_id` on a bound chain drains the group (≤3 devices) | **worst**: any client can assert any group id and drain a stranger's quota with no receipt or entitlement gate | none new |
| **Needs login to buy?** | **yes — reverses the merged M4 decision** (`202609170017…:222-227`, `MembershipPaywallView.swift:7-8`, UI test asserts no login gate) | no | no | no |
| **Client can ship independently?** | no (re-introduces a login gate + UI test rewrite) | **yes — server-only** | no (new wire field requires a coordinated release) | n/a |

### 2.2 Detail

**(i) Share by Supabase account.** The referent exists and already works for free: all signed-in
devices of one account resolve to the same `account:<uuid>` principal and therefore the same
`free_pool_id`. Extending it to Plus means letting an account own an entitlement, which the merged
model forbids in two places (the `check` at `202609090006…:21` and the gate at
`202609170017…:222-227`). The workarounds are all bad: re-opening account purchases reverses M4 and
restores "sign in to buy" (contradicting `membership-plan.md:42` and the shipped paywall), or
aggregating a signed-in user's *claimed* devices' entitlements, which covers only devices that ran
`claim-guest` and is ambiguous when several claimed devices hold different chains. Cost: high, and
it buys nothing that (ii) does not, except covering mixed signed-in/signed-out device sets.

**(ii) Share by purchase chain — recommended.** The referent is the device group the billing model
already maintains: `purchase_devices` rows with `revoked_at is null`, capped at 3 per chain
(`202609170017…:98-105`, `:311-317`). It needs no login, preserves every M4 decision, and makes the
3-device cap do double duty as the quota-sharing boundary, so "how many devices can share this
purchase" has exactly one answer. **The free-tier gap is real and must be stated to the product
owner: a free user has no purchase, so this option says nothing about the free tier.** For free, the
existing rule stays — lifetime 30 per principal, shared through `free_pools` after a
`claim-guest` — which means **two signed-out devices of the same free user do not share** unless the
user signs in and claims. Recommendation: accept and document that, because the free pool is
lifetime (not a recurring cost) and merging is already implemented; escalate it as decision **E4**.

**(iii) Client-asserted group id / hybrid.** Cheapest to *specify* and worst to *operate*: with no
receipt and no entitlement behind the group, an attacker who learns or guesses a group id joins it
and consumes the victim's allowance (constraint 2, unbounded). It also requires a new field on
`/api/auth/guest`, so the client can no longer ship independently, and it needs a backfill for
existing `principals` rows. The only variant worth considering is "account when signed in, chain
otherwise" — but that is (i) ∪ (ii), i.e. strictly more work than the recommendation, and §3.1
already reaches the same user-visible result using only server-authoritative referents.

**(iv) Do nothing; re-label the product text.** State it plainly: members currently get 30/day
**per device**, so a 3-device member gets up to 90/day and a 1-device member gets 30. Re-labelling is
not user-harmful in the direction one might fear — it is *more* generous than the promise, and the
cost scales with device count, which is the opposite of a cost control. The two decisive objections
are (a) it contradicts the product owner's explicit decision, and (b) it leaves the tiers using
**different referents**: free quota is already shared per account while Plus quota is per device, so
a user who logs in sees their free allowance shared across devices and their paid allowance not.
That inconsistency is the strongest argument against (iv) and, by the same token, the reason the
recommendation must also fix C6.

---

## 3. Recommended option (ii) — required changes

### 3.1 Semantics to implement

> The daily Plus allowance is one counter per purchase chain, shared by that chain's active member
> devices, reset at local midnight in the chain's `account_timezone`.

Resolution rule, defined **once** so every reader agrees (this is the part that must not be
duplicated in five places):

1. Determine the *billing subject* for the acting principal `actor`:
   - `actor = guest_*`: the subject is `actor` itself.
   - `actor = account:<uuid>`: the subject is the best Plus source among `actor` and every principal
     with `claimed_by = uuid` — ordered by plus-before-free, `active` before `grace`, then
     `expires_at desc nulls last`, matching the ordering already used by
     `billing_private.aggregate_entitlement` (`202609170017…:149-155`). If none has Plus, the
     request stays on the free period and the daily pool is not used.
2. The **meter scope** is the owning principal of that subject's chain:
   `store_purchases.principal` for the chain the subject is an active member of
   (`purchase_devices.revoked_at is null`). The chain owner is stable — `202609170017…:319-321`
   deliberately never rewrites it.
3. Read and write the member bucket at `(scope, period)`, not at `(actor, period)`. Receipts
   (`ai_private.requests`) remain keyed on `actor`, so idempotency, body-hash binding and refunds
   stay per device and continue to work through `bucket_id`.

This is what makes a signed-in device A and a signed-out device B that are bound to the same
purchase share one daily 30 — the only reading of "one quota shared across a user's devices" that
covers the mixed sign-in states a real user has.

### 3.2 File-by-file change list

**`model_server` — new migration `supabase/migrations/202609180018_shared_member_quota.sql`**

Next number is `…0018`; the 4-digit suffix is a single globally increasing counter
(`…0001`–`…0017`), not a per-day sequence, and the date prefix is the authoring date. Only the
lexical order after `202609170017` matters. Must be applied **after** `202609170017` (§3.5).

| # | Object | Action |
| --- | --- | --- |
| 1 | `billing_private.plus_source(p_actor text) returns table(plan text, status text, account_timezone text, scope text)` | **new** (`create or replace`, `security definer`, `set search_path=''`). Implements §3.1 steps 1–2. Reads `billing_private.account_entitlements`, `billing_private.store_purchases`, `billing_private.purchase_devices`, `ai_private.principals`. Optionally also requires `store_status in ('active','billing_retry')` and a non-expired `expires_at` at read time instead of trusting the projection — see **E5**, this would also narrow the accepted D5 gap. **Not implemented as designed:** the shipped `billing_private.plus_source(p_actor text)` returns `table(account_timezone text, scope text)` only — the proposed `plan`/`status` return columns were dropped; the `ae.plan = 'plus' and ae.status in ('active','grace')` gate lives in the function body instead (see `supabase/migrations/202609170020_shared_member_quota.sql`). |
| 2 | `billing_private.member_scope(p_actor text) returns text` | new thin wrapper selecting `scope` from (1), or fold into (1). Keeps call sites short. |
| 3 | `ai_private.quota_status(actor text, dev_allowed boolean, member_limit integer)` | `create or replace` (`202609170017…:171-201` shape). Replace the membership lookup at `:181-184` with `plus_source(actor)`, and replace `where principal=actor` at `:193` with `where principal = coalesce(scope, actor) and period = period_key`. `resetsAt` still comes from the entitlement timezone. |
| 4 | `public.ai_quota_service(p_action text, p_data jsonb)` | `create or replace`. In `reserve` (`202609140016…:222-251`), take `plus_source(actor)` and write the bucket at `coalesce(scope, actor)` at `:244-245`. **Take a row lock on the shared meter before the remaining-check** — `perform 1 from ai_private.buckets where principal = scope and period = period_key for update` (after an idempotent `insert … on conflict do nothing`), or lock `ai_private.principals where id = scope for update`, mirroring the free-pool lock at `:200`. Without this, two devices on one chain can each pass the `:241-242` check at `remaining = 1` and overspend, because `:201` only serializes requests from the *same* actor. In `admin_reset` (`:212-221`) also zero the scope's member buckets. |
| 5 | `public.billing_service(p_action text, p_data jsonb)` | `create or replace`. `entitlement` (`202609170017…:370-392`) and `apple_verify` (`:346-367`) currently return `aiQuota` from the **free** pool with `resetsAt: jsonb 'null'` (`:351-353`, `:358-361`). Return the member daily bucket when the resolved plan is plus, with `resetsAt` from the same rule as `quota_status`, otherwise the free pool as today. Without this the membership page cannot display the shared number (§3.6). |
| 6 | `public.ai_quota_export_legacy()` | `create or replace` (`202609140016…:475-509`): member rows can now live under a scope the exporting principal does not own. Decide explicitly (see **E3**); the safe default is to export the scope's member bucket alongside the owner's. |
| 7 | `revoke`/`grant` for every object above | `revoke all on function … from public,anon,authenticated,service_role` for `billing_private.*` (matching `202609170017…:460`), and `revoke … from public,anon,authenticated` + `grant execute … to service_role` for the two `public` RPCs (matching `202609140016…:264-265`, `:471-472`). `202609110010`'s blanket revoke is one-shot and does not cover objects created later — `202609170017…:106-107` records this trap. |
| 8 | `billing_private.purchase_devices` index | Optional `create index if not exists purchase_devices_active on billing_private.purchase_devices(principal) where revoked_at is null`, since `plus_source` filters on it on every quota read. |
| 9 | Guarded data block | `do $$ … $$` that (a) `raise notice` on the count of `ai_private.buckets where period like 'member:%'`, and (b) **fails closed** if such rows exist and no scope resolves, so the migration never silently orphans usage. |

**Idempotency.** Every object is `create or replace` / `create index if not exists` /
`do $$ … $$` guarded; no `alter table` and no data rewrite is required by the recommended
mechanism, so the migration is re-runnable. (This is the main reason to re-key the existing meter
rather than add a table + trigger + backfill.)

**Size warning.** Postgres cannot modify a plpgsql body in place, so items 4 and 5 re-emit roughly
225 + 250 lines. This migration will land around 550–650 lines and **exceed the repository's
≈400-production-line PR convention**. The device-principal design already records an approved
exception for exactly this reason (`membership-device-principal-implementation-design-20260917.md`,
"关于 ≤400 行的一处例外"), and the same justification applies verbatim.

**Tests — `supabase/tests/`**

| File | Change |
| --- | --- |
| `supabase/tests/member-shared-quota.test.mjs` | **new.** Cases: (a) two `guest_*` principals joined to one chain via `billing_service('apple_verify')` report the same `used`/`remaining` from `quota_status`; (b) `reserve`+`finish(consume=true)` from A reduces B's `remaining`; (c) a refund from B restores both; (d) a device not on the chain is unaffected; (e) a revoked device falls back to the free period; (f) `resetsAt` is identical for both; (g) a signed-in `account:<uuid>` whose claimed device holds Plus reaches the member period (the C6 regression); (h) **concurrency**: at `remaining = 1`, simultaneous reserves from A and B yield exactly one success and `used` never exceeds the limit. |
| `supabase/tests/billing.test.mjs` | extend the device-cap cases (`:159-275`) so the chain's members are asserted to share one meter; extend `:140` so `entitlement` returns the member pool for a plus principal. |
| `supabase/tests/guest-signout-quota.test.mjs` | keep `:81` ("a device principal with a plus projection gets the member period") but change its meaning under sharing, and add the account-actor case. |
| `supabase/package.json` | **add the new file to the `test:sync` script.** `test:sync` currently lists protocol, sync, deletion-worker, maintenance, background-worker, billing, billing-device-migration — it does **not** run `ai-quota.test.mjs` or `guest-signout-quota.test.mjs`. A new file added to `supabase/tests/` will silently never run in CI otherwise. |
| `supabase/tests/database.mjs` | if a `202609180018_…_rollback.sql` is added, append it to the `recoveryScripts` set at `:44-46`, or the harness will auto-apply the rollback as a forward migration. |

**Python — `business_api/`**

| File | Change |
| --- | --- |
| `app/account_api.py` | none required — the routes and the device/account split are unchanged. |
| `app/account_backend.py` | none required — `freeLimit`/`memberLimit` forwarding at `:185-186` is unchanged; both settings already default to 30 (`:79-80`). |
| `tests/test_billing_api.py` | extend `test_entitlement_returns_plan_status_and_free_pool` (`:196`) to cover the plus case; add a case asserting the RPC body still carries only `principal` and no `sessionID` (the invariant `:265` protects). |
| `app/quota_store.py` | **not changed.** See §3.4 for why, and what that costs on rollback. |

**Docs — `model_server`**

| File | Change |
| --- | --- |
| `docs/account-api.md` | add a "Membership quota scope" section stating the chain-shared rule and the free-tier rule; correct `:20-24` to spell out that free sharing is per `free_pool_id`; record the C6 fix. |
| `docs/architecture.md` | fix `:258-260`, `:350`, `:436-437` — they still claim no persistent/per-device quota exists. |
| `docs/development-membership.md` | scope `:5`'s 50/day explicitly to path A's development toggle, and note that path A is inactive under the accounts overlay. |
| `docs/ai-quota-shared-scope-analysis-20260917.md` | this file. |

**Docs — `time_fragment` (iOS repo; separate PR, and I did not modify that repo)**

| File | Change |
| --- | --- |
| `docs/membership-plan.md:99` | replace the `待产品确认` warning with the decided rule (chain-shared for Plus, pool-shared for free), and state the free-tier gap honestly. |
| `docs/subscription-client-design.md:215-216` | same; `:216`'s "**不再跨设备共享**" becomes wrong. |
| `docs/account-cloud-sync-design.md:18`, `:447` | reconcile with §9.1 (`:492`) — decide whether "同一账号授予同一份跨设备权益" is still the target text. |
| `docs/architecture.md:102` | the recorded deployment state ("线上免费额度仍是 50、`/billing/*` 仍按账号会话受理") must be re-checked and updated once 017 + 018 are applied. |
| `MembershipPaywallView.swift:235` | the copy "每日 30 次 AI 规划已在全部设备生效" becomes true only after this change; re-review it and the `en.lproj/Localizable.strings:445` counterpart. |

### 3.3 Deploy ordering

The already-planned order is fixed and must not be reordered
(`membership-device-principal-implementation-design-20260917.md`, "**部署顺序（不可颠倒）**"):
① apply migrations to hosted Supabase; ② `scripts/billing-deploy.sh`; ③ watch
`DEVICE_REQUIRED` / `DEVICE_LIMIT_REACHED` / `ACCOUNT_TOKEN_UNKNOWN`; ④ iOS rollout. The script
itself restates the prerequisite: *"Migrations must already be applied to the hosted Supabase
project BEFORE this script runs"* (`scripts/billing-deploy.sh:3-4`).

`202609180018` slots into ① and requires no change to ②–④:

1. **Apply `202609170017` first**, then `202609180018`, in that order in the same maintenance
   window. Because 017 is **[unverified]** as deployed and the iOS deployment note says it is not
   (`time_fragment:docs/architecture.md:102`), there is no reason to make this a second production
   event — and 018 *depends* on 017 for the `free_limit` unification (§3.5).
2. ②–③ unchanged. Add `AI_DAILY_QUOTA_EXHAUSTED` rate to the watch list, since a shared counter
   makes exhaustion arrive earlier for multi-device members.
3. ④ unchanged and **not gated by this change**: the wire format is unchanged, so the existing iOS
   build keeps working. The counter string becomes accurate without a client release (§3.6).

### 3.4 Rollback

- **What it does.** A `202609180018_…_rollback.sql` mirroring the `202609170017` rollback's
  approach (`supabase/migrations/202609170017_device_principal_billing_rollback.sql`): restore the
  pre-change bodies of `ai_private.quota_status`, `public.ai_quota_service` and
  `public.billing_service` from git, drop `billing_private.plus_source`/`member_scope`, and return
  the meter to `(actor, period)`.
- **What it does not restore.** It cannot remove the row lock or the `billing_service` `aiQuota`
  fix without also reverting the membership page to showing a wrong number; it does not touch
  `202609170017`'s `purchase_devices`, the free-limit 30 unification, or the free-pool mechanism.
  If both migrations are rolled back, `202609170017_…_rollback.sql` is **fail-closed** and refuses to
  run once any non-`account:` principal owns billing rows — and a device-bound purchase is exactly
  that, so **in practice 017 cannot be rolled back after the first real purchase**; the supported
  path is fix-forward.
- **Rollback is not lossless and errs generous.** Member bucket rows written under the chain owner
  during the shared window are indistinguishable from rows the owner legitimately wrote before the
  change. Reverting therefore either (a) leaves them, so the owner's device shows inflated usage and
  the other devices show 0 — i.e. each device appears to gain up to 30/day — or (b) zeroes them,
  which loses real usage. Recommend (a) with an explicit operator note, matching the existing
  convention that a rollback can leave counters stale (`docs/account-api.md:155-160`).
- **Path A comes back on an emergency cutover rollback.** `scripts/rollback-account-ai-cutover.sh`
  reverses to the legacy SQLite authority (`docs/account-api.md:139-166`), where metering is per
  device (`quota_store.py:134`, `:179`) and membership is development-only. **Accepted limitation:**
  a cutover rollback reverts Plus quota to per-device metering. I recommend **not** changing
  `quota_store.py` — it is not reachable in the deployed topology (C5), it has no entitlement concept
  to key a purchase chain on, and adding one would be a second, divergent implementation of the
  billing model purely for an emergency state. Record the limitation in `docs/account-api.md`
  instead.

### 3.5 Existing quota rows

- **Member rows: nothing to migrate.** Production is stated to have zero `store_purchases`,
  zero bound `user_id`, zero `plan='plus'`, zero `billing_claims`
  (`202609170017…:63-67`, "verified on production 2026-09-17"). With no plus entitlement there can
  be no `buckets` row with `period like 'member:%'`, so there is no re-keying to do. The migration's
  guard block (§3.2 item 9) verifies this at apply time and fails closed if the assumption has
  stopped holding. **I could not confirm the zero counts myself** — I have no production read path
  (§6).
- **Free rows: not touched, and already pooled.** `202609140016` **is applied**, so `free_pools`
  exists, `principals.free_pool_id` is populated, `create_free_pool`/`update_free_pool` are live, and
  claimed guests already share their account's pool. The free path is not modified by this change.
- **The 50→30 unification is 017's job, not this migration's.** Live `free_limit` is still 50 across
  ~150 rows plus one row at 3, and `ai_private.enforce_free_limit_30` does not exist, because
  `202609130015_free_quota_30` was never applied; `202609170017…:42-61` re-issues those three
  statements idempotently. **`202609180018` must therefore be applied after 017** (its `plus_source`
  reads `account_entitlements`, and the free-path `coalesce(free_limit,50)` fallbacks are removed by
  017). If the human wants 018 to be independently applicable, it must re-issue the same three
  idempotent 015 statements — say so in the PR, because duplicating them invites drift.
- The `150 rows` / `1 row at 3` figures come from the migration comment at `202609170017…:31-33` and
  are corroborated in prose by the iOS deployment note ("线上免费额度仍是 50",
  `time_fragment:docs/architecture.md:102`). Neither is a query I ran. **[unverified]**

### 3.6 iOS client impact

- **No client change is required to observe the shared counter.** `GET /billing/entitlement` already
  carries `aiQuota.{limit,used,remaining,resetsAt}` (`MembershipEntitlementModels.swift:23-28`) and
  the membership page already renders "今日剩余 %d/%d" for Plus (`MembershipPage.swift:63`). Once
  `billing_service` reports the shared daily bucket (§3.2 item 5), device B shows the shared number
  with no app update. **The server change can ship ahead of the iOS rollout — the client can ship
  independently.**
- **Two client-side caveats, neither blocking.** (a) The number refreshes only when the settings
  surface appears (`SettingsFeatureHost.swift:108-114`), so device B can show a stale count until the
  user opens it — existing behavior. (b) Before the first successful fetch the in-memory default is a
  hardcoded `30/30` (`MembershipEntitlementModels.swift:17-20`), and `resetsAt` is currently returned
  as `null` (`202609170017…:361`) so the documented recovery-time display
  (`membership-plan.md:188`) stays unserved; item 5 fixes the server half.
- **Copy to re-review, not to change blindly:** `MembershipPaywallView.swift:235`.

---

## 4. Acceptance criteria and evidence plan

| Claim | How it is proven | Status |
| --- | --- | --- |
| Two devices on one chain share one daily counter | `supabase/tests/member-shared-quota.test.mjs` cases (a)(b)(g), run on the `embedded-postgres` harness (`supabase/tests/database.mjs`, PG 17.7) via `cd supabase && npm run test:sync` | to be written |
| The shared counter cannot be overspent concurrently | case (h): simultaneous reserves at `remaining = 1` from two principals on one chain; assert exactly one success and `used <= limit`. **This is the load-bearing test** — the existing `:201` lock does not cover two different actors, so a missing shared-row lock shows up here and nowhere else | to be written |
| Idempotency and refunds survive sharing | cases (c)(d), reusing the receipt fencing that already exists (`202609140016…:246-261`) | to be written |
| A signed-in Plus member reaches the Plus quota (C6 regression) | case (g); plus `business_api/tests/test_billing_api.py` for the entitlement payload | to be written |
| The membership page shows the shared number | SQL: `billing_service('entitlement')` returns the member bucket with `resetsAt` for a plus principal — extend `supabase/tests/billing.test.mjs:140` | to be written |
| No role other than `service_role` can reach the new helpers | new case alongside `supabase/tests/billing.test.mjs:66` and `ai-quota.test.mjs:43` | to be written |
| The migration is re-runnable and fails closed on orphaned member rows | apply it twice in one harness run; and a negative case seeding a member bucket with no chain | to be written |
| Existing free-tier behaviour is unchanged | `supabase/tests/ai-quota.test.mjs:60` (`different sessions share 30 account calls`) and `guest-signout-quota.test.mjs` must stay green — **but see the `test:sync` gap: neither file is currently run by `npm run test:sync`** | verified gap |
| Python surface unchanged | `business_api/tests/test_billing_api.py`, `test_account_api.py` | not run (I ran no tests — this is an analysis task) |
| Production pre-conditions | Read-only: `select count(*) from billing_private.store_purchases` (expect 0), `select count(*) from ai_private.buckets where period like 'member:%'` (expect 0 → no re-keying), `select free_limit, count(*) from ai_private.principals group by 1` (expect 30 after 017). **No configured read-only path in this session; I ran none.** | unverified |

**User-visible consequence for a two-device member.** Device A and device B are both bound to one
purchase. A consumes N of the daily 30. When B next refreshes `GET /billing/entitlement` (on the
settings/membership surface appearing), B must show **`今日剩余 (30 − N)/30`** — the same number A
shows, with the same `resetsAt`. B's 31st overall request from the chain must fail with
`AI_DAILY_QUOTA_EXHAUSTED` and the same reset time, regardless of which device sends it. Today B
shows `30/30` and can consume a full 30 of its own; and because of C6 a signed-in B shows the *free
lifetime* remainder instead of either number.

**What would remain unverifiable locally.** Whether the hosted Supabase project has 017 applied; the
real `free_limit` distribution; Apple's behaviour on resubscribe (`originalTransactionId` stability —
M0.5, still unverified per the repository's own record) which now determines whether a resubscribe
resets **both** the 3-device cap and the shared quota; whether `inAppOwnershipType` is present on
non-family-shared receipts; and any real StoreKit purchase/restore end-to-end.

---

## 5.0 Decision record (2026-09-17, product owner via orchestrator)

Recorded here so the reasoning is auditable; these are **decisions, not proposals**.

| # | Decision | Consequence accepted |
| --- | --- | --- |
| **E1** | **Share the daily Plus allowance per purchase chain.** Explicitly *not* per Supabase account. | The merged no-login purchase decision and its UI test stand unchanged. "Per account / shared across all devices" is no longer the target spec: that wording was already withdrawn from the product docs in iOS commit `986619da`, which relabelled it 待产品确认, so this decision resolves an open question rather than reversing a settled one. |
| **E4** | **The free tier is NOT shared.** Two signed-out devices of one free user keep independent lifetime pools. | Accepted explicitly by the product owner ("两个未登录设备不共享没关系"). No `claim-guest` flow, no account referent, no client-asserted group id is introduced. |
| **E2** | **Fix the signed-in member path (C6) in the same change.** | Required, not optional: with per-chain sharing the meter is read from the entitlement's device principal, so as long as a signed-in member's AI request resolves to `account:<uuid>` their Plus counter stays invisible. Deferring would ship a sharing change that its own target users cannot observe. |
| **E3** | Emergency reverse export keeps the scope's member bucket (see below) — **still open**. | |
| **E5** | Self-correcting entitlement read — **deferred on 2026-09-17: recorded, deliberately not fixed yet.** Mechanism verified in source; see §5.0.1. | Accepted knowingly. With per-chain sharing the gap is no longer self-limiting (details below), so it should be closed soon after this ships, but it is not a blocker for the initial deployment. |
| **E6** | The >400-line migration and the per-device rollback degradation are accepted. | |

Still open for the product owner: **E3** (what the emergency reverse export does with shared member
rows) and **E5** (whether `plus_source` should re-check `store_status`/`expires_at` at read time, which
would also narrow the accepted D5 gap whose blast radius sharing widens from per-device to per-chain).

**Explicitly out of scope as a result of E1/E4**: the free tier keeps its current per-principal
lifetime pool and its current `claim-guest` behaviour. This analysis does not propose changing the
free ledger, and no option here should be read as covering it.

### 5.0.1 E5 — verified mechanism of the stale entitlement projection (recorded, not fixed)

Status: the product owner decided on 2026-09-17 to **record this and not fix it yet**. The mechanism
below was verified in source by the orchestrator after PR #59 merged, so a future reader does not have
to re-derive it.

**The projection is a cache, and only one device's cache is refreshed.** `ai_private.quota_status`
reads `billing_private.account_entitlements` and never consults the purchase tables
(`202609170017:181-184`). The only function that recomputes that row is
`billing_private.aggregate_entitlement(target_principal)` (`202609170017:144-164`), which picks the
device's best chain from `purchase_devices` and writes back `plan`/`status`. A refund or expiry
arrives as a webhook, and the webhook re-aggregates exactly **one** principal:
`actor_principal = str(account["principal"])` (`business_api/app/billing_worker.py:112`) is resolved
by `account_by_token` from the notification's `appAccountToken`, then passed to `apple_verify`
(`:121-122`). **Nothing tells the other devices on the chain.**

**Consequence for a 3-device chain.** Device A (the one in the notification) is recomputed to
`plan='free'` and is immediately correct. Devices B and C keep `plan='plus', status='active'`.
`billing_private.plus_source`'s gate is `ae.plan = 'plus' and ae.status in ('active','grace')`
(`202609170020`), so it still passes; and its chain ordering as shipped
(`case sp.store_status when 'active' then 0 when 'billing_retry' then 1 else 2 end, sp.expires_at desc
nulls last, sp.id` — three tiers, then expiry and id as tie-breaks) only ranks chains **against each
other** — it never checks that the selected chain is still valid, so a refunded chain with no
competitor is still selected. B and C therefore keep Plus, metered against **the chain owner's
bucket**, i.e. the chain's whole daily 30, until their own notifications arrive — which for a refund
they may never do.

**Why sharing makes it worse, precisely.** Under per-device metering a stale projection was
**self-limiting**: B could at most give itself 30/day it should not have. Under per-chain metering B
spends the **chain owner's** bucket, so the loss is charged to the whole group, and A — whose
projection is already correctly `free` — sees a different metering outcome than B on the same
membership. The gap is no longer confined to the device that has it.

**A deliberately decoupled pair.** `plus_source` falls back to `coalesce(subquery, p_actor)`, so when
no chain resolves the meter stays on the actor itself. That is intentional — it keeps a projection
with **no purchase behind it** (development and hand-seeded data, and the pre-M4 tests) metered where
it was. Any fix must therefore preserve that: "has a Plus projection" and "has a valid chain" are
intentionally separate, and the latter must not be made a precondition of the former.

**Two fixes, neither implemented.**

- **A — re-check at read time** in `plus_source` (`store_status in ('active','billing_retry')` and a
  non-expired `expires_at`). Single point, since `plus_source` is the only membership gate. But:
  `expires_at > now()` **cannot be added unconditionally** — a `billing_retry` (grace) device may
  momentarily have `expires_at < now()` and must keep its grace, so a careless one-line change
  silently **downgrades grace-period members**, and that error would propagate to the whole chain;
  and the `account_entitlements` gate **cannot simply be removed**, or development/hand-seeded data
  loses membership outright (see the decoupled pair above).
- **B — re-aggregate the whole chain** on refund/expiry (every `purchase_devices` row with
  `revoked_at is null`). Removes the staleness at its source so the projection becomes a trustworthy
  cache again, does not touch grace semantics, and does not endanger seed data. Costs a write-path
  fan-out plus new tests.
  **Recommended**, with A as defence in depth once its semantics are confirmed.

**The load-bearing test is the same either way**: build a 3-device chain, drive a refund/expiry for
**one** of them, and assert the **other two** stop being members on their next quota read **and** that
the shared bucket is no longer charged. Note this test would have **passed by accident** under
per-device metering (each device had its own bucket, so nothing observable differed) — which is part
of why the gap went unnoticed.

**Before dispatching any implementation**: run a read-only semantic check on whether
`store_purchases.expires_at` is trustworthy and how `billing_retry` combines with it in the existing
tests. "Add `expires_at > now()`" looks like one line and is a grace-period downgrade if it is wrong.

---

## 5. Open decisions for the human

1. **E1 — Confirm the referent.** Share Plus quota per **purchase chain** (recommended), per
   **Supabase account**, or not at all. Cost of choosing account: reverses the merged no-login
   purchase decision and requires re-adding a paywall login gate plus rewriting the UI test that
   asserts its absence. Cost of choosing neither: the documented per-device behaviour stands and
   only the docs change. **Recommend: purchase chain.**
2. **E2 — Fix the signed-in member path in the same change (C6)?** Today a signed-in Plus member is
   metered against the free lifetime pool, so the shared Plus counter would be invisible to exactly
   the users the decision targets. **Recommend yes**, via the `plus_source` rule in §3.1. Cost of
   deferring: ship a sharing change that signed-in members cannot observe, and leave a second,
   independent billing defect open. Cost of a narrower alternative (scope only, leave the entitlement
   lookup on `actor`): smaller migration, but C6 stays broken.
3. **E3 — What does the emergency reverse export do with shared member rows?** The chain owner now
   holds a bucket its peers consume, and the legacy SQLite ledger has no concept of a chain
   (`ai_quota_export_legacy`, `202609140016…:475-509`). **Recommend** exporting the scope's member
   bucket alongside the owner's and accepting per-device counters after a rollback. Cost otherwise:
   the reverse export silently drops post-cutover member usage.
4. **E4 — Is the free tier in scope?** With the recommended option it is **not** shared for two
   signed-out devices of one free user (no purchase exists; sharing requires a `claim-guest`, which
   requires an account). **Recommend** accepting this, documenting it, and leaving the lifetime pool
   rule alone. Cost of insisting on free-tier sharing: it forces the account referent (option i) and
   therefore login, or a client-asserted group (option iii) with the worst abuse surface.
5. **E5 — Make the entitlement read self-correcting while we are here?** The accepted D5 gap lets
   devices 2..3 keep a stale Plus projection after refund/expiry
   (`202609170017…:25-29`). Under sharing that gap now lets the *whole group* keep consuming a daily
   30. **Recommend** having `plus_source` re-check `store_status`/`expires_at` at read time — a few
   lines that also partially close D5. Cost otherwise: the D5 blast radius is silently widened from
   per-device to per-chain.
6. **E6 — Accept the two copy/deployment consequences?** (a) The migration re-emits ~480 lines of
   function bodies and exceeds the ≈400-line PR convention, as M4-b already did. (b) A cutover
   rollback reverts Plus quota to per-device metering because `quota_store.py` is left unchanged.
   **Recommend** accepting both with the reasons recorded in the migration header and
   `docs/account-api.md`.

---

## 6. What I could not verify

- **No production read path.** I did not run any query against production or staging; no
  read-only credential path was configured in this session. All production statements above are
  quoted from repository documents and migration comments and are marked as such.
- **Deployment state of `202609170017`.** Two in-repo sources say it is *not* applied
  (`202609170017…:31` "production's applied migration set ends at 202609140016"; and the iOS note
  `time_fragment:docs/architecture.md:102` "服务端设备主体迁移尚未部署：线上免费额度仍是 50、
  `/billing/*` 仍按账号会话受理"). I could not confirm either. Note this also means the merged
  `DEVICE_REQUIRED` gate (`account_api.py:190-191`) is not yet the live behaviour.
- **The `150 rows` / `1 row at 3` / zero-purchase counts** — from `202609170017…:31-33`, `:63-67`.
- **Apple-side semantics (M0.5):** whether a resubscribe yields a new `originalTransactionId` (which
  now decides whether a resubscribe resets both the 3-device cap and the shared quota), whether
  restore returns the same `originalTransactionId`, and whether `inAppOwnershipType` is always
  present. The repository records all three as unverified.
- **Whether the iOS counter ever renders the Plus number in production today** — it depends on the
  deployed server, which I could not reach. The client code path and the current server response
  (free pool, `resetsAt: null`) were both read directly.
- **I ran no tests.** For a design analysis that is deliberate; every "to be written" row in §4 is
  unexecuted, and no code in this document was compiled.
