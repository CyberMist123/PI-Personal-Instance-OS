# PI OS 架构说明

## 一句话

PI OS 不是重新开发社交平台内核，而是把 Mastodon v4.6.4 当作稳定的私人时间线后端，用 Docker 管理依赖、Nginx 统一入口、Cloudflare Tunnel 供手机浏览器访问，并把永久内部身份与可替换公网门牌分离。

当前网页是 Mastodon Web。独立 CMX 前端尚未加入；以后作为同源网页层接入，不改变数据库和核心容器职责。

## 域名分层

```env
LOCAL_DOMAIN=pi.invalid
WEB_DOMAIN=<WEB_DOMAIN>
STREAMING_API_BASE_URL=wss://<WEB_DOMAIN>
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
- 搜索框保留 Mastodon 原生界面：Mastodon 4.6 经 Axios/XHR 请求 `/api/v2/search`，Nginx 精确代理到同源 `/files/search?format=mastodon`。当前网页 token 首次经该共享搜索层分页读取可见 home REST 动态到既有 SQLite，后续以独立水位的 `min_id` 读取新增动态；SQLite LIKE 优先，结果不足时在同一 cache 做 RapidFuzz 中文 typo 与 pypinyin 全拼/首字母检索正文、媒体 alt 与已持久化 OCR/vision 文字，并返回原生的四个结果数组；
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
- 明确的 MCP/OAuth 路径转给 Windows `host.docker.internal:8766`；
- 向 Mastodon 网页注入带版本键的同源 `/files/voice.js`，同时承载语音与图片识别增量；
- 对 `/sw.js` 强制 `no-cache, no-store, must-revalidate`，并将注册 URL 绑定 Mastodon 版本键，避免门牌前的旧 Service Worker 继续引用已删除 chunk；
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

### 独立 CMX（计划中，未实现）

CMX 是未来的移动网页体验层，不是新的数据后端。

必须：

- 与 Mastodon 同源；
- 使用 Session/CSRF 或页面派发 token；
- REST 使用相对路径；
- streaming 与媒体从当前 origin/后端元数据获得；
- 不硬编码 `WEB_DOMAIN`；
- 不注册长期绑定某门牌的 OAuth application。

已实现的网页增量不是独立前端：Nginx 注入同源脚本，分别提供语音/图片与本地搜索能力。图片链为：

```text
POST /api/v2/media 成功 → Blob 写入当前浏览器 IndexedDB outbox
POST /api/v1/statuses 成功 → 后台 POST /files/recognize
本机 RapidOCR → 可选 Gemini 校正/画面理解 → PUT /api/v1/statuses/<id> 写回媒体 alt
网页搜索 → 当前页 token 首次 REST 全量刷新、后续 `min_id` 增量刷新 → SQLite 正文、媒体 alt、OCR/vision 子串检索
```

发布不等识图；失败保留 outbox 后续重试。页 bearer 只在当次同源请求中临时使用，不入 SQLite。识别结果按图片 SHA-256 共享缓存；Gemini 尝试按 UTC 日计数，超限仅降级本机 OCR。原生 App 不加载该脚本。

### AI / MCP（已实现读链路）

AI 作为正式 Mastodon 居民账号，通过每居民独立 Token 行动：

- 本地 STDIO 根据 Reader/Resident profile 注册工具；
- 公网 `/mcp/<bot_id>` 默认使用 Reader profile；OAuth 2.1 + PKCE 把 token subject/resource 绑定到该居民，Social 写能力还需 `cmx:social`、resident Token scope 与 capability 同时允许；
- Windows 服务只监听 `127.0.0.1:8766`，公网流量必须经过 Nginx/Cloudflare；
- 本地居民 Token 用 DPAPI 加密；远程 OAuth token 仅保存 SHA-256 hash；
- 不直连 PostgreSQL，不使用 Owner Token 或 `admin:*`；
- Mastodon/PostgreSQL 仍是账号、动态、关系、互动和媒体的事实源；
- 默认不读取全站或自动回应所有动态。

真实 `gpt` 的 STDIO、Claude Code 和公网 OAuth Reader 链路已验证；本地 Resident 写工具与新账号向导仍待人工验收。

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
├─ mcp\runtime           Bot 配置、搜索/图片识别缓存、Gemini 日额计数、OAuth hash 与 DPAPI Token 文件（不进 Git）
├─ mcp\spool             每居民允许上传的临时媒体目录（不进 Git）
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
