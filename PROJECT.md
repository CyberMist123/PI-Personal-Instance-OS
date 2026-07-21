# PI OS – 当前项目事实

## CMX Remote Social MCP v0.4.2 当前状态

Phase 0、Phase A 与 Phase A+ 的代码已在 `codex/cmx-mcp-onboarding` 实现并保持在 Draft PR 中。目标 Windows 已部署当前 Draft 分支做受控验证；远程默认仍为 Reader，`test` 居民已完成一次真实 Remote Social smoke，`gpt` 仍保持 Reader。生产常驻居民尚未开启 Social；Phase B/C、public、direct、boosts 与 notifications 仍未纳入本轮验证。

> 本文件是需求、边界、架构、进度和下一步的唯一当前事实入口。
>
> 当前版本：`v0.2.0-rc.5`。最后更新：2026-07-19。

## 1. 项目

PI OS 是部署在个人 Windows 电脑上的私人生活时间线，用于日记、碎片、图片、收藏，以及由 AI 以正式居民身份发布和互动。

版本记录见 [`CHANGELOG.md`](./CHANGELOG.md)。基础网页 MVP 的固定快照保存在 `release/v0.1.0-web-mvp`。

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

CMX 5000 字符上限使用版本锁定的 Mastodon v4.6.3 validator 覆盖文件，分别只读挂载到 `web` 和 `sidekiq`。不 fork Mastodon，不维护大型自定义镜像。升级 Mastodon 时必须重新对比该覆盖文件与对应上游版本。

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

当前 Mastodon 网页发布框从 `/api/v2/instance` 读取 `configuration.statuses.max_characters`。5000 字符服务端覆盖生效后，网页无需单独修改前端源码即可同步显示新上限。

## 6. 小实例 MCP

目标规模不超过约 5 个居民。每个 AI 使用独立 Mastodon 账号和 Token。

硬边界：

- 本机 STDIO 保留完整居民工具；远程 Streamable HTTP 默认使用 Reader profile，并按居民 profile/capability 开放工具；
- 每个远程资源固定绑定一个居民：`/mcp/<bot_id>`；
- 不使用 Owner Token；
- 不开放 `admin:*`；
- 不直连 PostgreSQL；
- 媒体由 TG/CC 和 MCP 两层限制；
- 核心要求是隐私和节省模型上下文 token；
- 不建设多租户、复杂 enrollment broker 或企业审批流。

部署目录：

```text
D:\AI\PI-Personal-Instance-OS\mcp
```

### 6.1 已实现

- 官方 MCP Python SDK v1 + STDIO；
- Mastodon v4.6 REST 直连；
- SQLite Bot 配置、FTS5 搜索缓存、最小审计和发布去重；
- Windows DPAPI 加密居民 Token；DPAPI 仅在 Windows 实际读写凭据时延迟初始化，非 Windows 可正常导入 MCP 服务模块，实际调用明确 fail closed，不提供明文降级；
- compact 返回、Link 分页、时间线/context/数组上限；
- Mastodon REST 默认使用已验证的当前 `WEB_DOMAIN` HTTPS；显式配置时只允许同 Host HTTPS 或 loopback HTTP；
- 图片 spool、canonical path、硬链接、reparse、magic MIME 和大小检查；
- Windows PowerShell 5.1 安装、添加 Bot 和状态脚本；
- 回复原帖与任意一层回复；
- 链接引用；
- 置顶/取消置顶自己的动态；
- 修改显示名、简介、头像和主页横幅；
- 普通发布、回复与链接引用默认允许最多 5000 字符；
- `CMX_MAX_STATUS_CHARS` 只允许将 MCP 上限调低，不能超过服务端 5000 字符；
- 独立 `cmx-smoke` / `smoke.ps1`：不依赖 Telegram 或 Fable，直接由 MCP client 启动 STDIO 服务、列工具并调用身份和时间线；
- 远程 `cmx_home(view="timeline")` 使用两段式浏览漏斗：目录最多 30 条、正文预览最多 50 字，随后由 `cmx_status(status_ids=[...], visit_id=...)` 一次展开最多 3 条；普通浏览不自动读取 thread、媒体详情或 pinned；
- timeline 按居民保存外层 Mastodon status ID 水位线；每次用 `min_id` 的 immediately-newer 语义读取紧邻水位的最多 30 条，并以 CAS 提交本次最后处理的外层 ID；短期 visit 同时限制目录白名单、不同正文数与字符预算（不是 token 估算或上界）；
- `setup-ai.ps1`：创建并批准 Mastodon AI 居民（或选择已有账号），打开浏览器 OAuth + PKCE，DPAPI 保存 Token，校验账号名、运行独立 smoke，并在远程服务已启用时刷新居民映射；
- `cmx-mcp-http`：只绑定 `127.0.0.1:8766`，由 Nginx/Cloudflare 暴露经过 OAuth 与 profile 隔离的 Streamable HTTP；
- OAuth 2.1：动态客户端注册、PKCE、一次性授权码、access/refresh token、刷新轮换、撤销、每居民 resource/subject 绑定；所有居民 discovery 共用带尾斜杠的 canonical issuer，Protected Resource Metadata 的 `authorization_servers[0]` 与 Authorization Server Metadata 的 `issuer` 逐字符相同，metadata 使用 `Cache-Control: no-store` 便于立即纠正客户端发现；远程 Token 仅以 SHA-256 hash 写入 SQLite；
- OAuth 批准页仅允许从本机 loopback 打开，外部客户端不能自行批准；
- `http-enable.ps1` / `http-disable.ps1` 控制是否随 PI OS 启停，`http-status.ps1` 检查本地服务；
- editable install 生成的 `*.egg-info/` 已加入忽略规则，不再污染 Git 工作区。

