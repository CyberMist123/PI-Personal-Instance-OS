# PI OS – 当前项目事实

## CMX Remote Social MCP 当前状态

Phase 0、Phase A 与 Phase A+ 已随 #6/#8/#7 合并链于 2026-07-22 进入 `main`。目标 Windows 已部署并完成受控验证；未配置的远程默认仍为 Reader，`test` 与 `gpt` 两个居民的 `remote_profile` 现均为 `social`（`boosts`/`notifications` 关闭），`test` 已完成一次真实 Remote Social smoke。Phase B/C、public、direct、boosts 与 notifications 仍未纳入本轮验证。

> 本文件是需求、边界、架构、进度和下一步的唯一当前事实入口。
>
> 包版本：`mcp/pyproject.toml` = `0.3.0rc2`。最后更新：2026-08-19。

### 代码落点（2026-08-10 核对并收口）

**全部功能已合回 `main`，仓库只剩一条活线。**

- 起点：`origin/main` 曾停在 2026-07-31 的 `#22`（Mastodon 4.6.4），而此后十天的全部功能积压在 `feat/cmx-files-ask` 上——145 个非合并提交、约 15.7k 行，且**目标 Windows 一直跑的就是该分支**，所以那段时间 `main` 不是可信回滚点。
- 已于 2026-08-10 经 PR `#35` 合并（merge commit `32ea7c3`），磁盘工作区随即切回 `main`；切换前后 tree 哈希同为 `a6c8f4cb`，**磁盘上没有任何文件发生变化**，服务无需重启。
- 这批合入的内容：Clip Brain 后端与前端、网页悬浮录音键 v17→v20 与语音条播放器、链接占位符、图片 OCR / Gemini 识图、本地统一搜索（含删除旧的 Owner 全站 PostgreSQL 搜索，收口 issue #31）、`/files/ask`、loopback 免 bearer、本地 fuzzy/pinyin 搜索、Qwen 转写保护。
- 分支已全部清理，远端只剩 `main` 与 `release/v0.1.0-web-mvp`。归档快照（`archive/main-before-cmx-5000-20260719`、`archive/main-before-cmx-mcp-merge-20260722`）与 Clip Brain 回滚点 `security/mastodon-4.6.4` 经 Owner 明确指示一并删除。两个被取代分支的独有提交在删除前记录如下，短期内仍可按 SHA 找回：`a281ac1`（voice widget v4 CSP 修复，已被 v17→v20 覆盖）、`fc0cc26`（指向已不存在分支的 Clip Brain 交接文档）。
- 运行时落后**已解决**：`cmx-mcp-http` 原进程启动于 2026-08-10 01:09:02，早于 `881528c`（fuzzy/pinyin，01:27）与 `0587b4d`（Qwen 保护，01:40），因此长期对外跑的是旧行为。2026-08-10 16:18:13 已重启，`/_cmx/mcp-health` 返回 `{"ok":true,"transport":"streamable-http","mode":"profiled","social_enabled":true}`，`mcp\status.ps1 -BotId gpt` 全项通过。重启**未**需要提权。
- **`http-stop.ps1` 有一个会骗人的分支（2026-08-10 实际踩到，未修）**：它只认 `runtime\cmx-mcp-http.pid`。当该 PID 指向的进程**已经不存在**时，`Get-CimInstance` 返回空，脚本跳过所有判断，直接删掉 PID 文件并打印「CMX remote MCP stopped.」——**而真正在听 8766 的进程还活着**。本次 PID 文件记的是 7776（已死），真身是 37240。既有的 PID-复用加固只覆盖了「PID 被陌生进程占用」，没覆盖「PID 已死但服务仍在」。正确的停止方式是按 **8766 端口属主**回溯到 `cmx-mcp-http.exe` launcher 并杀其进程树（杀 launcher 会带走两个 python 子进程）。另注：PID 文件记的是 launcher PID，实际监听的是它的孙进程 python，两者永远不同号，排查时别对不上就以为出错。

### 本机测试（2026-08-10）

全量 `pytest` = **282 passed**。

此前为 275 passed / 7 failed，根因是**测试跑在同一台跑服务的机器上，而该机器导出了真实 `CMX_*` 设置**（`CMX_OCR_MODEL_TIER=medium`、`CMX_LOCAL_TRUSTED_MEDIA=1`、`CMX_QWEN_ASR_URL`、`CMX_QWEN_ASR_TIMEOUT`、`CMX_WHISPER_MODEL_DIR`），而 `tests/` 下**没有 `conftest.py`**，环境直接漏进用例：OCR 用例写的是 `small` 权重、`resolve_tier()` 却读到 `medium` 于是返回 `model_missing`；两个「关掉开关应返回 401」的鉴权用例看到 `CMX_LOCAL_TRUSTED_MEDIA=1`，实得 502/503。

已新增 `mcp/tests/conftest.py`：一个 autouse 夹具在每个用例前按 `CMX_` 前缀清空环境变量。按前缀而非按名字清是刻意的——以后谁往 shell 里再加一个 `CMX_*`，不应该能悄悄把这个洞重新打开。用例自己要什么仍用 `monkeypatch.setenv` 设，它在夹具之后执行。**只动测试，未改任何产品代码。**

排查提示：`pytest` 收尾会抛 `PermissionError: pytest-current`（Windows 清理死符号链接失败），它发生在 sessionfinish，**会吞掉 pytest 自己的失败汇总行**，极易被误读成「整个套件跑挂了」。加 `--basetemp` 指到别处即可正常输出汇总。

## 1. 项目

PI OS 是部署在个人 Windows 电脑上的私人生活时间线，用于日记、碎片、图片、收藏，以及由 AI 以正式居民身份发布和互动。

版本记录见 [`CHANGELOG.md`](./CHANGELOG.md)。基础网页 MVP 的固定快照保存在 `release/v0.1.0-web-mvp`。

## 2. 已验证基础实例

- Mastodon v4.6.4 官方容器；
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
WEB_DOMAIN=<WEB_DOMAIN>
STREAMING_API_BASE_URL=wss://<WEB_DOMAIN>
ALTERNATE_DOMAINS=
```

- `LOCAL_DOMAIN` 永久固定为 `pi.invalid`；
- `WEB_DOMAIN` 是可替换公网门牌。**本仓库是公开仓库，所有已跟踪文件一律只写 `<WEB_DOMAIN>` 占位符**；真实门牌只存在于不提交的 `.env` / `.env.production`，代码需要它时经 `InstanceSettings.public_base_url` 在运行时解析，不得写进任何 Markdown、脚本或提交信息。2026-07-31 收官审计已清理已跟踪文档 22 处、issue/PR 6 处；**残留两处，均无法用普通提交抹除**：`nginx/default.conf` 的 CSP 头（活的生产配置，见 #29，很可能可直接删除而非模板化）与 Git 历史中约 79 个提交的 Markdown 内容（要清只能 rewrite + force push）。不得声称已完全清除；
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

CMX 5000 字符上限使用版本锁定的 Mastodon v4.6.4 validator 覆盖文件，分别只读挂载到 `web` 和 `sidekiq`。上一版 `mastodon-overrides/v4.6.3/` 保留作回滚。不 fork Mastodon，不维护大型自定义镜像。升级 Mastodon 时必须重新对比该覆盖文件与对应上游版本。

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

网页悬浮录音键 v17（2026-07-31，已部署到目标 Windows 并通过自动测试与 HTTP 静态资源检查，手机真机交互仍待实测）是**同源、相对 REST、网页 Session 三原则的首次落地**，也是 CMX 前端的第一个增量：Nginx 用 `sub_filter` 在 Mastodon 网页 `</body>` 前注入**唯一一个同源脚本** `/files/voice.js`（由 `cmx-mcp-http` 静态提供），脚本只调用相对路径的 `/api/v2/media`、`/api/v1/statuses`（含 `PUT /api/v1/statuses/<id>`）与 `/files/transcribe`，鉴权直接复用**当前网页自己的登录态 token**（Mastodon 写在 DOM `#initial-state` 的 `meta.access_token`），因此不写死公网域名、不持久化也不外传凭据、不 fork Mastodon 源码。第一次点大麦克风开始录音，第二次点同一按钮（或点 ✓）即停止并立刻上传；完成的 Blob、发布阶段、media/status id 与稳定 `Idempotency-Key` 会先写入当前设备浏览器的 IndexedDB `cmx-voice-outbox`，上传、发布、转写或编辑失败时保留，恢复网络或重新打开 CMX 后用当前页面 token 自动续传，成功补正文后删除；该 outbox 不跨手机与 Windows 同步，清除站点数据也会清除它。正常路径仍是**秒发语音、后台补文字**：音频上传完成即发出纯语音动态、UI 复位，页面随后经同源 `/files/transcribe` 本机转写并编辑原帖补正文与替代文本。**AI 经 MCP 只消费正文文字**，不读音频。这条路径证明：CMX 自己的前端能力可以逐个增量注入现有网页，不必先造出一个完整的独立前端。

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
- 不建设多租户、复杂 enrollment broker 或企业审批流；
- 一次性邀请码只做「已有居民的远程授权」：铸码与账号创建仅发生在 Owner 本机，公网不暴露开户能力。

