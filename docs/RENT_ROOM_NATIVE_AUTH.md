# 寻居 iOS 认证服务

增加原生 iOS 手机号短信验证码及 Apple 登录，复用 rent-room-api 的同一 SQLite 会话和容量账本。默认关闭新登录，已有微信会话通过 provider=wechat 的无损迁移保持原语义。未部署本分支；测试不代表短信到达、真实 Apple 授权或 StoreKit 购买已验收。

## 接口契约

根路径 `/api/rent-room`，响应均 `Cache-Control: no-store`。

| 请求 | 成功响应/行为 |
| --- | --- |
| POST `/auth/phone/code` `{phone}` | `{challengeId,expiresIn:300,retryAfter:60}`；只在短信供应商接受后激活验证码，不返回验证码 |
| POST `/auth/phone/session` `{phone,code,challengeId}` | `{account,token,provider:"phone",expiresIn:7200,packs,limit}` |
| POST `/auth/apple/challenge` | `{challengeId,nonce,expiresIn:300}`；原生 `ASAuthorizationAppleIDRequest.nonce` 原样设置为服务端 nonce |
| POST `/auth/apple/session` `{challengeId,identityToken}` | 同一会话契约，provider=apple；服务端验证 Apple JWKS 签名、issuer、audience、时间及一次性 nonce |
| POST `/session/revoke` Bearer | `{revoked:true}`；撤销当前 token |
| GET `/entitlements` Bearer | 已有的 `{packs,limit}` 契约，支持三类会话 |

手机号只接收中国大陆 11 位号码和 6 位 ASCII 验证码。验证码保存为带服务端密钥的 HMAC 摘要，号码以独立身份密钥 HMAC 后作为 subject；不保存明文号码或验证码。每个挑战最多失败5次，有效期5分钟，成功后即失效。发送间隔60秒、号码每天10次、IP十分钟20次，以及可配置全局每日发送预算都持久化在同一账本，重启不会清零。失败和超时计入发送限制，防止自动重试导致重复计费。

Apple 验证采用固定 `https://appleid.apple.com/auth/keys`，固定 issuer、服务端配置的唯一 Bundle ID、签名算法白名单和 PyJWT。公钥缓存支持轮换；不依据客户端给出的 user identifier、邮箱或姓名建立信任。不同 Apple subject、手机号及微信身份不自动合并。身份令牌和 bearer token 不得包含在房源 ZIP。

## 配置与真实接入

原生认证均需已有独立 `RENT_ROOM_TOKEN_SECRET`。Apple 设置最终 `RENT_ROOM_IOS_BUNDLE_ID` 后启用 `RENT_ROOM_APPLE_LOGIN_ENABLED`，客户端需在相同 App ID 上开通 Sign in with Apple。

短信使用阿里云中国站官方 SDK，需在 `.env` 或秘密管理系统提供：

- `RENT_ROOM_SMS_ACCESS_KEY_ID` / `RENT_ROOM_SMS_ACCESS_KEY_SECRET`：仅具短信发送权限的 RAM 凭证。
- `RENT_ROOM_SMS_SIGN_NAME` / `RENT_ROOM_SMS_TEMPLATE_CODE`：已通过供应商审核的验证码签名和模板，模板变量为 `code`。
- `RENT_ROOM_AUTH_IDENTITY_SECRET`：至少32字符的独立稳定随机密钥，与会话和支付密钥分离。轮换会改变手机号的身份映射，不能作为普通会话密钥轮换处理。
- `RENT_ROOM_SMS_LOGIN_ENABLED=true`，按需要设置 `RENT_ROOM_SMS_DAILY_LIMIT`（默认1000）。

凭证不写入 iOS App。SDK 关闭自动重试；只有 `Code=OK` 才返回已发送。HTTP/供应商失败返回安全的503错误，客户端保留输入，不显示成功。新原生会话不能调用微信下单获取支付参数，原生扩容另行接入 StoreKit。

正式启用前须分别验证短信实际到达/输入验证码、Apple 真机授权、退出撤销、再次登录得到同一账号。单测使用内存短信供应商及本地签名 JWT，绝不向真实号码发送测试短信。

依据：[阿里云 SendSms](https://help.aliyun.com/zh/sms/developer-reference/api-dysmsapi-2017-05-25-sendsms)、[Apple 验证用户](https://developer.apple.com/documentation/signinwithapple/verifying-a-user)。