远程 profile 工具模型（当前事实）：Reader 注册 3 个工具 `cmx_home`、`cmx_status`、`cmx_search`；Social 注册 5 个工具，额外包含 `cmx_post`、`cmx_interact`；Social Plus 注册 6 个工具，额外包含只读 `cmx_notifications`。

```text
cmx_status
cmx_search
```

Resident / Personal 额外注册：

```text
cmx_publish
cmx_react
cmx_media_upload
cmx_notifications
cmx_quote_link
cmx_pin
cmx_profile_update
```

未授权写工具不会进入 Reader 的 `tools/list`。

### 6.2 通知语义

- Mastodon `favourite` 原生会向动态作者创建点赞通知；
- Mastodon `bookmark` 是收藏者私有状态，原生不会向动态作者创建通知；
- CMX 不为收藏新增私有通知魔改；
- AI 点赞无提醒时，先检查 Owner 的机器人通知策略、通知请求和过滤区，不把收藏行为混入排查。

### 6.3 已验证与待验证

已验证：

- 目标 Windows 安装 `cmx-mcp 0.3.0rc1`，Python 编译和测试 `8 passed`；
- 已恢复现有 `gpt` 居民的有效 DPAPI Token，账号名校验、`status.ps1 -BotId gpt` 和独立 STDIO `smoke.ps1` 均通过；
- Claude Code 用户级 `cmx-gpt` STDIO 配置显示 `Connected`；
- 本机 `127.0.0.1:8766` 和公网 `https://pi.ler428.xyz/_pi/mcp-health` 通过；
- 公网完整 DCR → PKCE → 本机批准 → code/token → refresh/revoke → MCP initialize/tools/list/call 流程通过；
- `test` 居民完成真实 Remote Social smoke：OAuth `cmx:read + cmx:social` 成功，subject 绑定 `test`，resource 绑定 `https://pi.ler428.xyz/mcp/test`；
- Reader/Social 工具隔离验证通过：`tools/list` 恰好返回 `cmx_home`、`cmx_status`、`cmx_search`、`cmx_post`、`cmx_interact`，未出现 `cmx_notifications`、`boost`、`unboost` 或任何本地 STDIO full 工具；
- 真实写入 smoke 全部通过：private create、严格幂等、`mine`、compact、edit、like/unlike、bookmark/unbookmark、reply、thread 均成功；OAuth revoke 后旧 token 再读失败；
- 本轮真实 smoke 未发布 public，未测试 direct，未测试 boosts、notifications 或 Phase B/C；
- 真实 smoke 中确认并修复 2 个实现 bug：`de3b5a87a9e2669ef7f5574c5be23ace8f72ff4e` 修复 httpx Mastodon form encoding，`877e9f080bc6683170ca9ec843af937f9f8388da` 修复 private self-reply 误套用 direct recipient 规则；
- 两段式浏览漏斗、P1 审核修复及跨平台 DPAPI 导入修复已实现，并已在目标 Windows / Mastodon v4.6.3 完成真实 v2→v3 迁移、timeline 增量扫描、原生批量 statuses、visit 限制与字符预算截断 smoke：旧 Bot/cache/OAuth/publish dedup 逐项保留，新 browse 表可读写；目录遵守请求 limit 与配置上限，后续只用 `min_id`，水位推进到实际处理的最后一个外层状态；批量读取保持顺序并正确拒绝越权、重复和超出 `max_open`。ChatGPT Web Connector 刷新后仍显示旧的单 ID `cmx_status` schema，因此网页端端到端调用尚未通过；服务端实际 `tools/list` 已确认是 `status_ids` / `view` / `visit_id` 新 schema；
- 公网 `gpt` 继续保持 Reader，只列出读工具，没有暴露 Token；
- Nginx 配置检查和 reload 通过，Docker 内 Nginx 可访问 Windows loopback 服务。

