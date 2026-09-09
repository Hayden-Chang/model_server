# Hourly AI planning probe

This optional host-side probe follows the public HTTPS health → guest auth →
planning route. It submits a fixed synthetic task on tomorrow's empty plan:
08:00 Production Smoke for 30 minutes, with a 09:00 default. It validates the
V2 response, request/fingerprint correlation, one new task and exactly 08:00–08:30.
It never applies a plan or reads user task content. No application modules,
database migrations, model settings or Compose services change.

The systemd timer runs once per hour. It does not replay missed hours after
downtime or restart automatically on failure. A run has a 150-second outer
timeout and a process lock. Normal runs issue one planning request; the API may
perform its existing internal correction. Network/model/validation failures are
not retried by the probe. A validated probe-quota rejection can be replenished
and retried once; the rejected request made no model call.

## Installation contract

Use a reviewed commit in an isolated worktree. Install these two Python files
as root-owned, world-readable files in `/opt/model-server-hourly-probe/`:
`scripts/probe-ai-planning.py` and `scripts/validate-time-fragment-smoke.py`.
Install the supplied service/timer in `/etc/systemd/system/` and verify them with
`systemd-analyze verify` before enabling the timer.

Create a mode-0700 `/etc/model-server-hourly-probe/` directory and a mode-0600
`probe.env` with:

```ini
PROBE_BASE_URL=https://api.keeline.xyz
PROBE_DEVICE_ID=ai-planning-hourly-probe-<new UUID>
PROBE_SUPPORT_CODE=<support code bound to this exact probe identity>
```

Provision the existing API's admin credential locally on the server in a
mode-0600 `admin-key` file in that directory; never copy credentials into Git or
terminal output. The service uses systemd credentials and a dynamic user. The
script reads `CREDENTIALS_DIRECTORY`, including on systemd 249; `PROBE_ADMIN_KEY_FILE`
can provide an explicit file path for non-systemd execution. The `/opt` asset
directory must be mode 0755 so the dynamic user can read the scripts.

Bootstrap with the dedicated identity and obtain its support code from the
server's quota registry, matching the exact guest principal derived from this
device ID. Bind that code in `probe.env`. The probe refuses quota recovery for
any other support code, missing credentials, daily membership quotas or an
ordinary App device ID. It never calls reset-all and does not alter user quotas.

Start the service once, inspect the result, then enable/start the timer. Confirm
the timer is enabled and its next trigger is an hour boundary. Stop scheduling
with `systemctl disable --now model-server-hourly-probe.timer`.

## Results and notifications

Each run writes a sanitized JSON record to journald and atomically replaces
`/var/lib/model-server-hourly-probe/latest.json`. Records contain only status,
timing, safe error codes and probe request IDs. No tokens, raw provider errors,
request bodies or model output are logged. Events distinguish `failure`,
`still_failing`, `recovered` and `healthy` for notification routing.

Messaging and an external heartbeat remain separate setup steps: choose a
notification destination before sending messages. This host-side timer alone
cannot notify when the whole server is down. Normal one-round model cost for
720 monthly probes is estimated at CNY 4.8–9.6 using the observed small-task
usage, excluding cache discounts and internal correction calls.
