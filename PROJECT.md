# PI OS — 当前项目事实

> **这是本仓库最重要的权威文件。**
>
> 所有 AI、Agent 和维护者开始工作前先读本文件。完成已确认的功能或架构修改后，必须原地更新本文件及受影响的详细文档；不得另建重复的状态、交接或架构摘要。
>
> 最后更新：2026-07-16

## 1. 项目是什么

**PI OS（π / Personal Instance OS）** 是部署在个人 Windows 电脑上的私人生活时间线。

它用于保存和浏览：

- 日记、碎碎念和生活时间轴；
- 书、电影、音乐与收藏；
- 图片、视频和朋友圈式动态；
- 以后由 AI 以正式居民身份发布的博客、日记和状态。

PI OS 与 AI OS 平行存在。动态可以无人回复；AI 不默认分析全部生活，也不是全局监控器。

## 2. 已确认的产品要求

### 当前 MVP

- 手机浏览器通过 HTTPS 访问网页，不使用 Mastodon App。
- 当前底座使用 Mastodon v4.6.3 官方容器，不 fork 上游。
- Owner 可以登录、发文字、上传图片、浏览时间线和通知。
- 数据在重启后保留，并可备份与恢复。
- 关闭公开注册和公开联邦。
- 家庭路由器不开放入站端口；公网入口使用 Cloudflare Named Tunnel。

### CMX 网页

CMX 是计划中的同源移动网页体验层。当前仓库尚未包含独立 CMX 服务，首次运行仍由 Mastodon Web 提供网页；加入 CMX 时必须满足：

- 与 Mastodon 后端同源部署；
- REST 使用相对路径，如 `/api/v1/...`；
- 使用网页登录 Session 和当前 CSRF token，或使用页面派发的当前用户 token；
- 不注册长期绑定某个公网域名的 OAuth application；
- streaming、媒体和跳转地址从当前 origin 或后端元数据获得，不在源码中写死域名；
- Service Worker、Web Push 和浏览器缓存按当前 origin 独立管理。

### AI / Bot / MCP

这是**已确认的后续需求，尚未实现**：

- AI 可以作为独立正式用户，拥有头像、主页和发布历史；
- AI 可通过独立最小权限 Token，或通过窄权限 MCP 工具登录和发布；
- MCP 只暴露读时间线、发布、上传媒体、回复和设置可见性等业务动作，不暴露 PostgreSQL 或 Owner 管理权限；
- AI 可发布博客、日记或状态，并选择仅自己、指定圈子、实例居民或明确公开的可见范围；
- AI 默认不读取全站，不因每条动态自动触发分析或回复。

## 3. 域名模型

PI OS 把永久内部身份和可替换公网门牌拆开：

```env
LOCAL_DOMAIN=pi.invalid
WEB_DOMAIN=pi.ler428.xyz
STREAMING_API_BASE_URL=wss://pi.ler428.xyz
ALTERNATE_DOMAINS=
```

### 不变量

- `LOCAL_DOMAIN` 永远是 `pi.invalid`，不得修改。
- PostgreSQL、媒体、密码和加密密钥不随公网域名变化。
- 不允许把 `pi.invalid` 当作可访问 URL。
- 使用可变 `WEB_DOMAIN` 后，实例永久保持无公开联邦；历史 `statuses.uri` 等 ActivityPub 标识可能保留创建时的旧门牌。

### 可变项

- `WEB_DOMAIN` 是手机当前访问网页的公网门牌，可以按年更换。
- `STREAMING_API_BASE_URL` 必须与当前 `WEB_DOMAIN` 同步。
- `ALTERNATE_DOMAINS` 只用于切换期接受额外 Host 和 WebFinger 兼容；它不负责 URL 生成、CSP、WebAuthn、OAuth、Cookie、Service Worker 或 streaming 主地址。

### 换域名后允许失效

- 旧书签和旧域名绝对链接；
- 旧 origin 的 Cookie、Session、CSRF token、localStorage、IndexedDB 和 Service Worker；
- 旧 Web Push 订阅；
- 绑定旧 RP ID/origin 的 WebAuthn/passkey；
- 写死旧 callback 的 OAuth application。