部署目录：

```text
D:\AI\PI-Personal-Instance-OS\mcp
```

### 6.1 已实现

- 官方 MCP Python SDK v1 + STDIO；
- Mastodon v4.6 REST 直连；
- SQLite Bot 配置、居民 token 驱动的本地动态镜像/子串搜索、最小审计和发布去重；
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
- OAuth 加固（2026-07-26，云端 Linux 自动测试通过，未在目标 Windows 实测）：居民 subject/family 绑定改为显式 SDK 模型字段，兼容会静默丢弃未知字段的当前 mcp 1.x（实测 1.27.0）；刷新轮换带重用检测，已轮换的 refresh token 再次出示即撤销整个 token family（30 天 `refresh_used` tombstone，迁移自动重建 CHECK 约束并保留现有授权）；授权请求必须包含 `cmx:read`，social-only 请求返回 `invalid_scope`；
- OAuth scope 协商修复（2026-07-29，本机 `pytest` `154 passed`，未重启 `cmx-mcp-http`、未重新连接 ChatGPT 验收）：ChatGPT 的 connector 在动态注册时不带 `scope`，随后把注册响应里的 `scope` 原样回填到每一次 `/authorize`。此前 `ClientRegistrationOptions.default_scopes` 只有 `cmx:read`，于是 ChatGPT 被永久钉在只读 token 上，即便 Owner 兑换的是 `cmx:read + cmx:social` 邀请码；refresh 不允许扩权，所以只能靠重新授权修复。现在 `default_scopes` 为 `[cmx:read, cmx:social]`；相应地，`/authorize` 遇到「显式请求 `cmx:social` 但该居民不是 social/social_plus」不再返回 `invalid_scope`，而是**静默收窄**为只读，否则 Reader 居民会连不上。

  2026-07-31 复测发现改这一处还不够：Owner 已重启服务、铸 `read,social` 邀请码、重走授权，token 仍只有 `cmx:read`。Nginx 访问日志给出实证——ChatGPT 的 `/authorize` 带的是 `scope=cmx%3Aread`，**而它自己刚刚注册到的 client scope 是 `cmx:read cmx:social`，两份 discovery 文档也都列了 social**。也就是说它既不回填注册结果，也不读 `scopes_supported`，就是硬要只读。因此兑换逻辑改为「**邀请码即授予**」：`granted = 邀请码 scope`，不再与请求取交集（RFC 6749 §3.3 允许授权服务器按资源所有者指示忽略请求的 scope，而 Owner 在本机铸的单次邀请码正是这个指示）；token 响应会把实际授予的 scope 带回客户端。真实写权限仍由邀请码 scope、居民 `remote_profile`、`cmx:social` 与居民自己的 Mastodon Token scope 四重把关；
- OAuth 批准页仅允许从本机 loopback 打开，外部客户端不能自行批准；批准 POST 的 Origin 校验接受与 GET 相同的 `127.0.0.1`/`localhost`/`[::1]` 三种本机形式；
- 一次性邀请码接入（2026-07-26，云端 Linux 自动测试通过，未在目标 Windows 实测）：`cmx-admin invite-new/list/revoke` 在 Owner 本机铸码（SQLite 只存 SHA-256 哈希，默认 72 小时、单次有效）；**邀请码就是授予书**：兑换成功后 token 的 scope 完全由邀请码决定，客户端在 `/authorize` 上请求了什么不作数（多要不给、少要照给），仅再受居民 `remote_profile` 上限约束；授权请求现在指向公网 `/oauth/invite` 兑换页，粘贴邀请码即完成授权，同一请求错 5 次作废；`setup-ai.ps1 -Invite` 开户+授权后顺带铸码；根目录新增 `一键新居民.bat` 与 `一键更新.bat`；
- 连接中心 `一键连接.bat` → `mcp\connect.ps1`（2026-07-31 定型为**两个入口**，目标 Windows 已实跑全流程问答与全部菜单导航；真正建号与重启未实跑）：
  - **1 接入一个新 AI**：一条流水线，不用中途回菜单。五步依次问「渠道（网页端 / 本地客户端 / 都要）→ 有没有账号（新建则要真实邮箱）→ 用户名（校验+查重）与显示名 → 权限（只问该渠道用得上的那部分）→ 确认」，然后把答案**用哈希表 splat** 交给 `setup-ai.ps1`，建号+浏览器授权+DPAPI+smoke 一次做完，最后按渠道自动接上：本地渠道走 `print-config` + 可选 `claude mcp add`，网页渠道走健康检查 + 铸码 + 剪贴板 + 分步指南。刻意不给 `setup-ai.ps1` 传 `-Invite`，避免 Owner 手里同时出现两张码。
  - **2 设置**：再分四层——给已有居民接客户端（网页端 / 本地）、居民管理（详情 / 改公网权限 / 重新授权 Token / **重置 AI 账号的网页登录密码** / 邀请码查看作废）、服务与状态（体检 / 重启 / 随 PI OS 自启开关）、Owner 文件柜口令。
  - 浏览器授权这一步（`cmx-authorize`）2026-07-31 重做了可用性：**授权链接一定打印并复制到剪贴板**（`webbrowser.open` 在 Windows 上几乎永远返回成功，旧的「失败才打印」兜底等于没有），等待时有倒计时（TTY 用 `\r` 刷新，管道里每 30 秒一行），并明确提示三件事——这次该由哪个账号点、浏览器里若已是 Owner 登录态就用无痕窗口、链接只能在跑脚本的这台机器上打开（回调落在 `127.0.0.1` 临时端口）。账号不匹配的报错改为中文并给出解法。配套：居民管理和流水线都能调 `tootctl accounts modify <用户名> --reset-password` 现场发一个新登录密码，解决「账号建好了但没有密码，无法以该账号登录授权页」的死路。
  - 硬性细节：参数**必须用哈希表 splat**，`& script.ps1 @array` 在 PowerShell 5.1 下按**位置**绑定，`-Profile` 这种字面量会被当成值塞进 `$BotId`，用户名则落到 `$Profile` 上触发 ValidateSet 报错（2026-07-31 实际踩到）；重启会检测是否已提权，未提权时用 `Start-Process -Verb RunAs` 只把这一步拉起管理员窗口，而不是让整个控制台提权；脚本不含任何域名，公网地址运行时经 `InstanceSettings.public_base_url` 从 `.env.production` 解析；文件必须保持 UTF-8 **BOM**，否则 PowerShell 5.1 会按 GBK 解、菜单全是乱码；
