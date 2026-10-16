# 寻居房源包服务

`rent-room-api` 是同一服务器上的独立 FastAPI 进程，入口 `app.rent_room_main:app`。Caddy 将 `/api/rent-room/*` 优先路由至该进程，既有 TimeFragment、TestFlight、模型 API 和账本不变。服务不依赖模型或 Supabase，也不保存房源、答案或媒体。

## 产品和数据

`POST /api/rent-room/session` 是登录与容量的唯一身份入口，返回 `{account, token, packs, limit}`。`account` 固定为 SHA-256(AppID + ":" + OpenID)，供客户端本机目录隔离；`token` 是 2 小时的随机令牌，仅驻留客户端内存。后续权益和订单请求复用该令牌，401 后重新登录；支付关闭不影响账号登录或免费容量。客户端构建配置统一为 `WECHAT_API_URL=https://api.keeline.xyz/api/rent-room`。

唯一新购商品 `room_capacity_10`：199 分，增加 10 套容量，免费 2 套。订单唯一键为微信身份与客户端请求键；平台 `wx_order_id` 也唯一。SQLite 的独立 `rent-room-capacity` 卷持久保存会话和订单；会话密钥加密保存，客户端只收到短期随机令牌。回调和查单重复执行不重复增加权益，已退款订单不能被迟到支付结果恢复。

只有微信查单确认的支付单、正确金额及平台订单号才确认房源包。前端成功回调不发货。已确认付款先持久化，再通知微信发货；通知失败可重试。发货推送验签、解密并校验 AppID 后触发查单，前端或未经验证的通知不能授予权益。未识别的上游状态保留原记录并返回可重试错误。订单保存商品、金额和每包数量快照，旧行迁移为原商品 `house-capacity-10 / 190分 / 10套`，新单采用199分；不改历史金额。发货通知查单仍为未支付/处理中时返回可重试失败，避免丢失发货。

退款通知使用 `RetCode`、`MchRefundId`、`WxRefundId`、`RefundFee` 及原 `MchOrderId / WxOrderId` 校验，独立退款账本幂等记录。缺失结果、账号或平台单号不匹配、退款金额异常都拒绝；失败结果不扣权益；成功部分退款先累计，成功累计达到原支付全额才回收整个10套包。退款先于本地发货时先从微信核对平台支付标识，不补发已退权益。客户端保留超额房源。退款通知遗漏时，后台也会查原支付单：仅status=8（用户侧已收款）、left_fee为数值0、订单号/平台支付单号与下单快照一致、order_fee及paid_fee等于原金额、生产环境env_type=1且支付类型为0/7时回收整包。status=5仅表示商户扣款完成，继续等待；部分退款、缺失或错误类型的剩余金额都保留既有权益。退款单的单号映射无需用于这条原支付单兜底。

查单触发于支付结果轮询、重新登录、恢复购买、权益刷新和发货通知；服务另有每分钟后台补查，无需客户端在线。每轮按检查时间从旧到新最多 10 单，普通补查同单间隔 5 分钟；交互补查总预算 3 秒，后台单轮预算 45 秒，上游失败记录检查时间；交互预算超时改为30秒后重试，避免单笔阻塞后续订单。显式订单查询和发货通知强制查单。服务启动后首轮在一分钟执行，重启后由 SQLite 恢复待处理订单。真实安全推送、iOS 商品价位与商户状态须实际联调，Mock 不证明扣款成功。

## 配置和启用

复制 `.env.example` 新增的 `RENT_ROOM_*` 项至服务器私有环境。`TOKEN_SECRET` 为独立的至少 32 字符随机密钥，不能复用微信 AppKey 或其他产品密钥。`PUSH_TOKEN` 和 43 字符 `PUSH_AES_KEY` 来自微信消息推送配置，采用安全模式。所有配置完成前保持 `RENT_ROOM_PAYMENTS_ENABLED=false`；iOS 另行开通并验证后设置 `RENT_ROOM_IOS_PAYMENTS_ENABLED=true`。不支持在现网混用沙箱凭据；支付 `env` 固定 0。

Compose 默认不发布新端口，仅 Caddy 公网 443；数据库在独立卷 `/var/lib/rent-room/capacity.sqlite3`。Caddy 不依赖新增容量服务的健康状态，容量服务配置或启动失败只影响寻居对应路由。部署新服务和 Caddy 前备份当前 Compose、Caddy 配置和数据卷，按用户授权操作；不要整体覆盖服务器 `.env` 或其他产品数据库。先验证关闭支付时 `/catalog` 返回不可购买，再配置回调并进行小额实单。配置和本地测试完成不代表已部署。

| API | 用途 |
| --- | --- |
| `GET /api/rent-room/catalog` | 价格、容量、商户及 iOS 开通开关 |
| `POST /api/rent-room/session` | `code` 经微信换取身份，返回随机令牌与权益 |
| `GET /api/rent-room/entitlements` | 当前账号已确认房源包与总容量 |
| `POST /api/rent-room/orders` | `requestId`、`productId`、`platform`；价格与数量由服务端固定 |
| `GET /api/rent-room/orders/request/{requestId}` | 仅查询本人订单，核实支付并返回权益 |
| `GET/POST /api/rent-room/wechat/notify` | 微信 URL 校验、加密发货/退款通知 |

请求和响应均不应进入包含令牌、签名、微信登录 code 的业务日志。客户端 API 不接受 OpenID、已购数量或价格作为权益依据。微信后台发布商品、开通 IAP、配置 request 域名和通知 URL 是额外操作，当前 PR 不执行。

## 验证

`tests/test_rent_room_capacity.py` 覆盖身份隔离、签名与金额校验、订单与平台单号幂等、重复通知、退款终态、并发连接、持久化与默认关闭。HTTP 上游以 MockTransport/FakeWeChat 隔离，不实际扣款。

