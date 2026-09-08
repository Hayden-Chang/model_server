# Development membership

The development iOS settings switch uses authenticated `GET` and `POST /api/development/membership`. POST accepts only `{"enabled": true}` or `{"enabled": false}`. The server must list the installation's existing guest device ID in `TIME_FRAGMENT_DEVELOPMENT_DEVICE_IDS` (comma-separated); the default empty list grants nobody access. This is an internal test entitlement, not StoreKit purchase verification.

Members receive 50 successful AI planning requests per calendar day, resetting at 00:00 Asia/Shanghai using server time. Free installations retain their existing cumulative quota (default 50). Member use does not consume the free balance. Turning membership off and on preserves that day's usage. Failed requests are refunded, and previously consumed request IDs remain idempotent across modes and days.

Membership and quota records share the existing SQLite persistence volume. Migration preserves legacy buckets as the free pool. Back up SQLite before migration and keep the existing volume and environment configuration. Once multiple daily buckets exist, older builds that recreate the unique active-principal index cannot open this schema; rollback must retain the period-aware ledger code, not restore an old database over newer usage. Removing an installation from the allowlist and restarting revokes its server membership. The iOS client clears its cached entitlement when the server explicitly denies access, and otherwise keeps the last confirmed state offline for development fragment rewards.

The iOS fragment reward is applied locally only to newly completed tasks: 2 for a confirmed development member and 1 otherwise. It never retroactively doubles the wallet or rewards the same completion again. Release builds hide the development switch and do not grant this local test multiplier.

Deployment is pending the puzzle production feature's merge into Time Fragment main, then validation of the final combined candidate and enrollment of the isolated test installation.