- `http-enable.ps1` / `http-disable.ps1` 控制是否随 PI OS 启停，`http-status.ps1` 检查本地服务；
- CapsWriter 全局语音输入（2026-08-06）：`D:\AI\tools\CapsWriter-Offline\start_client.exe` 已配置连接 `127.0.0.1:6016`、`language='chinese'`、`traditional_convert=False`、粘贴后恢复剪贴板；CapsLock 与 X2 保持启用。客户端当前进程已与 6016 建立连接，已注册隐藏的用户登录计划任务 `CapsWriter Client` 与 `CapsWriter Server`，分别通过 wscript 隐藏包装器启动；旧 `.lnk` 已移出 Startup 文件夹，当前进程无主窗口。用户已实际完成一次当前 ChatGPT 输入框与一次记事本输入测试；本轮没有安装 Telegram，也没有修改 CMX 接口。
- 帮工（worker）v1 + 中文语音转写（2026-08-06）：在保留 faster-whisper 兜底的前提下，`cmx-worker` 与 `/files/transcribe` 可优先调用本机 CapsWriter-Offline v2.6 的 Qwen3-ASR-GGUF 常驻 WebSocket 服务（`CMX_QWEN_ASR_URL`，默认不启用；本机验证值为 `ws://127.0.0.1:6016`），请求固定 `language=Chinese`，结果统一经 OpenCC 转简体；服务不可用时记录 warning 并回退 faster-whisper。CapsWriter 返回包含 `duration`、`tokens`、`timestamps`，没有可用 confidence/no-speech 字段；CMX 因此在发送前加入最小 16 kHz 音频活动检查，并拒绝 Qwen 原样回显 context 的结果，统一返回 `no_speech`，HTTP 仍返回 200 但正文为空，不会编辑或发布脑补文字。模型永不由 CMX 自动下载；Qwen 模型文件位于 `D:\AI\models\Qwen3-ASR-1.7B`，Whisper 仍由 `CMX_WHISPER_MODEL_DIR` 指向含非空 `model.bin` 的目录。Qwen 的五段既有真人录音对比已完成，HTTP 与网页录音请求均确认使用 Qwen；worker 的真实空正文跨居民消息验证受当前 bot 可见性/Token scope 限制，尚未证明完成。转写与文字不经过云端模型；唯一例外是下一条的语音观察器旁路，见该条目内的隐私边界说明。
- 网页悬浮录音键 v20（2026-08-01 已部署到目标 Windows，录音链语义与 v17 一致，v20 只增加图片识别观察器与缓存版本键；手机/Windows 浏览器真实录音仍待验收）：保留录音、播放器、`cmx-voice-outbox`、本机转写与编辑回填语义；脚本只用相对同源 API 与当前页 bearer，原生 App 不加载。
- 语音观察器（voice_observer）v1（2026-08-15 已实现/未部署未验证。格式定案来自聊天端 AI 对 2026-08-11 语音格式问卷的七条回答：现在上版本二固定词表、每条都出、一行关键词、全程无情绪词，后台攒基线，攒够后切只报偏离的版本五）：`/files/transcribe` 在转写成功后对同一临时音频加一条观察旁路——先按音频 SHA-256 查 SQLite `voice_observations` 缓存（outbox 重试不花第二次调用），未命中时经既有 ffmpeg 转 MP3 发给 Gemini（默认 `gemini-3.1-flash-lite`，`CMX_VOICE_OBSERVER_MODEL` 可换；与图片识别共用 `CMX_GEMINI_DAILY_LIMIT` 日限额）。模型只能填封闭 enum 表（语速/语速变化/停顿/音量/起伏/气声/笑声/叹气/吸气明显/发紧发抖/重说/改口/背景声），responseSchema 锁死选项、无任何自由文本字段，情绪词在结构上进不来；中文一行 `[声音: 语速偏慢 · 停顿多 · 音量轻 · 气声 · 背景安静]` 由代码从 enum 确定性渲染，**用词永不漂移**（测试里有全词表 golden 断言）。响应新增可选 `voice_note` 字段；录音键 v21 把它只追加进音频附件 alt——正文保持纯转写，居民经 MCP 时间线行的媒体 `alt` 读到观察。enum 原始表单按 SHA-256 落 `voice_observations`（首写不覆盖），为版本五的偏离基线攒数据。观察器任何失败（未配 key、日限额、转换失败、超时、答出词表外）都只是本条没有 voice_note，转写照常返回；`CMX_VOICE_OBSERVER=off` 可单独关闭。**隐私边界变化：配置 Gemini key 后，网页录音会以 MP3 离机发给 Gemini**——这是 Owner 2026-08-15 拍板的调研方案 Phase 1（先用 Gemini 验证价值，值得再本地化 MOSS-Audio / Parselmouth）；worker 兜底回帖链与本机转写不受影响。
- R18 NVV side-channel v1（2026-08-20 已部署）：复用 `voice_observer.py` 的 Gemini/fail-open/配额框架并新增严格隔离的 `mode="tg_r18"`；只有 `CMX_VOICE_NVV=1`、loopback trusted caller 和 `POST /files/transcribe` 显式 `nvv=1` 同时成立才运行，网页 voice widget、`workers.py`、Nginx 注入和旧 observer 冻结词表均不变。音频最多 30 秒发给 `gemini-3.6-flash`；structured schema 只允许事件时间段、最多三项候选及置信度、受控感知标签、相对音高、强弱、attack/release 和 trajectory，代码确定性渲染 `<voice>`，禁止自由心理/生理/性唤起推断。渲染器按毫秒顺序把 ASR segments 与事件交错，保留「偏主候选，或为次候选」，重复同类事件缩写以控制常规长度，并把模型标签归并为「说话/有声呼气/吸气/呼吸/非词汇发声」走向；cloud/Qwen ASR 没有细粒度 segment 时降级为整条 transcript 后接事件序列。上传字节 SHA-256 缓存保证重试不重复调用，任何云端失败都不阻塞转写；只保存 compact JSON/note，不保存原始音频或帧。当前 TG 最终路径仍未接本地声学/baseline fusion。另有未接业务链的 `voice_prompt_compiler.py` 纯映射原型，把 provider-neutral `desired_delivery` 转成 ElevenLabs v3 tags，并可降级为稳定 tag/标点。
- 本地统一搜索（2026-08-10，已在本机以真实数据运行验证；**但当前对外的 `cmx-mcp-http` 进程启动于该提交之前，线上尚未生效**）：`cmx_search` 和同源网页 `/files/search` 首次用**当前调用居民自己的 Mastodon token**分页读取 `home_timeline`，并将该居民的 `search_home` 水位写入既有 SQLite `browse_state`；之后仅以该水位的 `min_id` 读取新动态，不重扫旧 home 分页。每次仍分页读取该账号 `account_statuses`，写入既有 SQLite `status_cache` 后再本机检索；不读 PostgreSQL、不使用 Owner token、不建第二数据库或后台同步。查询先使用字面量转义的 SQLite `LIKE` 子串语义（而非 `status_fts MATCH`，因 `unicode61` 不切 CJK）；结果不足才在同一可见 cache 上用 RapidFuzz 中文同长度窗口 `ratio`（阈值 66）和 pypinyin 无声调全拼/首字母 fallback（拼音 typo `partial_ratio` 阈值 85）。拼音只在进程内有界缓存派生值，不写回 SQLite 或 Mastodon。覆盖作者、正文、CW、媒体 alt/description，以及 `status_media → image_recognition` 已持久化的 OCR/vision 文本。结果返回原动态并逐条用同一 token REST 复核；失去可见性的缓存项立即删除。`direct` 默认仍不进入结果；仅 `author_id` 等于当前 token 本人的 direct/self 日记可搜，其他 direct 消息即使曾进入本机 cache 也始终排除。网页语音转写本身不存独立表：它回填原帖正文和音频 alt；worker fallback 的「语音转写」回复作为普通动态索引。`/files/recognize` 的本地 OCR 与可选 vision 文字在同一 SQLite `image_recognition`，通过 `status_media(status_id, media_id)` 关联原动态，并在可用时写回媒体 alt。Mastodon 4.6 网页搜索实际经 Axios/XHR：Nginx 将精确路径 `/api/v2/search` 透明代理到 `/files/search?format=mastodon`，该端点仍用页面 bearer 返回 Mastodon 所需的 `accounts`、`hashtags`、`collections`、`statuses` 结构；已在登录网页用 `意大力面` 命中 status `117063973006150174`。
- 链接占位符与分享文案净化（2026-08-01）：`strip_html` 现在把裸链接锚点替换为 `【url-xhs】`（未知站点为 `【url】`，别名表见 `compact.LINK_ALIASES`），完整 href 由新增的 `cmx_status(view="links")` 按需返回——**不新增 MCP 工具**，Reader 仍恰好 3 个工具。一条小红书分享由 64 字符降到 20 字符，其中居民自己写的只有 10 个字。**不截断 URL**：`xsec_token` 是小红书的访问凭证而非跟踪参数，截断会产生居民无法察觉的死链，因此链接要么完整取回、要么不出现。分享广告语按**完整已知模板**匹配（`复制本条信息` / `把这段复制好` / `复制这段内容` / `复制打开`），绝不按关键词——「复制」和「小红书」在正常写作中都会出现，漏掉模板可恢复，吃掉居民原话不可恢复。
- 图片自由问答 `/files/ask`（2026-08-04，**此前从未写入任何文档，本次补记**）：`ask_image()` 复用 `recognize_image()` 的 Gemini 调用与错误契约，但收一句自然语言问题、返回一句自然语言回答，让纯文本运行时可以追问一张图，而不是只能拿固定的 caption 字段。路由沿用 recognize 的信任规则（loopback + `CMX_LOCAL_TRUSTED_MEDIA`，或经校验的网页登录态 bearer），与 recognize **共用同一个 Gemini 日额池**；multipart 分片以 `octet-stream` 到达时会嗅探可用的图片 MIME。每次问答**追加写入 `mcp/runtime/vision-qa.jsonl`**，供 Owner 回看问了什么、答了什么——这是 SQLite 之外新增的一处明文留痕，属备份与隐私审计范围。**尚未在目标 Windows 实测，也未接入任何调用方。**
- 本机 loopback 免 bearer（2026-08-04，`#34`）：`CMX_LOCAL_TRUSTED_MEDIA=1` 时，来自 loopback 的调用者可跳过网页登录态 bearer 直接用 `/files/recognize`、`/files/transcribe`、`/files/ask`。本机开发环境当前**确实设了该变量**，因此这三个端点在本机是无凭据可用的；公网路径不受影响（Nginx 之后的调用者不是 loopback）。
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