首版不得把 WebAuthn/passkey 作为 Owner 唯一登录或二次验证方式。

## 4. 当前部署架构

```text
手机浏览器
    │ HTTPS
    ▼
当前 WEB_DOMAIN
    ▼
Cloudflare Named Tunnel
    ▼
cloudflared → nginx:80
                 ├─ 普通网页 / Session / REST / 上传 → web:3000
                 └─ /api/v1/streaming            → streaming:4000

sidekiq     图片处理、通知和异步任务
PostgreSQL  账号、动态、关系、设置和媒体元数据
Redis       缓存、Sidekiq 队列和短期状态
data/media  图片和视频文件
```

所有服务由 `compose.yml` 管理。Cloudflare Public Hostname 必须指向 `http://nginx:80`，主网页域名不能套 Cloudflare Access。

## 5. 精确接口与配置

### 公网 HTTP

- `https://<WEB_DOMAIN>/_pi/health`：Tunnel 与 Nginx 健康检查。
- `https://<WEB_DOMAIN>/api/v2/instance`：实例元数据；`domain` 应为 `pi.invalid`。
- `https://<WEB_DOMAIN>/api/v1/...`：标准 Mastodon REST API。
- `wss://<WEB_DOMAIN>/api/v1/streaming...`：实时更新。
- 其余登录、Session、媒体和网页均为标准 Mastodon 路径。

### 本地与 Docker

- `http://127.0.0.1:8080`：仅本机可访问的 Nginx 入口。
- `web:3000`、`streaming:4000`、`db:5432`、`redis:6379`：Docker 内部服务。

### 关键环境变量

- `LOCAL_DOMAIN=pi.invalid`：永久身份锚点。
- `WEB_DOMAIN=<当前公网门牌>`：可替换访问域名。
- `STREAMING_API_BASE_URL=wss://<当前公网门牌>`：必须与 `WEB_DOMAIN` 同步。
- `ALTERNATE_DOMAINS=<短期旧/新门牌>`：切换期 Host 兼容。
- `LIMITED_FEDERATION_MODE=true`。
- `AUTHORIZED_FETCH=true`。
- `DISALLOW_UNAUTHENTICATED_API_ACCESS=true`。

### 运维脚本

- `setup.ps1 -AccessDomain <domain>`：首次初始化；固定 `LOCAL_DOMAIN=pi.invalid`。
- `change-access-domain.ps1 -Phase Prepare -NewDomain <domain>`：加入过渡 Host并做基础预检。
- `change-access-domain.ps1 -Phase Switch -NewDomain <domain>`：正式切换 `WEB_DOMAIN`、清缓存、重建应用进程。
- `change-access-domain.ps1 -Phase Release`：移除旧 `ALTERNATE_DOMAINS`。
- `start.ps1` / `stop.ps1`：启动和停止，不删除数据。
- `status.ps1`：检查容器、本地链路、身份域名、当前公网门牌、streaming 和 Git 安全。
- `backup.ps1`：暂停应用后导出并验证 PostgreSQL 和媒体，再恢复运行。
- `install-autostart.ps1` / `安装开机自启.bat`：安装 Windows 登录后自动启动。

## 6. 数据所有权与恢复

```text
Docker named volumes
├─ pi-os_postgres_data
└─ pi-os_redis_data

D:\AI\PI-Personal-Instance-OS
├─ data\media
├─ backups
├─ logs
├─ .env
└─ .env.production
```

核心恢复集：PostgreSQL dump、媒体归档、`.env`、`.env.production` 和兼容版本的 `compose.yml`。

Redis 不是长期事实来源。恢复旧 PostgreSQL 快照后必须清 Redis，避免旧缓存和 Sidekiq 队列引用不存在的数据。

绝对禁止 `docker compose down -v`。绝不提交密钥、运行数据、日志或备份。

## 7. 运行流程

### 首次部署

