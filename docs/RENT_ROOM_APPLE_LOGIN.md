# 租房 iOS Apple 登录

本次从 main `65b1eb04cdfa4f5633bbe9727359351c97de172c` 接入 Apple 登录，提取旧 Draft PR #87 的 Apple 方案并适配当前容量 v2。短信和 StoreKit 仍由旧 PR 保留，本次不部署它们。

## 配置与接口

独立 `rent-room-api` 的私有环境文件需要保留现有 `RENT_ROOM_TOKEN_SECRET`，新增：

```dotenv
RENT_ROOM_APPLE_LOGIN_ENABLED=true
RENT_ROOM_IOS_BUNDLE_ID=com.keeline.xunju.ios.dev.iosparity20261019
```

这个 Bundle ID 是当前真机“寻居开发版”的独立身份，已在 Apple 开发者后台启用 APPLE_ID_AUTH。当前验收只信任这个 audience；正式 `com.keeline.xunju.ios` 尚未登记/启用，不用开发版登录结果代表正式包验收。上线正式包前须单独配置准确的 App ID；不同 App ID 的本地账号命名空间不会自动合并。

- `POST /api/rent-room/auth/apple/challenge` 返回 `challengeId`、`nonce`、`expiresIn:300`。原生 `ASAuthorizationAppleIDRequest.nonce` 使用返回值。
- `POST /api/rent-room/auth/apple/session` 仅接收 `challengeId`、`identityToken`。验证固定 Apple JWKS、RS256 签名、issuer、单一准确 audience、有效时间、sub、nonce 后消费一次性挑战。返回 `account`、`token`、`provider:apple`、`expiresIn:7200` 和现有容量结果。
- 客户端发送 `x-rent-room-capacity-version:2`，继续使用线上可配置免费容量；不恢复旧版本的默认 2 个免费名额。
- `GET /api/rent-room/entitlements` 接受业务 bearer 会话；`POST /api/rent-room/session/revoke` 只撤销当前会话。
- 挑战与验证分别限流，并持久化限流状态；nonce 只保存摘要。公钥定时刷新及未知 kid 刷新有缓存节流；网络失败返回 503，非法凭证返回 401，输入校验不回显 token。响应 `no-store`。

登录不依赖微信开放平台，不请求短信，不从客户端信任 account/email/name，不按相同邮箱合并账号。本期使用新鲜 identityToken + nonce 校验，业务会话有效期 2 小时，不保存 Apple refresh token；Apple token exchange、授权撤销通知和账号注销不在本次接入范围。参考 [Apple 身份验证](https://developer.apple.com/documentation/signinwithapple/verifying-a-user)。房源和媒体仍保存在手机本地，本次没有云同步。

## 数据兼容和发布

会话 provider 存在新的旁表 `rr_session_providers`，旧微信行按 `wechat` 读取。`rr_sessions` 保持原有五列，旧镜像回退后仍能写入微信会话；旧镜像会拒绝没有微信加密 session_key 的 Apple 会话。新挑战/限流表为纯增量，订单、退款、反馈表和原有账号 ID 不改写。Apple 会话不能取得微信支付参数。

发布只更新 `/opt/model_server/rent-room` 的 `rent-room-api`，不重建共享 gateway/Caddy 等服务。使用精确提交归档构建固定镜像标签；发布前保存 compose、0600 私有环境文件以及 SQLite online backup，并对备份执行 `quick_check`。保留原镜像与原配置用于回退，不用旧数据库覆盖上线后新增订单/反馈。

## 定向验证映射

- `tests/test_rent_room_apple_auth.py`：全部为新测试，覆盖签名/算法/claims、nonce/重放/过期、账号隔离、容量 v2、撤销、并发、限流重启、JWKS 缓存/轮换/失败、秘密不回显、旧版本读写兼容以及 Apple 禁止微信支付。
- `tests/test_rent_room_capacity.py`：选择 disabled catalog、configuration、authentication isolation、order idempotency、checkout rejection、disabled checkout reconciliation、ledger restart、optional service compose、shared session expiry、failed WeChat login、login reconciliation、production catalog、legacy migration、v1 capacity 这 14 个测试函数。它们直接消费修改后的配置、会话持久化、鉴权和 checkout。
- `tests/test_rent_room_capacity_policy.py`：`test_v2_configured_policy_and_legacy_contracts`、`test_policy_is_public_even_when_payments_and_login_are_disabled`，验证容量协商保留。
- `tests/test_rent_room_feedback.py`：`test_anonymous_feedback_is_persisted_and_idempotent`、`test_feedback_requires_one_valid_answer_and_limits_text`，验证增量数据库初始化和 validation handler 不影响反馈。

初次开发验证 57 项通过，记录在 `/private/tmp/rent-room-apple-evidence/pytest-01.log`、`pytest-01.xml`。最终提交验证单独记录。JWT 测试使用本地生成 RSA 密钥及 mock Apple JWKS，不等于真实 Apple 账号授权。当前线上接口在部署前复核为 HTTP 404。
