# PI OS 架构说明

## 一句话

PI OS 不是重新开发社交平台内核，而是把 Mastodon v4.6.3 当作稳定的私人时间线后端，用 Docker 管理依赖、Nginx 统一入口、Cloudflare Tunnel 供手机浏览器访问，并把永久内部身份与可替换公网门牌分离。

当前网页是 Mastodon Web。独立 CMX 前端尚未加入；以后作为同源网页层接入，不改变数据库和核心容器职责。

## 域名分层

```env
LOCAL_DOMAIN=pi.invalid
WEB_DOMAIN=pi.ler428.xyz
STREAMING_API_BASE_URL=wss://pi.ler428.xyz
ALTERNATE_DOMAINS=
```

- `LOCAL_DOMAIN`：永久内部身份锚点，只是字符串，不做 DNS 访问。
- `WEB_DOMAIN`：当前公网网页入口，可以通过专用脚本替换。
- `STREAMING_API_BASE_URL`：当前 WebSocket 入口，必须与 `WEB_DOMAIN` 同步。
- `ALTERNATE_DOMAINS`：切换期间接受额外 Host，不负责完整 origin 迁移。

使用可变 `WEB_DOMAIN` 后，实例永久保持无公共联邦。历史 status/relationship ActivityPub URI 可以保留旧门牌，不做全库替换。

## 请求如何流动

```text
手机浏览器
        │ HTTPS / Session / REST / WebSocket
        ▼
当前 WEB_DOMAIN
        ▼
Cloudflare Edge
        │ 加密 Tunnel；家庭路由器不开放端口
        ▼
cloudflared 容器
        │ HTTP: nginx:80
        ▼
Nginx 容器
   ├─ 网页、登录、REST、媒体上传 ──→ Mastodon Web :3000
   └─ /api/v1/streaming ─────────→ Mastodon Streaming :4000
```

Nginx 必须存在，因为普通网页/API 与实时 streaming 是两个独立进程。Cloudflare 只连接 Nginx，Nginx 再按路径分流。

## 各组件职责

### `web`

Mastodon Rails Web 服务：

- 密码/TOTP登录与网页 Session；
- 时间线、动态和标准 REST API；
- 账号、设置和后台管理；
- 页面与静态资源；
- 图片上传请求；
- 根据启动时读取的 `WEB_DOMAIN` 生成 URL、CSP、WebAuthn origin 和网页元数据。

域名切换后必须 recreate。

### `streaming`

Mastodon Node.js streaming 服务：

- 实时时间线和通知；
- WebSocket/SSE 长连接；
- 避免网页依赖轮询。

切换脚本统一 recreate，以保持运维状态一致。

### `sidekiq`

后台任务执行器：

- 图片处理与缩略图；
- Web Push、邮件和异步任务；
- 使用 Rails 启动配置生成部分 URL。

域名切换后必须 recreate。切换前未完成任务需要排空或明确接受在 `FLUSHDB` 时丢弃。

### `db`

PostgreSQL 保存长期结构化事实：

- 账号、动态、关系和设置；
- 媒体元数据；
- 历史 `statuses.uri` 等可能含创建时的旧 `WEB_DOMAIN`。

数据库使用 Docker named volume，避免 PostgreSQL 直接运行在 Windows NTFS bind mount 上。

域名切换不迁移数据库主键、正文或媒体记录，也不执行全库 URL 替换。

### `redis`

保存缓存、Sidekiq 队列和短期状态。它不是长期事实来源。

正式切换 `WEB_DOMAIN` 时执行 `FLUSHDB`，清除旧 origin 派生缓存和队列；恢复旧 PostgreSQL 快照时也要清 Redis。

### `nginx`

唯一内部 Web 入口：

- 普通请求转给 `web:3000`；
- streaming 路径转给 `streaming:4000`；
- 保留公网 HTTPS、真实客户端 IP 和 WebSocket 头；
- 本机调试入口限制在 `127.0.0.1:8080`；
- 配置不写死公网域名，因此换门牌通常无需 reload。

### `cloudflared`

从家中电脑主动连接 Cloudflare：