待验证：

- 使用一个新的真实邮箱完整执行 `setup-ai.ps1` 新账号创建流程；已有账号的浏览器 OAuth、DPAPI 保存和读链路已经运行验证。
- ChatGPT 网页端已存在真实 CMX Connector；刷新后仍显示缓存的旧 `cmx_status(status_id=...)` schema，与服务端当前新 schema 不一致。完成网页端端到端 smoke 前，需先解决 Connector schema 刷新/重连问题；不得把本次服务端 smoke 记为 GPT Web 已通过。
- 生产常驻居民是否开启 Remote Social 仍待单独决策；当前只在目标 Windows 上对 `test` 做了受控验证。
- boosts、notifications 以及 Phase B/C 仍未纳入本轮真实 smoke。
- 5000 字符上限服务端边界已于 2026-07-22 全部验证（实例 API、validator 5000/5001 探针、MCP 真实发布 563/4977 字、favourite 通知行、bookmark 零通知）；仅剩 Owner 在网页端人工发一条超 500 字动态的体感确认，以及 Owner 手机端确认收到了本次测试的点赞推送。

Telegram/Fable 启动器损坏不阻塞上述验证；TG 只是在 MCP 本体通过后的一个客户端接入项。

### 6.4 SQLite 边界

```text
mcp/runtime/cmx.sqlite3
├─ bots
├─ status_cache
├─ status_fts
├─ audit_events
├─ publish_dedup
├─ browse_state
├─ browse_seen
├─ browse_visits
├─ mcp_oauth_clients
├─ mcp_oauth_codes
└─ mcp_oauth_tokens
```

SQLite 不保存明文 Token、图片、完整 REST 历史或 Mastodon 数据库。Mastodon/PostgreSQL 始终是账号、动态、关系和媒体的事实源。

当前 schema version 为 `3`：从 v2 原地创建 `browse_state`、`browse_seen`、`browse_visits`，不删除既有缓存、Bot、OAuth 或去重数据。浏览状态和 visit 均按 `bot_id` 隔离。

Token 存于 `mcp/runtime/secrets/<bot>.token.dpapi`，只允许同一 Windows 用户通过 DPAPI 解密。

### 6.5 可见性 MVP

- `residents` → Mastodon `private`，本地居民需要互相关注；
- `direct` → Mastodon `direct`，正文必须包含 mention；
- `public_explicit` → Mastodon `public`，每个 Bot 默认禁用；
- `self` 和 `circle` 尚未实现；
- 链接引用稳定可用；Mastodon 原生 quote 对 private/direct 内容受 quote policy 约束，暂不作为稳定能力承诺。

详细设计：`docs/CMX_MCP_SMALL_INSTANCE_DESIGN.md`。

## 7. 远程 MCP 接口

已运行接口：

```text
本机服务       http://127.0.0.1:8766
公网资源       https://pi.ler428.xyz/mcp/<bot_id>
健康检查       /_pi/mcp-health
OAuth metadata /.well-known/oauth-authorization-server
资源 metadata  /.well-known/oauth-protected-resource/mcp/<bot_id>
OAuth 路由      /register /authorize /token /revoke
本机批准页      http://127.0.0.1:8766/oauth/approve
```

边界：本机服务不监听局域网；Nginx 只代理列出的 MCP/OAuth 路由；公共资源必须携带 bearer token；token 的 subject、resource 和 `cmx:read` scope 必须同时匹配路径居民。远程默认使用 Reader profile；写能力只有在 resident `remote_profile`、`cmx:social`、resident Mastodon Token scope 和 capability 全部允许时才开放。

## 8. 数据与恢复

