# Scheduled account deletion and maintenance

These two private, one-shot Node processes run on the existing Linux backend
host. They expose no listening port and do not use the AI Docker Compose stack.
Use Node 24 LTS in a dedicated runtime directory; do not replace the host's Node.

The deletion timer runs one minute after its previous invocation ends, including
a failed invocation. Each pass uses the existing durable queue (at most 100 jobs)
and retries failed Auth deletion without marking it complete. The maintenance
timer runs every five minutes after completion and processes at most 100 accounts.
Locked accounts are skipped. Attempt timestamps rotate both successful and failed
accounts through the queue. Cleanup failures roll back that account's work.

Both timers resume after boot. A unit already executing is not started again.
The service timeout is 90 minutes, above the deletion batch's 100 jobs times three
15-second request deadlines. An interrupted batch is retried from its durable
database state. The intervals are bounded retry delays, not immediate retry loops.

## Install a reviewed candidate

First apply migration `202609080005_maintenance.sql` to the selected project after
inspecting `supabase db push --dry-run`. It adds only a maintenance timestamp,
index and service-only batch RPC. The four existing migrations stay unchanged.

Create a release directory `/opt/daymosaic-sync-worker/releases/<git-sha>/scripts/`
containing exactly `scripts/background-worker.mjs` and `scripts/deletion-worker.mjs`
from the reviewed candidate. Compare their SHA-256 hashes after copying. Point
`/opt/daymosaic-sync-worker/current` to that release. Code and directories remain
owned by root and readable by the dynamic service user.

Download the official Linux Node 24 release archive and its `SHASUMS256.txt` from
the matching directory under `https://nodejs.org/dist/`. Verify the archive's
SHA-256 before extracting to `/opt/daymosaic-sync-worker/runtime`. The initial
deployment pins Node 24.19.0, matching the local validation runtime.

Provision `/etc/daymosaic-sync-worker/supabase.json`, owned by root with mode 0600:

```json
{"url":"https://YOUR_PROJECT.supabase.co","key":"SERVER_ONLY_SERVICE_ROLE_KEY"}
```

Use the existing project's service-role credential from the secret manager or
authenticated CLI. Transfer it through stdin; never include it in a shell command,
Git, a PR, logs or client configuration. `LoadCredential` supplies a private
read-only copy to each dynamic service user. No dependency install is required.

Install the three unit files from this directory into `/etc/systemd/system/` with
mode 0644, then validate before enabling:

```sh
sudo systemd-analyze verify /etc/systemd/system/daymosaic-sync@.service \
  /etc/systemd/system/daymosaic-sync-deletion.timer \
  /etc/systemd/system/daymosaic-sync-maintenance.timer
sudo systemctl daemon-reload
sudo systemctl start daymosaic-sync@deletion.service daymosaic-sync@maintenance.service
sudo systemctl enable --now daymosaic-sync-deletion.timer daymosaic-sync-maintenance.timer
```

## Verify and operate

```sh
systemctl list-timers 'daymosaic-sync-*' --all
systemctl show daymosaic-sync@deletion.service daymosaic-sync@maintenance.service \
  -p Result -p ExecMainStatus -p ExecMainExitTimestamp
journalctl -u daymosaic-sync@deletion -u daymosaic-sync@maintenance --since today --no-pager
```

Logs contain only the worker kind, aggregate counts and `ok`; fatal failures emit
`workerFailed` without error bodies, user IDs, credentials or receipt tokens.
A partial failure exits nonzero and the timer retries. Investigate repeated
nonzero exits and a deletion queue that remains pending across retries. External
alert delivery is not provisioned by these units.

Verify actual scheduled execution using fresh temporary test accounts, then read
back receipt completion and Auth-user removal. A manual unit start alone does not
prove that its timer ran. Do not process invented or existing real-user deletion
requests to test the system.

For rollback, stop both timers and both service instances, point `current` back
to the previous reviewed release, then start the timers again. Keep credentials
and existing database state. The additive maintenance migration can remain while
the services are stopped; no destructive schema rollback is required.

References: [systemd timer semantics](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml),
[systemd credential isolation](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml).