- 家庭路由器无需端口映射；
- 家庭公网 IP 不直接暴露；
- 公网 HTTPS 由 Cloudflare 处理；
- Tunnel token 只在本机 `.env`；
- dashboard-managed route 决定哪些公网域名进入同一 `nginx:80`。

### CMX（计划中，未实现）

CMX 是未来的移动网页体验层，不是新的数据后端。

必须：

- 与 Mastodon 同源；
- 使用 Session/CSRF 或页面派发 token；
- REST 使用相对路径；
- streaming 与媒体从当前 origin/后端元数据获得；
- 不硬编码 `WEB_DOMAIN`；
- 不注册长期绑定某门牌的 OAuth application。

### AI / MCP（计划中，未实现）

AI 可作为正式居民账号，或通过窄权限 MCP 工具行动：

- 独立身份、独立 Token、独立发布历史；
- 只暴露发布、媒体、回复、时间线和可见性动作；
- 不直连 PostgreSQL，不使用 Owner Token；
- 默认不读取全站或自动回应所有动态。

## 内容权限模型

Mastodon 底层可见性为基础，CMX 以后映射为更自然的产品语义：

```text
仅自己
指定圈子
实例居民可见
明确公开
```

“公开”是否允许匿名互联网查看必须由 CMX/实例策略明确决定，不因 AI 选择某个底层值而默认泄露内容。

## 数据分层

```text
Docker named volumes
├─ pi-os_postgres_data   PostgreSQL
└─ pi-os_redis_data      Redis

D:\AI\PI-Personal-Instance-OS
├─ data\media            上传图片和视频
├─ backups               数据库导出、媒体归档和密钥快照
├─ logs                   自动启动日志
├─ .env                   Docker / Tunnel 密钥
└─ .env.production        身份、门牌和 Mastodon 加密密钥
```

GitHub 只保存“如何建造世界”，不保存真实内容。

## 域名切换拓扑

### Prepare

```text
旧 WEB_DOMAIN 仍是主 origin
新域名加入 Cloudflare + ALTERNATE_DOMAINS
→ 只验证 Tunnel、HostAuthorization、HTML/API 基础 GET
```

这时 URL、CSP、Cookie、WebAuthn 和主 WSS 仍属于旧 origin，不能当作完整切换成功。

### Switch

```text
备份
→ 停 web/streaming/sidekiq
→ WEB_DOMAIN / STREAMING_API_BASE_URL 切到新门牌
→ 旧门牌进入 ALTERNATE_DOMAINS
→ Redis FLUSHDB
→ recreate web/streaming/sidekiq
→ 新 origin 完整登录、旧数据、发文、发图、streaming smoke
```

### Release

```text
清空 ALTERNATE_DOMAINS
→ recreate 应用进程
→ 删除旧 Cloudflare route
```

## 私密边界

配置为：

- 关闭公开注册；
- `LIMITED_FEDERATION_MODE=true`；
- `AUTHORIZED_FETCH=true`；
- `DISALLOW_UNAUTHENTICATED_API_ACCESS=true`；
- 不加入公开联邦；
- AI/Bot 只使用独立账号和最小权限接口。

这提高控制权，但不是端到端加密。Cloudflare、服务器系统和 Owner 管理权限仍在信任边界内。

## 故障影响

- 家中断网：手机暂时无法访问，数据仍在本机。
- Tunnel/当前域名失效：公网门牌不可达，数据库与媒体不受影响。
- Web 挂掉：登录、网页和 REST 不可用。
- Streaming 挂掉：网页可用但实时更新异常。
- Sidekiq 挂掉：图片处理和异步任务积压。
- PostgreSQL 丢失：账号和动态主体丢失，必须恢复备份。
- `.env.production` 密钥丢失：部分加密数据和通知能力可能不可恢复。
- 域名切换后旧 Session/Push/Service Worker/passkey 不可继承，需要在新 origin 重建。

## 当前停止线

基础 Beta 只验证：启动、手机网页登录、文字/图片发布、旧数据读取、时间线、streaming、重启恢复、备份和自动启动。

独立 CMX、AI居民、MCP、内容权限中文语义和公开博客出口属于后续独立增量。