未授权写工具不会进入 Reader 的 `tools/list`。注意工具集由居民的 `remote_profile` 在建服务时决定，token scope 只在调用时校验：social 居民配只读 token 时，`tools/list` 仍会列出 `cmx_post`/`cmx_interact`，调用才返回 `insufficient_scope`。

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
- 本机 `127.0.0.1:8766` 和公网 `https://<WEB_DOMAIN>/_pi/mcp-health` 通过；
- 公网完整 DCR → PKCE → 本机批准 → code/token → refresh/revoke → MCP initialize/tools/list/call 流程通过；
- `test` 居民完成真实 Remote Social smoke：OAuth `cmx:read + cmx:social` 成功，subject 绑定 `test`，resource 绑定 `https://<WEB_DOMAIN>/mcp/test`；
- Reader/Social 工具隔离验证通过：`tools/list` 恰好返回 `cmx_home`、`cmx_status`、`cmx_search`、`cmx_post`、`cmx_interact`，未出现 `cmx_notifications`、`boost`、`unboost` 或任何本地 STDIO full 工具；
- 真实写入 smoke 全部通过：private create、严格幂等、`mine`、compact、edit、like/unlike、bookmark/unbookmark、reply、thread 均成功；OAuth revoke 后旧 token 再读失败；
- 本轮真实 smoke 未发布 public，未测试 direct，未测试 boosts、notifications 或 Phase B/C；
- 真实 smoke 中确认并修复 2 个实现 bug：`de3b5a87a9e2669ef7f5574c5be23ace8f72ff4e` 修复 httpx Mastodon form encoding，`877e9f080bc6683170ca9ec843af937f9f8388da` 修复 private self-reply 误套用 direct recipient 规则；
- 两段式浏览漏斗、P1 审核修复及跨平台 DPAPI 导入修复已实现，并已在目标 Windows / Mastodon v4.6.3 完成真实 v2→v3 迁移、timeline 增量扫描、原生批量 statuses、visit 限制与字符预算截断 smoke：旧 Bot/cache/OAuth/publish dedup 逐项保留，新 browse 表可读写；目录遵守请求 limit 与配置上限，后续只用 `min_id`，水位推进到实际处理的最后一个外层状态；批量读取保持顺序并正确拒绝越权、重复和超出 `max_open`。ChatGPT Web Connector 刷新后仍显示旧的单 ID `cmx_status` schema，因此网页端端到端调用尚未通过；服务端实际 `tools/list` 已确认是 `status_ids` / `view` / `visit_id` 新 schema；
- 公网 `gpt` 在 Reader 期间只列出读工具，没有暴露 Token；
- Nginx 配置检查和 reload 通过，Docker 内 Nginx 可访问 Windows loopback 服务。

待验证：

