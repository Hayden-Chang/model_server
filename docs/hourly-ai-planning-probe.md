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

Use a reviewed commit in an isolated worktree. Install these three Python files
as root-owned, world-readable files in `/opt/model-server-hourly-probe/`:
`scripts/probe-ai-planning.py`, `scripts/validate-time-fragment-smoke.py` and
`scripts/notify-ai-planning.py`.
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

An external heartbeat remains a separate setup step. This host-side timer alone
cannot notify when the whole server is down. Normal one-round model cost for
720 monthly probes is estimated at CNY 4.8–9.6 using the observed small-task
usage, excluding cache discounts and internal correction calls.

## 运维速查：脚本位置与邮件通知

截至 2026-09-09，生产环境已启用每小时检测和邮件告警，并已确认测试邮件到达收件箱。
检测目标为 `https://api.keeline.xyz`；发件人为 `haichao_em@163.com`，
收件人为 `shenshuoyouguang@outlook.com`，使用 `smtp.163.com:465` 的 SSL 加密连接。

### 脚本和配置位置

| 用途 | 仓库位置 | 服务器位置 |
| --- | --- | --- |
| 发起真实排程检测 | `scripts/probe-ai-planning.py` | `/opt/model-server-hourly-probe/probe-ai-planning.py` |
| 校验排程结果 | `scripts/validate-time-fragment-smoke.py` | `/opt/model-server-hourly-probe/validate-time-fragment-smoke.py` |
| 邮件模板、发送及通知状态管理 | `scripts/notify-ai-planning.py` | `/opt/model-server-hourly-probe/notify-ai-planning.py` |
| 检测服务 | `deploy/model-server-hourly-probe.service` | `/etc/systemd/system/model-server-hourly-probe.service` |
| 每小时定时器 | `deploy/model-server-hourly-probe.timer` | `/etc/systemd/system/model-server-hourly-probe.timer` |
| 加载邮件凭据 | `deploy/model-server-hourly-probe-email.conf` | `/etc/systemd/system/model-server-hourly-probe.service.d/email.conf` |

服务器上的运行配置位于 `/etc/model-server-hourly-probe/probe.env`，邮件配置及授权码位于
`/etc/model-server-hourly-probe/mail.json`。配置目录权限为 `0700`，配置文件权限为 `0600`；
授权码仅保存在服务器，不能写入文档、Git 或日志。

最近一次检测结果：`/var/lib/model-server-hourly-probe/latest.json`。
故障和邮件发送状态：`/var/lib/model-server-hourly-probe/mail-state.json`。
部署来源及文件哈希：`/opt/model-server-hourly-probe/deployed-source.json`。

### 发送时机

当前服务器按北京时间每小时整点触发，例如 13:00、14:00；脚本完成检测后决定是否发送邮件。
固定任务是在明天的空白日程中安排 `08:00–08:30` 的 `Production Smoke`，同时传入 `09:00`
的默认开始时间，以验证用户明确时间优先。检测覆盖健康检查、游客鉴权和真实 AI 排程，
不会将方案应用到用户日程。

| 检测情况 | 邮件行为 |
| --- | --- |
| 首次检测失败：连接失败或超时、接口报错、鉴权失败、模型网关异常、响应格式或排程结果不正确 | 立即尝试发送一封故障邮件，不要求连续失败两次 |
| 检测账号额度无法按绑定规则恢复，或脚本捕获到配置错误 | 同样发送故障邮件 |
| 同一故障持续存在，且故障邮件已被 SMTP 接受 | 不重复发送 |
| 故障后首次检测恢复正常 | 发送一封恢复邮件 |
| 一直正常 | 不发送 |
| 邮件发送失败 | 保留状态，下次每小时检测时重试；若届时服务已恢复，发送恢复邮件。恢复邮件发送失败也会重试 |
| 人工执行邮箱配置验证 | 仅发送测试邮件，不调用模型、不改变故障状态；不会随定时器每小时发送 |

SMTP 接受邮件表示发件服务器已接收投递请求，收件箱是否收到需单独确认。
检测进程和后端位于同一台服务器；整机停机、检测进程无法运行，或网络故障导致 SMTP
也不可达时，不能立即发出告警，需要额外的外部监控。两次检测之间发生又恢复的短暂故障可能漏检。

### 故障邮件示例

下面是模板示例，时间、阶段、错误码和 HTTP 状态随实际检测结果变化，并非一次真实事故记录。

发件人：`haichao_em@163.com`

收件人：`shenshuoyouguang@outlook.com`

主题：`[DayMosaic] AI 规划服务检测异常`

```text
AI 规划服务检测异常

服务：https://api.keeline.xyz
检测时间：2026-09-09T13:00:00+08:00
阶段：plan
错误码：MODEL_GATEWAY_ERROR
HTTP 状态：502

每小时检测一次；本邮件只包含合成测试的状态信息。
```

恢复邮件主题为 `[DayMosaic] AI 规划服务已恢复`，正文包含服务地址、检测时间、首次异常时间、
`真实排程已通过：08:00–08:30。` 和本次检测耗时。测试邮件主题为
`[DayMosaic] AI 规划告警邮箱验证`，正文说明它是配置验证邮件且没有触发模型调用。
邮件均不包含用户日程原文、模型原始输出或凭据。

## Optional email alerts

Configure a real outbound SMTP account before activation. A recipient address
alone is not a sending account. Keep a root-owned mode-0600
`/etc/model-server-hourly-probe/mail.json` containing `host`, `port`, `security`
(`ssl` or `starttls`), `from`, `to`, `username` and `password`. Use one plain
recipient address. Store the SMTP credential only on the server, never in Git,
chat, terminal arguments or command output.

Install `deploy/model-server-hourly-probe-email.conf` as
`/etc/systemd/system/model-server-hourly-probe.service.d/email.conf` only after
the credential file exists, then run `systemctl daemon-reload`. The optional
drop-in exposes the JSON through `CREDENTIALS_DIRECTORY/mail-config`. Without
the drop-in, the existing probe continues with notification status `disabled`.

Validate delivery with `scripts/notify-ai-planning.py --test` in a service using
that credential, or set `PROBE_MAIL_CONFIG` to the protected JSON path for a
manual root run. The test does not call the model or change incident state.
`accepted_by_smtp` means the sender accepted the email; inbox receipt must be
confirmed separately.

The first failed probe sends an alert and recovery sends another email. Normal
results and a continued, already-notified failure stay quiet. Failed deliveries
retain the incident in `mail-state.json` and retry on the next hourly probe,
without additional model requests. Recovery is also retried until accepted.
Sending uses certificate-verified TLS and a 10-second socket timeout. Raw SMTP
errors, credentials, user tasks and model output are never included in messages
or logs. A network interruption after SMTP acceptance or a crash before saving
delivery state can cause a duplicate; exactly-once email delivery is not claimed.
