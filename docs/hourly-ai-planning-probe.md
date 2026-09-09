# 每小时 AI 规划服务检测

这套可选检测脚本运行在后端服务器上，依次访问公网 HTTPS 健康检查、游客鉴权和排程接口。
每次在明天的空白日程中提交一个固定测试任务：08:00 开始，持续 30 分钟，任务名为
`Production Smoke`，同时传入 09:00 的默认开始时间。脚本校验 V2 响应格式、请求标识与
方案指纹是否对应，并确认只新增一项任务，且时间准确落在 08:00–08:30。
检测不会应用方案或读取用户任务内容，也不修改应用模块、数据库、模型设置或 Compose 服务。

systemd 定时器每小时运行一次，停机期间错过的检测不会补跑，检测失败也不会自动重启进程。
单次运行的外层超时为 150 秒，并通过进程锁防止并发执行。正常情况下只发起一次排程请求，
接口内部可能按现有逻辑进行纠正。网络错误、模型错误和排程校验失败不会由检测脚本重试。
若确认请求因专用检测账号额度耗尽而被拒绝，可补充该账号额度后重试一次；被额度拦截的
请求没有调用模型。

## 安装要求

使用隔离工作树中经过评审的提交，将以下三个 Python 文件安装到
`/opt/model-server-hourly-probe/`，文件归 root 用户所有，允许其他用户读取：
`scripts/probe-ai-planning.py`、`scripts/validate-time-fragment-smoke.py` 和
`scripts/notify-ai-planning.py`。
将仓库提供的服务和定时器配置安装到 `/etc/systemd/system/`，并在启用定时器前使用
`systemd-analyze verify` 校验。

创建权限为 `0700` 的 `/etc/model-server-hourly-probe/` 目录，以及权限为 `0600` 的
`probe.env` 文件，填入以下配置：

```ini
PROBE_BASE_URL=https://api.keeline.xyz
PROBE_DEVICE_ID=ai-planning-hourly-probe-<新生成的 UUID>
PROBE_SUPPORT_CODE=<与该检测账号绑定的支持码>
```

在服务器本地将现有接口的管理员凭据写入同一目录下权限为 `0600` 的 `admin-key` 文件，
不要将凭据复制到 Git 或终端输出。服务使用 systemd 凭据机制和动态用户运行。
脚本通过 `CREDENTIALS_DIRECTORY` 读取凭据，兼容 systemd 249；在 systemd 以外执行时，
可通过 `PROBE_ADMIN_KEY_FILE` 指定凭据文件路径。脚本目录
`/opt/model-server-hourly-probe/` 的权限必须为 `0755`，以便动态用户读取脚本。

初始化时使用专用检测身份，从服务器额度记录中查出对应的支持码，确认它属于该设备标识
生成的游客账号，再将支持码绑定到 `probe.env`。对于其他支持码、缺少凭据、会员每日额度
或普通应用设备标识，脚本均拒绝执行额度恢复。它不会调用全量重置接口 `reset-all`，
也不会修改用户额度。

先手动启动一次服务并检查结果，再启用和启动定时器，确认定时器已启用且下一次执行时间
为整点。停止定时检测可执行 `systemctl disable --now model-server-hourly-probe.timer`。

## 检测结果与通知

每次运行都会向 journald 写入脱敏后的 JSON 记录，并原子替换
`/var/lib/model-server-hourly-probe/latest.json`。记录仅包含状态、时间、耗时、
安全错误码和检测请求标识，不记录令牌、供应商原始错误、请求正文或模型输出。
事件分为 `failure`（首次故障）、`still_failing`（持续故障）、`recovered`（已恢复）
和 `healthy`（正常），供通知逻辑判断。

外部心跳监控需要单独配置，仅依靠本机定时器无法在整台服务器停机时发送通知。
按此前观测到的小任务用量估算，每月 720 次检测、每次一轮模型调用的费用约为
4.8–9.6 元，未计入缓存优惠及接口内部纠正带来的额外调用。

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

## 邮件告警的可选配置

启用前需要配置可实际发信的 SMTP 账号，仅提供收件地址无法发信。
创建归 root 用户所有、权限为 `0600` 的 `/etc/model-server-hourly-probe/mail.json`，
包含 `host`、`port`、`security`（取值为 `ssl` 或 `starttls`）、`from`、`to`、
`username` 和 `password` 字段。收件人只能填写一个纯邮箱地址。
SMTP 凭据仅保存在服务器，不得放入 Git、聊天、终端命令参数或命令输出。

确认凭据文件已存在后，再将 `deploy/model-server-hourly-probe-email.conf` 安装为
`/etc/systemd/system/model-server-hourly-probe.service.d/email.conf`，然后执行
`systemctl daemon-reload`。这份可选的服务附加配置通过
`CREDENTIALS_DIRECTORY/mail-config` 提供 JSON 凭据；未安装附加配置时，
检测仍可运行，通知状态为 `disabled`（未启用）。

在加载该凭据的服务中执行 `scripts/notify-ai-planning.py --test` 验证发信；
也可将 `PROBE_MAIL_CONFIG` 指向受限 JSON 文件，由 root 用户手动执行。
测试不会调用模型或改变故障状态。`accepted_by_smtp` 表示发件服务器已接受邮件，
收件箱是否收到仍需单独确认。

首次检测失败时发送告警，恢复时再发送一封邮件；正常结果及已经通知过的持续故障保持静默。
发送失败时，故障状态保留在 `mail-state.json`，到下一次每小时检测时重试，
不会为邮件重试额外调用模型。恢复邮件也会重试，直到被发件服务器接受。
发信使用经过证书校验的 TLS 连接，套接字超时为 10 秒。邮件和日志均不包含 SMTP
原始错误、凭据、用户任务或模型输出。若 SMTP 接受邮件后网络中断，或保存发送状态前
进程崩溃，可能出现重复邮件，因此不保证每封邮件严格只投递一次。