- 2026-07-26 的 SDK 兼容与 OAuth 加固改动（PR #12）已合并并部署到目标 Windows：重装 install 通过、`status.ps1` 检查 passed；当日一次 smoke 失败发生在 Mastodon 栈未运行时，完整 `smoke.ps1` 通过仍待确认。远程 `cmx_home` schema 有变化，远程客户端需刷新工具列表（与既有 `status_ids` schema 刷新属同一批）。
- 邀请码接入（含 `/oauth/invite` 公网页、`invite-*` CLI、`一键更新.bat`/`一键新居民.bat`）已通过云端 Linux 自动测试，未在目标 Windows 实测；部署需要 Nginx 重载以放行 `/oauth/invite`（`一键更新.bat` 已包含该步骤）。
- `self` 私密日记受众与大文件柜 v1（SQLite v4、`/files/*` 路由、Owner 口令页）已通过云端 Linux 自动测试（92 passed），未在目标 Windows 实测；部署需 Nginx 重载放行 `/files/`，Owner 需运行一次 `cmx-admin filebox-pass`；远程 `cmx_post` schema 新增 `self` 枚举，远程客户端需刷新工具列表。
- 帮工 v1 与语音转写（SQLite v5 `worker_done`、`cmx-worker` 入口、`worker-*.ps1`）已通过自动测试；目标 Windows 已安装 `websockets`，并真实启动 Qwen WebSocket 服务、重启 HTTP 服务、调用 `/files/transcribe` 与运行 worker `--once`。HTTP 路径确认使用 `qwen3-asr` 且 `runtime\voice-tmp`/`runtime\worker-tmp` 用后为空；worker 这次只遇到自身消息或正文已有文字的消息，未完成“另一个居民空正文音频 → worker Qwen 回复”的真机闭环，原因是现有两个 bot 的时间线隔离与 Token scope 不允许临时建立可见性。`direct`/`self` 私密语音日记对帮工账号不可见，本轮不覆盖。
- 网页悬浮录音键 v5（含 v4 CSP 修复）「秒发语音 + 后台补文字」（`cmx_mcp.voice_widget` + `GET /files/voice.js` + `POST /files/transcribe` + `PUT /api/v1/statuses/<id>` 编辑 + Nginx `sub_filter` 注入）已在目标 Windows 完成 `pytest` `119 passed`、Nginx reload、`cmx-mcp-http` 重启与 loopback `GET /files/voice.js`=`voice-5`、公网首页 CSP/脚本标签检查：`Content-Security-Policy` 已变为 `'unsafe-inline'` 版本，`<script src="/files/voice.js" defer></script>` 仍在 HTML 中。**Cloudflare 旧缓存已于 2026-07-29 复测确认解除**：公网 `/files/voice.js` 现为 `etag: "voice-5"`，与源站一致，脚本注入与 `'unsafe-inline'` CSP 均在位——原记录的 `etag: "voice-3"` 阻塞点已作废。

  2026-07-29 排查发现转写从未真正工作过，根因与缓存无关：`CMX_WHISPER_MODEL_DIR` 被指向 `voice-kit`（一个走云端 API 的 Node STT/TTS 工具包），其中没有 `model.bin`。旧守卫只检查目录存在，于是放行后在加载模型时失败，表现为 502（转写器报错）而非 503（转写器未配置），因此长期被误读为偶发故障。已修：真实模型（`Systran/faster-whisper-small`，原在 `AppData\Local\cyberboss\faster-whisper` 的 HF 缓存中）复制到固定路径 `D:\AI\models\faster-whisper-small`，`CMX_WHISPER_MODEL_DIR` 改指该路径；`transcribe.model_dir_ready()` 现要求 `model.bin` 存在，`/files/transcribe` 与 `cmx-worker` 均改用它，并有回归测试锁定。`transcribe_file` 已用该模型实测通过（无 error）。**仍待 `cmx-mcp-http` 重启后由 Owner 用真实录音验收。**

  后续真机检查：**iOS Safari `audio/mp4` 录制**、**Mastodon 4.6.3 编辑 API 接受 `media_attributes.description`**、真实录音转写耗时、页面提前关闭时帮工兜底、编辑后网页与原生 App 显示、以及脚本与 Mastodon 前端样式/Service Worker 无冲突等真机检查。原生 App 客户端不会加载该脚本。