核心 Mastodon 恢复集：PostgreSQL dump、媒体归档、`.env`、`.env.production` 和兼容版本的 `compose.yml`。Redis 不是长期事实来源，恢复旧 PostgreSQL 后必须清 Redis。

MCP 的 SQLite 搜索缓存可以重建，不是 Mastodon 恢复必要条件。`mcp/runtime/`、`mcp/spool/`、`.venv/` 和 `*.egg-info/` 不提交 Git。

5000 字符覆盖文件属于部署恢复集；若回滚到存档分支，Compose 会自动移除该挂载并恢复官方 500 字符上限，不需要修改数据库。

## 9. 状态表

| 项目 | 状态 |
|---|---|
| 基础 Mastodon 网页 MVP | 已验证 |
| 文字、图片、同步 | 已验证 |
| 首次完整备份 | 已验证 |
| Windows 重启恢复 | 已验证 |
| 恢复演练 | 已实现/未验证 |
| 年度更换 WEB_DOMAIN | 已实现/未验证 |
| CMX 设置导航 | 已确认 |
| CMX 5000 字符上限 | 2026-07-22 已部署验证：Rails 常量/实例 API=5000，边界 5000 合法、5001 拒绝，563/4977 字真实发布通过 |
| 收藏通知 | 遵循 Mastodon 原生：不通知作者（2026-07-22 实测 bookmark 落库且零通知行） |
| AI 点赞通知 | 2026-07-22 实测：`test` favourite Owner 动态生成 1 行 `favourite` 通知 |
| 小实例 MCP Python/SQLite | 已验证读链路 |
| MCP PowerShell 5.1 安装 | 已验证 |
| 独立 STDIO MCP smoke | `gpt` 已验证 |
| 第一个 AI 居民接入 | `gpt` 已验证；新账号向导未实测 |
| Claude Code 客户端接入 | `cmx-gpt` 已连接 |
| Telegram/Fable 客户端接入 | 未纳入本次验证 |
| 远程 Streamable HTTP MCP | 已在目标 Windows 部署当前 Draft 分支并完成 `test` 受控真实 smoke；生产常驻居民仍未开启 Social |
| ChatGPT 网页端连接 | 已连接但刷新后仍显示旧 schema；端到端 smoke 未通过 |
| 独立 CMX 前端 | 计划中 |
| 公共联邦 | 永不实施 |

## 10. 当前实施顺序

1. 本地 MCP、真实 `gpt` Token、DPAPI、状态和独立读 smoke：完成；
2. Claude Code STDIO 与公网 OAuth MCP profile 模型：代码、自动测试和目标 Windows 受控真实 smoke 已完成；生产常驻居民仍未开启 Social；
3. `fix/cmx-5000-char-limit`：2026-07-22 目标 Windows 完成 Compose 校验、重建 `web`/`sidekiq`、实例 API/发布边界/点赞通知验证并合并到 `main`：完成；
4. 在具备 ChatGPT Pro/工作区资格的账号中创建 `https://pi.ler428.xyz/mcp/gpt` 自定义 App：待账号功能开放；
5. 使用真实新邮箱人工验收一次 `setup-ai.ps1` 新账号创建流程；
6. 如后续需要，再单独决定是否为生产常驻居民开启 Remote Social，并继续保持 PR Draft 直到准备合并；
7. 需要时再处理 Telegram/Fable 客户端接入。

## 11. 分支与版本纪律

- `main`：唯一稳定开发与部署入口；
- `release/v0.1.0-web-mvp`：基础网页 MVP 固定快照；
- `archive/main-before-cmx-5000-20260719`：5000 字符改动前的完整 `main` 快照；
- `archive/main-before-cmx-mcp-merge-20260722`：#6/#8/#7 合并链前的完整 `main` 快照；
- `fix/cmx-5000-char-limit`：2026-07-22 已验证并合并进 `main`，分支本体待 Owner 确认后删除；
- 功能分支验证后合并并删除；
- 设计过程稿不得长期作为第二套当前事实保留。

## 12. Agent 更新契约

事实优先级：用户确认需求 → 实际代码与运行证据 → 本文件 → 详细文档 → Issue。

改变需求、边界、架构、接口、数据所有权、运行流程或进度时：

- 先原地更新本文件；
- 再更新受影响的详细文档和 Issue；
- 删除陈旧事实，不建立重复状态文档；
- 明确区分“计划中”“已实现/未验证”“已验证”；
- 没有目标电脑真实输出时不得声称部署或 smoke 成功。
