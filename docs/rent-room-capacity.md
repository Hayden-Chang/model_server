# 寻居房源包服务

`rent-room-api` 是同一服务器上的独立 FastAPI 进程，入口 `app.rent_room_main:app`。Caddy 将 `/api/rent-room/*` 优先路由至该进程，既有 TimeFragment、TestFlight、模型 API 和账本不变。服务不依赖模型或 Supabase，也不保存房源、答案或媒体。

## 产品和数据

唯一商品 `house-capacity-10`：190 分，增加 10 套容量，免费 2 套。订单唯一键为微信身份与客户端请求键；平台 `wx_order_id` 也唯一。SQLite 的独立 `rent-room-capacity` 卷持久保存会话和订单；会话密钥加密保存，客户端只收到短期随机令牌。回调和查单重复执行不重复增加权益，已退款订单不能被迟到支付结果恢复。

只有微信查单确认的支付单、正确金额及平台订单号才确认房源包。前端成功回调不发货。已确认付款先持久化，再通知微信发货；通知失败可重试。加密推送验签、解密并校验 AppID 后触发查单，通知正文不能直接授予或撤回权益。未识别的上游状态保留原记录并返回可重试错误。退款确认后减少容量，客户端保留超额房源。

查单触发于支付结果轮询、恢复购买、应用启动刷新及通知；普通权益刷新对同一订单设 5 分钟间隔，订单查询和通知强制查单。当前没有后台扫描进程，未送达通知由客户端下次恢复购买补查。部分退款、真实安全推送响应的微信验收、iOS 商品价位与商户开通状态尚需实际商户联调，不能从 Mock 测试推断通过。

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