边缘配置影响测试：`test_account_routing.py` 的两个配置用例及 `test_real_caddy_routes_public_api_and_blocks_private_planning`；`test_edge_config.py`；`test_puzzle_theme_publish.py::test_content_mount_is_read_only_and_both_hosts_serve_the_same_catalog`。真实 Caddy 用例通过 `CADDY_BINARY` 指向校验过的 2.11.4 二进制，在临时本机端口验证四个目标服务及私有路由屏蔽。

协议依据：[腾讯云开发支付参考](https://github.com/TencentCloudBase/skills/blob/main/skills/cloudbase-wechat-integration/references/virtual-payment.md)、[维护者对自建接入的说明](https://github.com/TencentCloudBase/CloudBase-AI-Toolkit/issues/1132)、[微信查单接口](https://developers.weixin.qq.com/miniprogram/dev/server/API/VirtualPayment/api_query_order)。签名、回调和退款的商户联调需再与微信后台现行契约核对。

## 2026-10-16 联调配置与执行边界

公开配置：AppID `wxf43c7f280c98efca`，OfferID `1450681249`，商品 `room_capacity_10`，199分、每包10套、免费2套。回调 URL 为 `https://api.keeline.xyz/api/rent-room/wechat/notify`，request 合法域名为 `https://api.keeline.xyz`。

在服务器私有环境配置 `RENT_ROOM_APP_SECRET`、`RENT_ROOM_APP_KEY`、独立随机 `RENT_ROOM_TOKEN_SECRET`、消息推送 `RENT_ROOM_PUSH_TOKEN / RENT_ROOM_PUSH_AES_KEY`。密钥不写入仓库、小程序、命令输出或验证报告。消息推送选安全模式，与服务端Token/AESKey保持一致。完成配置和联调前保持两个支付开关为false。

本轮线上catalog仍为旧190分商品且available=false。已根据用户提供的部署说明，在 `/opt/model_server/rent-room/private.env` 配置OfferID、现网AppKey和新生成的回调Token/AESKey，保留原AppSecret与Token Secret；文件权限0600。原配置和SQLite在线备份位于 `/opt/model_server/rent-room/backups/virtual-payment-config-20261016T043212Z`。只重建寻居独立容器加载配置，继续使用旧镜像 `rent-room-api:f186bee17c59`，健康检查通过，其他容器身份/镜像/启动时间未变。新业务代码尚未部署；微信后台配置和真实付款仍未完成。不得将客户端与服务端分开上线造成商品不一致。部署时仅更新rent-room-api；备份独立SQLite卷（使用SQLite备份接口或停止本服务后备份），保留现有环境文件及镜像以便回退，不重建其他服务。回调验签与URL检查可在关闭新购时完成，配置完成后只开放已联调的平台。

验收顺序：关闭支付时真实wx.login成功；GET回调校验；安卓1.99元真单及后台订单一致；重复通知不重复加包；模拟通知遗漏后后台主动补发；退出/换机同微信账号恢复权益；成功全额退款回收10套、失败退款保留、重复退款不重复扣；新增/导入/归档恢复拦截，归档和删除释放有效占用。另核对退款单查询回包及iOS资格，未确认前保持iOS关闭。

### 本轮定向测试与现场检查

执行 `PYTHONPATH=business_api /private/tmp/rent-room-payment-python/bin/python -m pytest -q business_api/tests/test_rent_room_capacity.py`。该文件覆盖本次全部API、SQLite迁移、固定商品、后台补发、退款结果/金额/重复通知/环境与签名边界；未运行其他业务全套测试。日志位于本worktree的 `artifacts/`。

修改前回归：漏通知后同账号登录返回limit2（预期12）；未确认付款的发货通知返回200（预期503）；显式非现网退款被接受并回收容量（预期拒绝）。原始失败分别保存为 `payment-regressions-before.log` 和 `refund-environment-before.log`。

服务端配置自检：GET签名URL校验通过，AES未知事件探针通过，错误签名返回403；微信access_token可取得。对随机不存在订单的真实签名查询返回268490002，错误签名对照返回268490003，仅证明只读接口可达及签名检查可区分，不代表发生支付。所有查询均不创建订单或修改购买权益；密钥、OpenID和token不进入报告。

尚未完成：微信后台回调保存、真实微信身份重登、1.99元实单、退款实单以及iOS资格。新代码保持草稿，不将mock和端点自检当成正式收费验收。

### 官方协议原文复核与退款兜底

2026-10-16通过服务器读取[微信查单原文](https://developers.weixin.qq.com/miniprogram/dev/server/API/VirtualPayment/api_query_order)及[通知字段](https://developers.weixin.qq.com/miniprogram/dev/platform-capabilities/business-capabilities/virtual-payment.html)，取得比旧维护者说明更明确的退款阶段：5为商户扣款完成，8为用户收款完成；支付单left_fee为退款后剩余金额。请求env=0与响应env_type=1均指生产，不能混用编码。原文保存在本机 `/private/tmp/wechat-virtual-query-order.html` 及 `/private/tmp/wechat-virtual-payment-official.json`，不提交完整网页。

据此补上漏退款通知时的原支付单全额回收，已知平台支付ID不允许被查询结果替换。发货通知必须具有匹配OpenId及生产Env；退款字段表没有Env，因此缺失不默认成沙箱或现网，靠生产支付查询和严格OpenId/WxOrderId关联，显式非生产Env仍拒绝。新增覆盖商户扣款未完成到用户收款、部分/缺失/字符串/布尔剩余金额、环境和金额不匹配、重复全额回收、迟到通知及支付结果。平台支付ID变更回归在修复前返回200（预期503），失败证据在 `artifacts/platform-identity-before.log`。