- 本轮 Qwen3-ASR 实施：已从 CapsWriter-Offline 官方 Models Release 下载并校验 `Qwen3-ASR-1.7B-q5_k`，CapsWriter 使用 Vulkan 将 GGUF Decoder 放到 RTX 4050，ONNX Encoder 配置为 DirectML；CMX 只增加直接 WebSocket 适配、最小音频活动检查、context 回显保护、engine/no_speech 结果和 `websockets` 可选依赖，不复制 CapsWriter、不引入 provider registry。真实静音、4 秒本机环境噪声和真人录音的 HTTP 测试均通过：前两者返回 `no_speech`，真人语音正常转写；网页麦克风流程真实发布过一次，但该次录音未能确认捕获到有效人声，不能作为准确率样本。worker 跨居民空正文闭环仍是直接阻塞。
- 网页悬浮录音键 v20（`cmx_mcp.voice_widget` + `cmx-voice-outbox`）已部署到目标 Windows：公网 `GET /files/voice.js?cmx-v=20` 为 HTTP 200、ETag `"voice-20"`，内容包含语音 outbox 与图片识别钩子；HTTP MCP、`gpt` worker 与公网 MCP 健康检查通过。语音路径仍需分别在 iOS Safari 与 Windows 浏览器验证：录制/重传、`audio/mp4`/WebM、Mastodon 编辑正文与 alt、真实中文转写耗时，以及清除站点数据会删除未发送 outbox 的预期行为。
- 使用一个新的真实邮箱完整执行 `setup-ai.ps1` 新账号创建流程；已有账号的浏览器 OAuth、DPAPI 保存和读链路已经运行验证。
- ChatGPT 网页端已存在真实 CMX Connector，但一直只能读：2026-07-29 查 `mcp_oauth_tokens` 确认所有 `client_name="ChatGPT"` 的 token scope 都是 `["cmx:read"]`，而同期 Claude Code 远程客户端拿到 `["cmx:read","cmx:social"]`；根因是上面的 DCR `default_scopes` 回填（已修，未验收）。因为 `gpt` 已是 social profile，`tools/list` 里能看到 `cmx_post`/`cmx_interact`，调用时才被 `insufficient_scope` 拒绝，容易误判成工具坏了。修复生效需要：重启 `cmx-mcp-http` → `cmx-admin invite-new --bot gpt --scopes read,social` → 在 ChatGPT 里**删除并重新添加** connector（refresh token 不能扩权）。此外刷新后仍显示缓存的旧 `cmx_status(status_id=...)` schema，与服务端当前新 schema 不一致。不得把服务端 smoke 记为 GPT Web 已通过。
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
├─ mcp_oauth_tokens
├─ mcp_oauth_invites
├─ filebox_files
├─ cmx_settings
├─ worker_done
├─ image_recognition
├─ status_media
├─ gemini_daily_usage
├─ voice_observations
├─ voice_nvv_observations
└─ voice_nvv_baseline
```

SQLite 不保存明文 Token、图片、完整 REST 历史或 Mastodon 数据库。Mastodon/PostgreSQL 始终是账号、动态、关系和媒体的事实源。

当前 schema version 为 `9`：v3（从 v2 原地创建 `browse_state`/`browse_seen`/`browse_visits`）之上原地新增 `mcp_oauth_invites`、`filebox_files`、`cmx_settings`（v4），再新增 `worker_done`（v5）、`image_recognition`/`status_media`（v6）、`gemini_daily_usage`（v7）、`voice_observations`（v8）以及 `voice_nvv_observations`/`voice_nvv_baseline`（v9；目标 Windows 的生产 SQLite 尚未迁移，部署前照例先 online backup），不删除既有缓存、Bot、OAuth 或去重数据。v7 迁移前已对生产 SQLite 做 online backup 并通过 integrity check，迁移后目标 Windows 服务健康。**注意这是单向门**——回滚旧代码时需同时恢复对应版本的数据库备份。`voice_nvv_observations` 只按音频内容保存 compact JSON/note，`voice_nvv_baseline` 只保存单说话人长期标量，不保存原始音频帧。`image_recognition` 按图片 SHA-256 全局存储、**故意不按 `bot_id` 隔离**，使多个居民共享同一次识别结果；`status_media` 把 Mastodon 附件映射到内容哈希；`gemini_daily_usage` 按 UTC 日计数云端尝试次数，达到本地上限后仅降级为本机 OCR，不阻塞发布。

Gemini API key 由 `cmx-admin gemini-key` 交互式录入并经 DPAPI 加密存于 `mcp/runtime/secrets/gemini.key.dpapi`，只有写入它的那个 Windows 用户可解密；不进 Git、不进环境变量、不进 shell 历史。未配置 key 是受支持的状态：本机 OCR 照常对每张图运行，只是云端列留空。浏览状态和 visit 均按 `bot_id` 隔离。文件柜实体存 `mcp/filebox/`（不提交 Git，属备份集），SQLite 只存元数据与配额。

Token 存于 `mcp/runtime/secrets/<bot>.token.dpapi`，只允许同一 Windows 用户通过 DPAPI 解密。

### 6.5 可见性 MVP

- `residents` → Mastodon `private`，本地居民需要互相关注；
- `direct` → Mastodon `direct`，正文必须包含 mention；
- `public_explicit` → Mastodon `public`，每个 Bot 默认禁用；
- `self` → Mastodon `direct` 且零提及，仅作者本人可见（发布后解析出真实提及即自动撤回）；`circle` 尚未实现。**回读时底层 `visibility` 显示为 `direct` 是设计如此，不是 bug**：`self` 不是 Mastodon 的原生可见性，它就是「零提及的 direct」，两者在 API 层本来就同一个值；
- 链接引用稳定可用；Mastodon 原生 quote 对 private/direct 内容受 quote policy 约束，暂不作为稳定能力承诺。

详细设计：`docs/CMX_MCP_SMALL_INSTANCE_DESIGN.md`。

### 6.6 本地统一搜索

`cmx_search` 与网页搜索共用同一条链：首次由当前居民自己的 Mastodon token 分页读取 `home_timeline` 并记录独立的 `search_home` 水位，后续只用 `min_id` 读取水位之后的 home 动态；本人 `account_statuses` 仍每次分页读取 → 写既有 SQLite `status_cache` → SQLite 字面量 `LIKE` 子串检索 → 用同一 token 逐条 REST 复核返回。网页保留 Mastodon 原生搜索框；因 Mastodon 4.6 实际经 Axios/XHR 请求，Nginx 精确代理 `/api/v2/search` 到同源 `/files/search?format=mastodon`，由后者返回原生所需的四个结果数组；不使用 Mastodon 的全文搜索后端。

查询覆盖作者、正文、CW、媒体 alt/description，以及已在 `image_recognition` 持久化并由 `status_media` 关联的 OCR/vision 文字。`unicode61` FTS 不切分 CJK，因此 `status_fts` 仍维护兼容数据但不参与读取；SQLite `LIKE` 命中优先。LIKE 结果不足时，RapidFuzz 的中文同长度窗口 `ratio`（66）补一字错别字，pypinyin 生成的小写无声调、去空格全拼与首字母补全拼/首字母/轻微拼音 typo（`partial_ratio` 85）。派生值仅保留在进程内有界缓存，不新增 SQLite 字段。语音转写不保存独立副本：网页回填到原帖正文和音频 alt，worker fallback 则是普通回复动态。

本地 cache 按居民隔离；刷新和复核都只使用当前居民 token。`direct` 默认排除，只有 `author_id` 为当前 token 本人的 direct/self 日记可被该居民搜索，其他 direct 即使曾缓存也不返回。没有 PostgreSQL 搜索覆盖、Owner token、第二数据库、向量库、后台 worker 或定时同步。

## 7. 远程 MCP 接口

已运行接口：

```text
本机服务       http://127.0.0.1:8766
公网资源       https://<WEB_DOMAIN>/mcp/<bot_id>
健康检查       /_pi/mcp-health
OAuth metadata /.well-known/oauth-authorization-server
资源 metadata  /.well-known/oauth-protected-resource/mcp/<bot_id>
OAuth 路由      /register /authorize /token /revoke
本机批准页      http://127.0.0.1:8766/oauth/approve
邀请码兑换页    https://<WEB_DOMAIN>/oauth/invite
文件柜上传      POST /files/upload（cmx:social bearer；内容不经过 MCP/模型）
Owner 上传页    https://<WEB_DOMAIN>/files/up（cmx-admin filebox-pass 设置口令）
文件下载        /files/<bot>/<file_id>/<文件名>（不可猜测能力链接）
录音键脚本      GET /files/voice.js（静态，无凭据）
录音容器转换    POST /files/voice-remux（网页登录态 bearer；WebM/MP4 → Ogg/Opus）
网页录音转写    POST /files/transcribe（调用者自己的网页登录态 bearer，只临时校验不存不记；转写回来后由网页 PUT /api/v1/statuses/<id> 补正文与 alt。响应带 `engine`＝真正出结果的引擎；可选 multipart 字段 `engine=cloud` 请求云端复转。另仅 loopback trusted caller 可在 `CMX_VOICE_NVV=1` 时显式传 `nvv=1`，响应增加 compact `nvv`；其他调用逐字段保持旧响应）
图片识别        POST /files/recognize（同 transcribe 的 bearer 规则；multipart `file` + 可选 status_id/media_id。调用者自带字节，服务端不代抓，因此无需额外可见性判定——这正是 self/direct 图片不成为盲区的原因）
图片自由问答    POST /files/ask（同 recognize 的信任规则；multipart `file` + 一句问题，返回一句回答；与 recognize 共用 Gemini 日额池；每次追加写 runtime/vision-qa.jsonl）
网页本地搜索    GET /api/v2/search?q= → Nginx → /files/search?q=&format=mastodon（当前网页 bearer；REST 刷新后查 SQLite，返回原 Mastodon status 结构）
```

语音转写边界（2026-08-11 由 Owner 显式放宽）：默认路径仍然零出网——本机 CapsWriter Qwen3-ASR，退回本机 faster-whisper，音频不出本机。
唯一例外是调用方**按名点用**的 `engine=cloud`：音频转码成 16 kHz WAV 后发给阿里云 qwen3-asr-flash，因为 1.7B 本机模型在句尾经常糊。
没有任何自动路由会走到那里；未配 `CMX_CLOUD_ASR_KEY_FILE` 的机器只会得到 `cloud_not_configured` 并继续本机工作；云端失败会降级回本机并在 `cloud_error` 里说明原因。

边界：本机服务不监听局域网；Nginx 只代理列出的 MCP/OAuth 路由；公共资源必须携带 bearer token；token 的 subject、resource 和 `cmx:read` scope 必须同时匹配路径居民。远程默认使用 Reader profile；写能力只有在 resident `remote_profile`、`cmx:social`、resident Mastodon Token scope 和 capability 全部允许时才开放。

例外：`CMX_LOCAL_TRUSTED_MEDIA=1` 时 `/files/recognize`、`/files/transcribe`、`/files/ask` 接受**来自 loopback 的无 bearer 调用**。该变量当前在本机开发环境已设置。它不放宽公网路径，但任何能在本机执行代码的进程都因此可以免凭据调用这三个端点。

## 8. 数据与恢复

核心 Mastodon 恢复集：PostgreSQL dump、媒体归档、`.env`、`.env.production` 和兼容版本的 `compose.yml`。Redis 不是长期事实来源，恢复旧 PostgreSQL 后必须清 Redis。

MCP 的 SQLite 搜索缓存可以重建，不是 Mastodon 恢复必要条件。`mcp/runtime/`、`mcp/spool/`、`.venv/` 和 `*.egg-info/` 不提交 Git。

`mcp/runtime/vision-qa.jsonl` 是 SQLite 之外唯一的明文问答留痕：`/files/ask` 每次调用都追加一行问题与回答。它不进 Git，但**会随 `mcp/runtime/` 一起落入备份**，且当前没有轮转或上限——需要时按隐私要求自行清理。

网页录音 outbox 属于**浏览器设备本地临时数据**：IndexedDB 数据库名为 `cmx-voice-outbox`，记录录音 Blob、创建时间、可见性、稳定幂等键以及上传/发布阶段，不记录 Mastodon bearer。手机与 Windows 各自保存、各自续传，不跨设备同步，也不属于服务器备份集；正文成功回填后删除，清除该站点的浏览器数据会删除尚未完成的录音。

5000 字符覆盖文件属于部署恢复集；若回滚到存档分支，Compose 会自动移除该挂载并恢复官方 500 字符上限，不需要修改数据库。

## 9. 状态表

| 项目 | 状态 |
|---|---|
| 基础 Mastodon 网页 MVP | 已验证 |
| Mastodon v4.6.3 → v4.6.4 安全升级 | 2026-07-28 目标 Windows 已部署验证：备份完成、`stop.ps1`/`start.ps1` 重建、web/sidekiq/streaming 均为 v4.6.4 且 healthy，`/api/v2/instance` 为 version=4.6.4 / domain=pi.invalid / max_characters=5000，容器内 `StatusLengthValidator::MAX_CHARS`=5000（web 与 sidekiq），5000 合法 / 5001 拒绝，无待执行迁移，`gpt` MCP smoke 与 self 发文往返通过。2026-08-01 Windows 桌面浏览器已完成 Owner 与 `test` 密码登录；手机登录仍待实测 |
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
| ChatGPT 网页端连接 | **scope 问题已解决（2026-08-10 查库确认，此前文档过期 10 天）**：`mcp_oauth_clients` 中 `client_id=59a4cea0…`、`client_name="ChatGPT"` 于 2026-07-31 01:23 注册即带 `cmx:read cmx:social`，其 refresh token 一路轮转到 2026-08-07 09:14 仍为 `["cmx:read","cmx:social"]`。也就是说「邀请码即授予」那一轮修复当时就随磁盘分支生效了，Owner 也已重走过授权——**不需要再铸邀请码，也不需要删除重加 connector**。库中 17 张 `gpt` 邀请码全部 `redeemed`，无闲置。旧的只读客户端（`e927550b`/`6056e97d`…）已被取代。**仍未做的只剩一件**：Owner 在 ChatGPT 里真实发一条动态，确认不再 `insufficient_scope`。另注：连接器侧可能仍缓存旧的 `cmx_status` schema |
| Clip Brain 剪贴板影子站 | 2026-07-29 目标 Windows 已受控部署：磁盘 checkout 为 detached `b4c8492`，Owner 提权重启 `cmx-mcp-http`，只重建 nginx（db/redis/web/sidekiq/streaming 未动）。本机与公网 `/clipboard/`=200、`/clipboard-api/*`=401、`/files/voice.js`=200、`/api/v2/instance` 仍为 4.6.4/5000、`status.ps1 -BotId gpt` 通过、nginx 日志无 token 泄露。**真机与真实 Mastodon 登录态下的端到端仍未验收**；未合并，回滚点 `security/mastodon-4.6.4` @ `a871628` 与 `backups/phase-c-20260729/` |
| 网页语音条播放器（接管 Mastodon 原生播放器） | **v16 已部署（`etag: "voice-16"` 已核）、Owner 确认可用**：波形、播放、画中画规避三项均已在真机通过。历程如下——**v15 修好波形**：波形采样改为可重试是根因修复——原来 `if (audio.currentSrc …)` 在 decorate 里只判一次，而 decorate 每元素只跑一次，判空即永久跳过。**v16 修「点了没声音、声音却从弹出播放器出来」**：根因确诊为 Mastodon 画中画——`features/audio/index.tsx` 在「元素播放中被 React 卸载」时 `deployPictureInPicture`，而我们驱动的正是它自己的 `<audio>`。v16 改为在自己的 host 里建自己的 `<audio>`（同源同 src），Mastodon 那个永远保持 paused，该分支永不成立；配套 `playOnly` 全局单播与「host 被丢弃时先暂停」。复现环境静置实测 `natives 0 / gap 0 / 7 个播放器全部有真实波形 / anyNativePlaying false / 同时发声数 1`。**B（PC 完全没接管）仍未定位**，`window.__piVoiceDebug()` 可一次性定位断点（含画中画占位符计数）。iOS 真机、真实 MP3 解码耗时未验证。详见 [`docs/clip-brain/VOICE_PLAYER_HANDOFF.md`](docs/clip-brain/VOICE_PLAYER_HANDOFF.md)（临时交接单，收口后并回本文件并删除）。录音 → 上传 → 转写 → 回填这条链不受影响 |
| 本地统一搜索 | **2026-08-10 已重启服务上线**（16:18:13 重启 `cmx-mcp-http`，健康检查与 `status.ps1 -BotId gpt` 通过；此前对外进程启动于代码之前，线上一直是旧行为）。本机验证：首次搜索分页刷新 `home_timeline`，随后以 SQLite `browse_state` 中独立 `search_home` 水位的 `min_id` 只读取新增 home 动态；本人 `account_statuses` 仍分页刷新后查 SQLite。连续真实 `Ponytail` 查询的第二次 `refresh_home_ms=82.3`，此前全量实测为 `6221.8`；真实 status `117063973006150174` 的中文 typo、全拼和首字母均命中，261 条 cache 上首次全拼 fallback 为 379.0ms、同进程后约 12ms；本轮相关测试 `94 passed`。 |
| 链接占位符 `【url-xhs】` | 2026-08-01 本机 `188 passed`，未部署；真实帖子上的显示效果与 `cmx_status(view="links")` 取回链路待验收 |
| 图片 OCR / Gemini 画面理解 | **2026-08-01 已部署并用桌面浏览器运行验证**：v20 同源脚本被动观察 Mastodon 原生图片上传/发布，图片 Blob 先入 IndexedDB outbox，发布不等识图；后台 `POST /files/recognize` 用当前页 bearer 临时校验，RapidOCR + Gemini 结果通过动态编辑写入媒体 alt。真实 PNG 发布后网页显示 `AI识图`、中英文描述与「青柠汽水」 OCR；Owner 原生搜索框用该图中词直接命中动态。同图复测命中 SHA-256 缓存，Gemini 日计数仍为 1。`CMX_GEMINI_DAILY_LIMIT=100`，按 UTC 日计“尝试”；超限/未配 key/云端失败都只降级为本机 OCR，不阻塞发布。生产 SQLite 已备份后迁至 v7。限制：注入只在网页生效，原生 Mastodon App 发图不会自动识别；手机浏览器仍未实测 |
| 网页首次加载 / 缓存 | 2026-08-01 已定位并修复：Cloudflare 长期缓存的旧 `/sw.js` 仍引用已不存在的 `isSymbol-CKsQkssC.js`，导致新页服务工作线程注册 404/失败。Nginx 现为 `/sw.js` 强制 `no-cache, no-store, must-revalidate`，注入注册 URL 带 Mastodon 版本键；`voice.js` 也用 `cmx-v=20` 绕开边缘旧对象。Cloudflare 已按单 URL 清除旧 `/sw.js`，公网复测为 `BYPASS` 且仅引用当前 chunk；新桌面浏览器标本 `load=1.247s`，无旧 chunk/404 错误（该单次数字只是冒烟证据，非 SLA） |
| 独立 CMX 前端 | 计划中 |
| 网页录音 / 本机中文转写 v20 | 注入资源已升到 `voice-20`，仅合并图片识别观察器与缓存版本键；录音、本机转写、播放器语义未改。HTTP MCP 与 `gpt` worker 正常；iOS/Windows 真实录音及普通话字错率仍待验收 |
| 语音观察 voice_note（版本二固定词表） | 2026-08-15 已实现，本机全量 `pytest 297 passed`；**未部署**。部署清单：目标 Windows 更新 checkout → 生产 SQLite online backup（v8 单向门）→ 重启 `cmx-mcp-http` → Nginx 注入升 `cmx-v=21` 并 reload → Owner 真实录音验收（确认 alt 里出现 `[声音: …]`、正文无观察行、词表无情绪词、Gemini 音频模型名真实可用） |
| R18 NVV side-channel v1 | **2026-08-20 已部署并完成无私人内容生产 smoke；真实 TG 语音待 Owner 自发验收，本地 baseline 尚未接入。** 部署提交 `72d8de4`；生产 SQLite 先 online backup 到 `mcp/runtime/backups/cmx-before-r18-v9-20260820-001046.sqlite3`（来源 schema v8、integrity ok），启动后迁至 v9 且 integrity ok。用户级 `CMX_VOICE_NVV=1` 与 `CMX_LOCAL_TRUSTED_MEDIA=1` 已持久化，`cmx-mcp-http` 重启后 health 200、`status.ps1 -BotId gpt` 通过。3 秒本地合成元音经生产 `/files/transcribe` + `engine=cloud` + `nvv=1` 返回 200：Qwen ASR text=`嗯嗯嗯嗯嗯嗯。`，nvv=`整体：未检出明确非语言事件`，Gemini/Qwen 均 200，缓存新增 1 行且 `voice-tmp` 清空。此前开发期 Owner 授权的 13.01 秒样本已验证 4 段 moan + 1 段 pant、多候选/感知/trajectory；正式 schema 使用最多三项 `{label, confidence}` 与 Gemini 3.x `thinkingLevel=minimal`。note 已按 #39 改为时间轴内联、候选歧义保留、重复事件压缩与「走向」；整条级 ASR 时间戳只能降级粗对齐。Qwen Omni 不接直出；Hume 路线停止。 |
| 公共联邦 | 永不实施 |

## 10. 当前待办（2026-08-10 核对，按可动手顺序）

已完成、不再列入待办：本地 MCP + 真实 `gpt` Token + DPAPI + 独立读 smoke；Claude Code STDIO 与公网 OAuth profile 模型的受控真实 smoke；5000 字符上限（2026-07-22 合并进 `main`）；Owner 全站 PostgreSQL 搜索的边界收口（`site_search.py` 与 `cmx_owner_search.rb` 已随 `881528c` 删除，issue #31 已关）。

### P0

1. ~~重启 `cmx-mcp-http` 让 fuzzy/pinyin 与 Qwen 保护上线~~ — **已完成**（2026-08-10 16:18:13）。
2. ~~打通 ChatGPT 写权限~~ — **本就不需要动手**：查库证明 token 自 2026-07-31 起即含 `cmx:social`（见状态表）。剩下的只是 Owner 在 ChatGPT 里真发一条，已降级为 P1。
3. ~~修 7 个环境泄漏的失败用例~~ — **已完成**，新增 `mcp/tests/conftest.py`，全量 282 passed。
4. **把 `feat/cmx-files-ask` 开 PR 合回 `main`。** 145 个提交、约 15.7k 行悬在功能分支上，`main` 已落后 10 天，而目标 Windows 实际跑的是这条分支——`main` 当前**不是**可信回滚点。这是 P0 里唯一还没做的。

### P1 — 有代码、缺真机验收

4.5. **Owner 在 ChatGPT 里真实发一条动态**，确认写权限端到端可用（scope 已具备，只差这一下）。
4.6. **Owner 从真实 TG 私聊发一条语音验收 R18**：生产代码、v9 迁移、开关、健康检查与无私人内容 smoke 已完成；剩余只核对真实 TG 返回的时间轴、compact note 和重复请求缓存。若后续需要「相对平时」，再单独接本地声学/baseline，不阻塞当前首发。
5. **iOS Safari 与 Windows 浏览器真实录音验收**：`audio/mp4` / WebM 录制、`cmx-voice-outbox` 断网续传、Mastodon 编辑回填正文与 alt、真实中文转写耗时、清站点数据会丢未发送录音。
6. **定位语音条播放器「PC 端完全没接管」**（问题 B）。`window.__piVoiceDebug()` 可一次性打出断点（含画中画占位符计数）。波形与画中画（问题 A/C）已在 v16 修复并由 Owner 确认。
7. **worker 跨居民空正文闭环**：现有两个 bot 的时间线可见性与 Token scope 不允许「另一个居民发空正文音频 → worker 用 Qwen 回复」，需要先造出可见性再验。
8. **`/files/ask` 从未实测、也没有调用方**：要么补一次真机验证并接上调用者，要么明确记为暂缓。
9. **`setup-ai.ps1` 用真实新邮箱完整走一次开户流程**；已有账号的授权/DPAPI/读链路早已验证，缺的只有新建账号那一段。
10. **Clip Brain 剪贴板影子站（#33）**：真机与真实 Mastodon 登录态下的端到端未验收。

### P2 — 隐私与运维欠账

11. **#29：`nginx/default.conf:239` 的 CSP 里硬编码了真实公网域名 9 处**，这是公开仓库中已跟踪文件的真实泄漏。同时 `docs/ARCHITECTURE.md` 声称「配置不写死公网域名」，与实际相反，已就地修正。PROJECT.md §3 判断该 CSP 头很可能可**直接删除**而非模板化——删之前需确认 Mastodon 自带 CSP 足够。
12. ~~STDIO MCP 进程堆积~~ — **不是问题，已排除**。本机的 5 个 `cmx-mcp --bot test` STDIO 进程，父进程逐一查证全部是**仍在运行的 `claude.exe`**（本机共 17 个 Claude Code 进程）。每个 Claude Code 会话按配置各起一个自己的 STDIO MCP 服务，属预期行为，不是未回收的孤儿进程。**不要因为「数量多」就去杀它们**，那会打断正在使用的会话。
12.5. **修 `http-stop.ps1` 的假成功分支**（见「代码落点」）：PID 已死但服务仍在时，它会删掉 PID 文件并报告「已停止」，实际没停。正确做法是回落到按 8766 端口属主定位真身。这条会让任何「停→改→启」的运维流程静默失效，包括 `一键更新.bat`。
13. **#25：本地 MCP 421 探测与备份版本标签**（小）。
14. ~~#28：CapsWriter-Offline 中文转写参考~~ — **已关闭**（2026-08-10）。Qwen3-ASR 已落地并跑在 6016。
15. ~~分支清理~~ — **已完成**（2026-08-10），远端只剩 `main` 与 `release/v0.1.0-web-mvp`。
16. **#32 密码管理与网络防控**（大，未拆解）；**#2 后续功能更新**（长期收集箱）。

### 待外部条件

17. 在具备 ChatGPT Pro/工作区资格的账号中创建 `https://<WEB_DOMAIN>/mcp/gpt` 自定义 App：待账号功能开放。
18. 是否为生产常驻居民开启 Remote Social：仍是待单独决策项，当前只对 `test` 做过受控验证。
19. Telegram/Fable 客户端接入：需要时再处理，不阻塞任何上述项。

## 11. 分支与版本纪律

- `main`：唯一稳定开发与部署入口，且**当前确实如此**——2026-08-10 起磁盘工作区与目标 Windows 运行的都是 `main`；
- `release/v0.1.0-web-mvp`：基础网页 MVP 固定快照，远端仅存的另一条分支；
- 归档快照与回滚点分支已按 Owner 指示全部删除，不再维护第二套历史入口。回滚依赖改为：Git 历史本身、`backups/` 下的数据备份，以及 `mastodon-overrides/v4.6.3/` 这类留在树内的版本目录；
- 功能分支验证后合并并删除，不长期悬挂；
- 设计过程稿不得长期作为第二套当前事实保留。

## 12. Agent 更新契约

事实优先级：用户确认需求 → 实际代码与运行证据 → 本文件 → 详细文档 → Issue。

改变需求、边界、架构、接口、数据所有权、运行流程或进度时：

- 先原地更新本文件；
- 再更新受影响的详细文档和 Issue；
- 删除陈旧事实，不建立重复状态文档；
- 明确区分“计划中”“已实现/未验证”“已验证”；
- 没有目标电脑真实输出时不得声称部署或 smoke 成功。
