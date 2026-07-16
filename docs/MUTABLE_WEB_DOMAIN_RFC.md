# RFC：固定内部身份 + 可替换 CMX 访问域名

> 状态：**已接受并写入 GitHub 部署代码，但尚未在目标 Windows 电脑运行验证。**
>
> 当前实现文件：`.env.production.example`、`setup.ps1`、`status.ps1`、`change-access-domain.ps1`。

## 1. 背景

PI OS 是单用户、无公开联邦、手机浏览器访问的私人生活时间线。当前运行底座是 Mastodon Web；以后 CMX 作为同源网页体验层接入。

公网域名只承担“手机当前从哪里进入”的作用。用户可能按年更换低价域名，因此公网门牌不能成为不可替换的数据身份。

## 2. 决策

```env
# 永久内部身份，不解析、不访问、不更换
LOCAL_DOMAIN=pi.invalid

# 当前手机网页入口，可以按受控流程替换
WEB_DOMAIN=pi.ler428.xyz

# 显式与当前网页入口同步
STREAMING_API_BASE_URL=wss://pi.ler428.xyz

# 只在域名切换期使用
ALTERNATE_DOMAINS=
```

公网链路：

```text
手机浏览器
   │ HTTPS
   ▼
当前 WEB_DOMAIN
   ▼
Cloudflare Named Tunnel
   ▼
cloudflared → nginx:80
                  ├─ 网页 / Session / REST / 上传 → web:3000
                  └─ /api/v1/streaming         → streaming:4000
```

## 3. 硬边界

- `LOCAL_DOMAIN` 永远固定为 `pi.invalid`。
- CMX、Nginx 和脚本不得把 `pi.invalid` 当作可访问 URL。
- 使用可变 `WEB_DOMAIN` 后，实例永久不加入公开联邦。
- 不对数据库执行全库 `REPLACE(old_domain, new_domain)`。
- 域名切换不重建 `SECRET_KEY_BASE`、`OTP_SECRET`、`ACTIVE_RECORD_ENCRYPTION_*` 或 VAPID keys。
- 首版不启用 WebAuthn/passkey；以后也不得把它作为 Owner 唯一可用的认证方式。
- CMX 使用同源网页 Session 和 CSRF token，或页面派发的当前用户 token；不注册长期绑定公网域名的 OAuth application。

## 4. 源码事实与可行性

Mastodon v4.6.3 的 `config/initializers/1_hosts.rb` 分别读取 `LOCAL_DOMAIN` 与 `WEB_DOMAIN`，不对 `LOCAL_DOMAIN` 做 DNS 解析：

- `config.x.local_domain` 保存内部身份字符串；
- `config.x.web_domain` 用于网页、URL helper、邮件默认 URL 和 WebAuthn origin；
- streaming 默认可从 `WEB_DOMAIN` 派生，本项目显式设置以便切换脚本校验；
- local、web 和 alternate domains 都可加入 Rails HostAuthorization。

因此 `LOCAL_DOMAIN=pi.invalid` + 真实 `WEB_DOMAIN` 可以完成建库、Owner 创建、网页登录、REST、媒体、Sidekiq 和 streaming。`/api/v2/instance` 的 `domain` 字段会显示 `pi.invalid`；CMX 不得把该字段当成 API base URL。

## 5. 数据库中的旧门牌

确定会保留历史 `WEB_DOMAIN` 的核心字段：

- `statuses.uri`：本地 status 创建时写入当时的绝对 ActivityPub URI；
- 本地产生的 follow、follow request、block、report 等关系对象可能保存 `/payloads/<uuid>` 绝对 URI；
- 使用 OAuth 时，`oauth_applications.redirect_uri`、`oauth_applications.website` 和 access grant redirect URI 可保存旧门牌；
- 用户手写正文、预览卡、Webhook、审计 permalink 或邮件队列中也可能含旧绝对链接。

本地账号的 account URI、媒体文件路径和 REST 返回的 status/account/media URL主要按当前 `WEB_DOMAIN` 动态生成。历史 `statuses.uri` 不需要默认改写；在无联邦且接受旧书签失效的前提下，它不破坏本地时间线或数据库主键。

因此：

- 切换前只做只读盘点；
- 不做默认 SQL URL 迁移；
- 永远不开联邦，避免把历史旧 URI 重新暴露为网络身份。

## 6. CMX 认证与前端约束

CMX 必须同源：

- REST 请求使用 `/api/v1/...` 等相对路径；
- 浏览器自动携带当前 origin 的 Mastodon Session Cookie；
- 写请求使用当前页面提供的 CSRF token；
- streaming 从当前 origin 或实例元数据获得；
- 媒体使用后端返回 URL，不自行拼接旧门牌；
- OAuth callback、登出、Service Worker、Web Push 和缓存不写死域名。

不采用长期 OAuth PKCE application 作为默认 CMX 登录方式，因为 redirect URI 会持久化旧门牌。将来 CMX 与 Mastodon 分离成不同 origin 时，再单独设计 OAuth。

## 7. `ALTERNATE_DOMAINS` 的真实作用