```text
Cloudflare 添加当前 WEB_DOMAIN → http://nginx:80
→ clone 到 D:\AI\PI-Personal-Instance-OS
→ setup.ps1 -AccessDomain <domain>
→ 保存只显示一次的 Owner 密码
→ status.ps1
→ 手机浏览器登录、发文字和图片、检查时间线与实时更新
→ backup.ps1
→ 安装开机自启
```

### 更换公网门牌

```text
Cloudflare 先添加新域名
→ Prepare：备份、把新域名加入 ALTERNATE_DOMAINS、基础 Host/TLS/GET 预检
→ Switch：再次备份、切换 WEB_DOMAIN、同步 streaming URL、FLUSHDB、重建 web/streaming/sidekiq
→ 在新域名完整登录/旧数据/发文/发图/streaming smoke
→ 临时阻断旧域名，确认新页面不暗中依赖旧 origin
→ 过渡期结束后 Release，并删除旧 Cloudflare route
```

不对 `statuses.uri` 等历史字段执行全库字符串替换。

## 8. 功能与进度表

状态含义：`已实现/未实测` 表示 GitHub 代码已写入，但目标 Windows 电脑尚未运行验证。

| 项目 | 状态 | 当前事实 / 验收证据 |
|---|---|---|
| Docker/Mastodon 基础栈 | 已实现/未实测 | Compose、Nginx、Web、Streaming、Sidekiq、PostgreSQL、Redis、cloudflared 已配置 |
| 固定 `LOCAL_DOMAIN=pi.invalid` | 已实现/未实测 | env 模板与 `setup.ps1` 固定写入并校验 |
| 可替换 `WEB_DOMAIN` | 已实现/未实测 | setup、status 与切换脚本已按双域名模型设计 |
| Owner 创建与关闭注册 | 已实现/未实测 | `--confirmed --approve --role Owner`；注册关闭 |
| 备份与恢复 | 已实现/未实测 | PostgreSQL/媒体校验备份；恢复时清 Redis |
| Windows 登录后自动启动 | 已实现/未实测 | 计划任务脚本已存在 |
| 手机 Mastodon 网页 MVP | 待本地部署验证 | 登录、发文、发图、时间线、streaming、重启恢复 |
| 独立 CMX 前端 | 计划中 | 本仓库尚无 CMX 服务；必须同源 Session、相对 API |
| 内容可见性中文语义 | 计划中 | 仅自己 / 指定圈子 / 居民可见 / 明确公开 |
| AI 正式居民账号 | 计划中 | 独立账号、独立最小 Token，不直连数据库 |
| AI MCP 发布接口 | 计划中 | 只暴露窄业务动作，权限与账号隔离 |
| 公共联邦 | 永不实施 | 与可变 `WEB_DOMAIN` 的历史 URI 策略冲突 |

## 9. 当前下一步

1. 将 Cloudflare Public Hostname `pi.ler428.xyz` 指向 `http://nginx:80`。
2. clone 到 `D:\AI\PI-Personal-Instance-OS`。
3. 运行 `setup.ps1 -AccessDomain pi.ler428.xyz`。
4. 完成一次 `status.ps1` 和手机浏览器人工 smoke。
5. 运行一次备份并安装自动启动。
6. 验收后停止扩范围；CMX 与 AI/MCP 作为后续独立增量。

## 10. Agent 更新契约

事实优先级：

1. 用户已确认的需求和边界；
2. 实际代码、配置和运行验证；
3. 本文件；
4. 详细文档；
5. Issue 与历史讨论。

任何任务只要改变需求、边界、架构、接口名、数据所有权、运行流程或进度状态，就必须执行 `skills/project-doc-sync/SKILL.md`：

- 先更新本文件中的当前事实和进度表；
- 再更新受影响的 `docs/MVP_SCOPE.md`、`docs/ARCHITECTURE.md`、部署/恢复文档；
- 更新当前 Issue 的剩余步骤；
- 删除或替换陈旧描述，不并存两套架构；
- 明确区分“计划中”“已实现/未实测”“已运行验证”；
- 没有真实输出时不得声称部署成功。