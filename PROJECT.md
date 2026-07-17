# PI OS — 当前项目事实

> 本文件是需求、边界、架构、进度和下一步的唯一当前事实入口。最后更新：2026-07-17。

## 1. 项目

PI OS 是部署在个人 Windows 电脑上的私人生活时间线，用于日记、碎片、图片、收藏，以及以后由 AI 以正式居民身份发布和互动。

## 2. 已验证基础实例

- Mastodon v4.6.3 官方容器；
- 手机和 PC 可通过 HTTPS 登录；
- 文字、图片和跨设备同步正常；
- 公开注册关闭，不加入公开联邦；
- PostgreSQL、Redis、媒体和密钥保存在本机；
- Cloudflare Named Tunnel 只提供网页入口，家庭路由器不开放入站端口；
- `status.ps1` 全链路曾通过；
- `backup.ps1` 已显示 `Backup completed`；
- Windows 重启后 Docker Desktop 与 PI OS 双层自启恢复网页和旧内容。

## 3. 不变量

```env
LOCAL_DOMAIN=pi.invalid
WEB_DOMAIN=pi.ler428.xyz
STREAMING_API_BASE_URL=wss://pi.ler428.xyz
ALTERNATE_DOMAINS=
```

- `LOCAL_DOMAIN` 永久固定为 `pi.invalid`；
- `WEB_DOMAIN` 是可替换公网门牌；
- 不对历史 ActivityPub URI 做全库字符串替换；
- 不开启公共联邦；
- 不运行 `docker compose down -v`；
- 不提交 `.env`、运行数据、日志、备份或凭据。

## 4. 基础架构

```text
浏览器 → WEB_DOMAIN → Cloudflare Tunnel → cloudflared → nginx
  ├─ web:3000
  └─ streaming:4000

sidekiq / PostgreSQL / Redis / data/media
```

Windows 启动链：

```text
Windows 登录
→ Docker Desktop 静默启动 Linux engine
→ PI-OS-Autostart 等待 Docker
→ start.ps1 启动 tunnel profile 和全部服务
→ 本机健康检查与日志
```

`start.ps1`、计划任务脚本和相关 bat 都是有效运维文件，必须保留。

## 5. CMX 网页

独立 CMX 前端尚未实现。未来必须同源、使用相对 REST、网页 Session/CSRF，不写死公网域名。

设置入口已确认：

```text
偏好设置
CMX 设置

邀请用户
AI 居民
开发
```

“邀请用户”管理真人注册链接；“AI 居民”管理 AI 账号、权限、MCP 配置和媒体目录。

## 6. 小实例 MCP

目标规模不超过约 5 个居民。每个 AI 使用独立 Mastodon 账号和 Token。

硬边界：

- 只走本机 STDIO；
- 不新增公网 MCP；
- 不使用 Owner Token；
- 不开放 `admin:*`；
- 不直连 PostgreSQL；
- 媒体由 TG/CC 和 MCP 两层限制；
- 核心要求是隐私和节省模型上下文 token；
- 不建设多租户、复杂 enrollment broker 或企业审批流。

实施分支：`feature/cmx-mcp-sqlite-mvp`

部署目录：

```text
D:\AI\PI-Personal-Instance-OS\mcp
```

### 6.1 已实现/未在目标电脑验证

- 官方 MCP Python SDK v1 + STDIO；
- Mastodon v4.6 REST 直连；
- SQLite Bot 配置、FTS5 搜索缓存、最小审计和发布去重；
- Windows DPAPI 加密居民 Token；
- compact 返回、Link 分页、时间线/context/数组上限；
- loopback REST，发送当前 `WEB_DOMAIN` 作为 Host；
- 图片 spool、canonical path、硬链接、reparse、magic MIME 和大小检查；
- Windows PowerShell 5.1 安装、添加 Bot 和状态脚本。

Reader 注册：

```text
cmx_identity
cmx_timeline
cmx_status
cmx_search
```

Resident / Personal 额外注册：

```text
cmx_publish
cmx_react
cmx_media_upload
cmx_notifications
```

### 6.2 SQLite 边界

```text
mcp/runtime/cmx.sqlite3
├─ bots
├─ status_cache
├─ status_fts
├─ audit_events
└─ publish_dedup
```

SQLite 不保存明文 Token、图片、完整 REST 历史或 Mastodon 数据库。Mastodon/PostgreSQL 始终是账号、动态、关系和媒体的事实源。

Token 存于 `mcp/runtime/secrets/<bot>.token.dpapi`，只允许同一 Windows 用户通过 DPAPI 解密。

### 6.3 可见性 MVP

- `residents` → Mastodon `private`，本地居民需要互相关注；
- `direct` → Mastodon `direct`，正文必须包含 mention；
- `public_explicit` → Mastodon `public`，每个 Bot 默认禁用；
- `self` 和 `circle` 尚未实现。

详细设计：`docs/CMX_MCP_SMALL_INSTANCE_DESIGN.md`。

## 7. 数据与恢复

核心 Mastodon 恢复集：PostgreSQL dump、媒体归档、`.env`、`.env.production` 和兼容版本的 `compose.yml`。Redis 不是长期事实来源，恢复旧 PostgreSQL 后必须清 Redis。

MCP 的 SQLite 搜索缓存可以重建，不是 Mastodon 恢复必要条件。`mcp/runtime/`、`mcp/spool/` 和 `.venv/` 不提交 Git。

## 8. 状态表

| 项目 | 状态 |
|---|---|
| 基础 Mastodon 网页 MVP | 已验证 |
| 文字、图片、同步 | 已验证 |
| 首次完整备份 | 已验证 |
| Windows 重启恢复 | 已验证 |
| 恢复演练 | 已实现/未验证 |
| 年度更换 WEB_DOMAIN | 已实现/未验证 |
| CMX 设置导航 | 已确认 |
| 小实例 MCP Python/SQLite | 已实现/未验证 |
| MCP PowerShell 5.1 安装脚本 | 已实现/未验证 |
| 第一个 AI 居民接入 | 计划中 |
| 独立 CMX 前端 | 计划中 |
| 公共联邦 | 永不实施 |

## 9. MCP 六步实施

1. 小实例设计和数据边界：完成；
2. Python、SQLite、FTS5 和工具：完成/未实测；
3. compact、分页、context、去重、媒体和隐私限制：完成/未实测；
4. PowerShell 5.1 安装、添加 Bot、状态脚本：完成/未实测；
5. 目标 Windows 拉取分支，在 `mcp/` 安装并添加第一个 AI：待执行；
6. 真实 smoke 后修复确定问题，再决定是否合并：待执行。

当前只有代码静态检查和 2 个本地单元测试通过证据，没有目标 Windows、真实 Token 或 MCP 客户端运行证据。

## 10. Agent 更新契约

事实优先级：用户确认需求 → 实际代码与运行证据 → 本文件 → 详细文档 → Issue。

改变需求、边界、架构、接口、数据所有权、运行流程或进度时：

- 先原地更新本文件；
- 再更新受影响的详细文档和 Issue；
- 删除陈旧事实，不建立重复状态文档；
- 明确区分“计划中”“已实现/未验证”“已验证”；
- 没有目标电脑真实输出时不得声称部署或 smoke 成功。