它只负责：

- 让 Rails HostAuthorization 接受过渡 Host；
- 辅助 WebFinger 把额外域名识别为本机。

它不负责：

- canonical URL 或媒体 URL 生成；
- CSP `connect-src`；
- WebAuthn allowed origin / RP ID；
- Session Cookie 和浏览器存储；
- OAuth redirect URI；
- Service Worker 和 Web Push origin；
- 主 streaming URL。

所以新域名仅加入 `ALTERNATE_DOMAINS` 时，只能证明 Host/TLS/基础 GET 可达，不能证明已独立接管完整网页功能。

## 8. 两阶段切换

专用脚本：

```powershell
.\change-access-domain.ps1 -Phase Prepare -NewDomain "pi.new-domain.xyz"
.\change-access-domain.ps1 -Phase Switch  -NewDomain "pi.new-domain.xyz"
.\change-access-domain.ps1 -Phase Release
```

### Prepare：兼容与基础预检

1. 用户先在 Cloudflare 为新域名添加 Public Hostname，仍指向 `http://nginx:80`。
2. 脚本验证新域名 `/_pi/health` 可达。
3. 运行完整备份。
4. 把新域名加入 `ALTERNATE_DOMAINS`，不改当前 `WEB_DOMAIN`。
5. recreate `web`、`streaming`、`sidekiq`。
6. 只验证 HostAuthorization、HTML/API 基础 GET 和 `instance.domain=pi.invalid`。

此阶段 canonical URL、Cookie、CSP 和 WebSocket 主地址仍属于旧门牌；新页面可能跨 origin 使用旧 WSS。它不是完整切换成功证据。

### Switch：正式切换

1. 确认新域名已完成 Prepare。
2. 检查 WebAuthn 风险与未完成 Sidekiq 队列；默认拒绝无确认地丢弃。
3. 再做一次完整备份。
4. 停止 `web`、`streaming`、`sidekiq` 写入/消费。
5. 原子修改：

```env
LOCAL_DOMAIN=pi.invalid
WEB_DOMAIN=pi.new-domain.xyz
STREAMING_API_BASE_URL=wss://pi.new-domain.xyz
ALTERNATE_DOMAINS=pi.old-domain.xyz
```

6. `redis-cli FLUSHDB`，清除旧 origin 派生缓存、队列和短期状态。
7. recreate `web`、`streaming`、`sidekiq`。
8. 验证实例身份仍为 `pi.invalid`，streaming URL 已改为新门牌。
9. 完成浏览器人工 smoke，并临时阻断旧域名，证明新页面没有暗中依赖旧 origin。
10. 失败时脚本把 env 回滚为旧 `WEB_DOMAIN`，再次清 Redis 并重建应用进程。

### Release：结束过渡

人工确认新域名稳定后：

1. 清空 `ALTERNATE_DOMAINS`；
2. recreate 应用进程；
3. 删除旧 Cloudflare Public Hostname；
4. 清理旧 origin 的 Service Worker / Push 订阅。

## 9. 换域名后预期变化

允许失效：

- 旧域名书签和手工绝对链接；
- 旧 Cookie、Session 和 CSRF token；
- 旧 origin 的 localStorage、IndexedDB、Service Worker 和缓存；
- 旧 Web Push 订阅；
- 旧 WebAuthn/passkey；
- 写死旧 redirect URI 的 OAuth application；
- 切换前未处理的少量 Push/Mailer URL 或被明确丢弃的 Sidekiq 任务。

必须保持：

- PostgreSQL 账号、动态、关系和主键；
- 媒体文件和相对存储路径；
- Owner 密码与 TOTP；
- `LOCAL_DOMAIN=pi.invalid`；
- 所有 Mastodon 加密密钥和 VAPID keys；
- 旧正文、时间线、图片和后续新写入能力。

## 10. 最小验收

首次部署或 Switch 后至少验证：

1. `https://<WEB_DOMAIN>/_pi/health` 返回 200。
2. `/api/v2/instance` 的 `domain` 为 `pi.invalid`。
3. `configuration.urls.streaming` 为 `wss://<WEB_DOMAIN>`。
4. 浏览器密码/TOTP登录成功。
5. 可读取切换前的旧 status 和旧媒体。
6. 新发文字与图片，Sidekiq 完成缩略图。
7. 时间线刷新与 streaming 事件正常。
8. REST 返回的新 canonical/status/media URL 使用当前门牌。
9. 浏览器无 CSP、CSRF、mixed-content 错误。
10. 临时阻断旧域名后，媒体和 streaming 仍正常。
11. 重启服务后重新登录并读取数据。

## 11. 当前实施状态

已写入 GitHub、未在目标电脑验证：

- env 双域名模板；
- `setup.ps1 -AccessDomain`；
- `status.ps1` 身份/门牌/streaming 一致性检查；
- `change-access-domain.ps1` Prepare / Switch / Release 与失败回滚；
- 项目需求、架构、部署和 Agent 文档同步。

剩余验证只来自首次真实部署与未来一次真实域名切换；不得用继续纸面审计替代运行输出。