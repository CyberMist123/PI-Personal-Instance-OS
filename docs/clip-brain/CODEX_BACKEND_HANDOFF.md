# Clip Brain 后端同步与视觉收口：Codex 施工单

> **2026-07-29 修订。** 按 Owner 决策扩充范围：搜索、手动主题标签、★ 收藏由「禁止」
> 改为 v1 正式功能；批量动作由两个改为三个；每条文件上限 30 → 20；新增 2 GiB 账户
> 总配额与亮/暗双主题。范围见 [`V1_SCOPE.md`](./V1_SCOPE.md)，边界见
> [`PRODUCT_BOUNDARY.md`](./PRODUCT_BOUNDARY.md)。原 `DEMO_SCOPE.md` 已作废删除。

## 0. 身份、分支与硬边界

你是本轮实现者。目标分支：

```text
feat/clip-brain-backend
```

基线来自 `demo/clip-brain-site-link`。不得修改、移动或合并 `main`，不得开 PR，未完成全部验证前不得部署。

禁止：

- `docker compose down -v`、清卷、prune 或删除现有数据；
- 修改 `LOCAL_DOMAIN=pi.invalid`；
- 把 Clipboard 内容写成 Mastodon status、media attachment 或写入 Mastodon PostgreSQL；
- 公网分享码、二维码、匿名下载和无需登录的链接；
- AI、语义/向量检索、自动分类、自动打标、自动总结、文件预览和转嘟文；
- 无上限的永久保存：★ 收藏免于 24h 焚毁，但仍受 2 GiB 账户总配额硬顶约束；
- 把 Mastodon Session Token 或文件 Blob 写入磁盘、SQLite、localStorage、日志或错误正文
  （只有主题偏好这一项标量允许存 localStorage）；
- 继续把功能塞进 `remote.py` 或单一前端巨型文件；
- 使用斯普拉遁现成角色、Logo、字体、贴图或受版权保护素材。

所有新增前端文件和新的 Python 模块接近 300 行时必须先拆分。`remote.py` 只允许保留很薄的 route 注册/组装改动。

## 1. 产品事实

Clip Brain 是 CMX 的影子网站：

- 同一公网域名；
- Mastodon 根页面位于 `/`；
- Clipboard 位于 `/clipboard/`；
- 复用当前 Owner 的 Mastodon 登录身份；
- Clipboard 数据不进入时间线，也不进入联邦；
- 每条创建后默认 24 小时自动焚毁；**★ 收藏的条目清除 `expires_at`，不进焚毁队列**，
  取消收藏则按当时时间重新起算 24 小时；
- PC、Mac、手机通过后端读取同一批数据；
- 单条可含文本、文件或两者；
- 文本最多 10000 个 Unicode code point；
- 每条最多 **20** 个文件；
- 文本 UTF-8 字节与文件字节合计必须严格小于 1 GiB；
- 每账户总量上限 **2 GiB**（收藏一并计入）；用量 > 1.5 GiB 才显示容量计；达上限拒绝新建，
  不自动删除旧内容；
- 主题标签由 Owner **手动**指定；类型筛选（文本/图片）按已有元数据判定；
- 关键词检索限当前账号、覆盖当前视图内的正文与文件名；
- 任意文件类型允许上传，但一律下载，不做浏览器内执行或预览。

## 2. 可以复用的 CMX 基础设施

优先复用现有 `cmx-mcp-http` Starlette 进程，而不是新增第四套 Web 服务。

现有可复用事实：

- `mcp/src/cmx_mcp/remote.py` 已使用 Starlette/uvicorn，且服务只绑定 loopback；
- `_verify_mastodon_bearer` 已通过 `/api/v1/accounts/verify_credentials` 验证网页自己的 Bearer，且不落盘；
- 已有 SQLite WAL、文件名清理、文件落盘、下载和配额实现；
- Nginx 已经把同源请求转发给 CMX HTTP 服务；
- `/clipboard/` 静态页面和 Mastodon 字标双向入口已经存在于基线分支。

只复用登录验证、Starlette、SQLite/磁盘存储方法和 Nginx 同源入口。不要把 Clipboard 套进 `filebox_files` 或 Mastodon media/status 模型。

## 3. 后端模块边界

建议结构：

```text
mcp/src/cmx_mcp/
├─ web_auth.py          # Mastodon 网页 Bearer 验证，返回最小 account identity
├─ clipboard_store.py   # 独立 SQLite 元数据、事务、收藏与过期清理
├─ clipboard_search.py  # FTS 索引维护与关键词查询
├─ clipboard_files.py   # staging、原子落盘、安全文件名和删除
├─ clipboard_api.py     # Starlette routes、输入校验和响应
└─ remote.py            # 仅调用 build_clipboard_routes(...) 并加入 lifespan
```

数据保存在已经被忽略的运行目录：

```text
mcp/runtime/clipboard.sqlite3
mcp/runtime/clipboard/objects/<entry_id>/<file_id>
```

不要把私人文件保存在 Git 跟踪目录。不要沿用 `filebox_files` 表；Clipboard 使用独立数据库，便于整体回滚和清理。

## 4. 最小数据模型

```sql
clipboard_entries(
  entry_id TEXT PRIMARY KEY,
  owner_account_id TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER,              -- NULL = 已收藏，不焚毁
  favorited_at INTEGER,            -- NULL = 临时
  topic TEXT,                      -- 手动主题标签，NULL = 未归类
  total_bytes INTEGER NOT NULL,
  file_count INTEGER NOT NULL
)

-- 关键词检索：正文 + 文件名，contentless FTS5，按 entry_id 关联
clipboard_fts USING fts5(
  body,
  entry_id UNINDEXED,
  owner_account_id UNINDEXED
)

clipboard_files(
  file_id TEXT PRIMARY KEY,
  entry_id TEXT NOT NULL REFERENCES clipboard_entries(entry_id) ON DELETE CASCADE,
  original_name TEXT NOT NULL,
  safe_name TEXT NOT NULL,
  content_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  created_at INTEGER NOT NULL
)
```

要求：

- SQLite 开启 WAL、foreign_keys、busy_timeout 和 `auto_vacuum = INCREMENTAL`；
  每轮过期清理后执行 `PRAGMA incremental_vacuum`。Clipboard 每天焚毁大量行，
  不回收就会让库文件单调增长（PicoShare 踩过同一个坑，只能让用户手动 VACUUM）；
- `expires_at` 只由服务器计算为 `created_at + 86400`，忽略客户端传值；
- 收藏时把 `expires_at` 置 NULL 并写入 `favorited_at`；取消收藏时按**当时**时间重新计算
  `expires_at = now + 86400`，不沿用原始 `created_at`（否则旧条目一取消收藏就立刻消失）；
- 过期清理必须跳过 `expires_at IS NULL` 的行；
- 账户用量为该 `owner_account_id` 全部条目 `total_bytes` 之和，**含收藏**；
  新建前校验 `已用 + 本次 <= 2 GiB`，超出即拒绝，不自动删除旧内容；
- FTS 行随条目创建/删除同步维护；`body` 为正文与全部 `original_name` 的拼接；
  检索必须同时按 `owner_account_id` 过滤，不能只靠 FTS 匹配；
- 列表和文件读取都必须绑定 `owner_account_id`；
- 多文件上传先写 staging；全部校验和写入成功后再原子 rename；任一步失败必须回滚 SQLite 并删除 staging；
- 删除条目、删除单文件和过期清理必须同时删除元数据与磁盘对象；
- 启动时清理残留 staging；每次 list/mutation 前清理过期项，并运行低频后台 sweep；
- 自动清理失败要记录最小错误信息，但绝不记录文本、文件名以外的文件内容、Bearer 或完整请求体。

## 5. 身份与安全

把现有 Bearer 验证提取到 `web_auth.py`，返回至少：

```text
account_id
acct
```

前端从 Mastodon 根 HTML 的 `#initial-state` 读取 access token，仅保存在当前 JS 内存中。所有 `/clipboard-api/*` 请求携带：

```http
Authorization: Bearer <current Mastodon web token>
```

后端每次验证或使用短 TTL 内存缓存；缓存键只能是 token 的 SHA-256，不保存明文。缓存不是必需项，先正确再优化。

所有变更请求同时检查 `Origin`：

- 正式站只接受当前 `public_origin`；
- 测试只接受明确的 loopback origin；
- 缺失或错误 Origin fail closed；
- 所有 API 响应 `Cache-Control: no-store`；
- 下载使用 `Content-Disposition: attachment` 和 `X-Content-Type-Options: nosniff`；
- 文件实际路径只由服务器生成的 ID 决定，客户端不得提供磁盘路径；
- 原始文件名只用于显示和下载名，必须去控制字符、路径分隔符和 CR/LF；
- 不解压压缩包，不扫描或执行用户文件。

## 6. API 契约

统一前缀：

```text
/clipboard-api
```

最小路由：

```text
GET    /clipboard-api/entries?view=&topic=&type=&q=
POST   /clipboard-api/entries
PATCH  /clipboard-api/entries/{entry_id}
DELETE /clipboard-api/entries/{entry_id}
DELETE /clipboard-api/entries/{entry_id}/files/{file_id}
POST   /clipboard-api/entries/delete-many
GET    /clipboard-api/entries/{entry_id}/files/{file_id}
GET    /clipboard-api/usage
```

`POST /entries` 使用 multipart：

- `text`：可空；
- `files`：0..20；
- 文本和文件不能同时为空；
- 单条总字节严格 `< 1073741824`；
- 账户累计超过 `2147483648` 时拒绝，错误码与单条超限区分开；
- 超限在写入正式目录前拒绝；
- 成功返回完整 entry JSON。

`PATCH /entries/{entry_id}`：

- 仅接受 `favorite`（bool）与 `topic`（string 或 null）两个字段；
- 只能改当前 account 自己的条目；
- 收藏语义按 §4；
- 返回更新后的完整 entry JSON。

`GET /usage`：返回 `{used_bytes, quota_bytes, warn_bytes}`；前端仅在
`used_bytes > warn_bytes`（1.5 GiB）时渲染容量计。

`GET /entries`：

- `view=temporary`（默认）只返回未过期且未收藏的；`view=favorite` 只返回已收藏的；
- `topic` 与 `type` 为可选筛选；`q` 为可选关键词，限当前账号、当前视图；
- 最新在前；
- 不分页，第一版最多返回 100 条；超过时返回明确 `truncated: true`，但前端仍采用单一滚动区；
- 文件响应只含 ID、原始名、类型、大小和下载 URL，不把 Blob 放进 JSON。

`delete-many`：

- 最多 100 个 entry ID；
- 事务内删除；
- 只删当前 account 的条目；
- 返回实际删除数量；
- 不允许“空选择默认删除全部”，前端想作用于全部时必须显式发送当前全部 ID。

## 7. 前端数据改造

IndexedDB 不再作为事实源。改为：

- 页面加载调用 `GET /clipboard-api/entries`；
- 新增、删除和删除文件走 API；
- `BroadcastChannel` 只用于同浏览器标签页通知重新拉取；
- 页面获得焦点、visibility 变为 visible 时刷新；
- 页面可见时每 5 秒轻量轮询，隐藏时停止；
- 不在 localStorage/IndexedDB 保存服务端文件 Blob 或 token；
- API 不可用时明确显示“后端未连接”，禁止显示假成功。

本地 `127.0.0.1:4173` Demo 可以保留 mock/IndexedDB 开发模式，但正式 `/clipboard/` 必须走后端。两种模式要在单独 adapter 文件中隔离，例如：

```text
clipboard-client.js
clipboard-client-local.js
```

不得把双模式分支散落在 `app.js`。

## 8. 交互修正

### 8.1 唯一批量入口

右上角当前显示总数的胶囊（例如 `4 条`）就是唯一批量入口，不再额外显示一个隐藏的“已选 N 条”胶囊。

行为：

- 没有勾选时显示 `4 条`；悬停约 260ms 或点击后展开，作用目标为当前视图全部 4 条；
- 勾选后显示 `已选 2 / 4`；批量动作只作用于勾选项；
- 胶囊在展开态与静止态必须**在亮暗两套下都有明显色差**：静止为描边态，
  悬停/展开转实心墨黄。不得出现某一套配色下两态同色（暗色版曾踩过这个坑）；
- 触屏通过点击开合；
- 鼠标移入浮层不消失；
- 无 transition、animation 或浏览器原生 confirm；
- “全部焚毁”仍为按钮内二次确认；
- 改变勾选集合后立即解除已武装的焚毁确认。

**三个动作**（复制与下载不再合并成一个自适应按钮）：

```text
全部复制
全部下载
———
全部焚毁      危险色，与前两项之间有分隔线
```

### 8.3 临时 / 收藏门牌

顶栏「临 / ★」是门牌的正反面，**同时只渲染一个字**：

- `临` 为墨黄态，`★` 为青绿态；
- 悬停变色，**单击翻面**并切换列表视图；
- 不做分段器、不并排显示两个选项。

站点标识同理：右上角只出现本站那一面，位置对应长毛象论坛标识位，点一下切到对面站点，
不显示任何「翻面」提示文案。

### 8.2 滚动而非分页

- 删除上一页、下一页和页码；
- 右侧条目区单一滚动；
- 每条内部文件列表也有固定最大高度和独立滚动；
- `还有 16 个文件` 点击后只打开该条目的内部滚动文件区，不继续撑高整个页面；
- 桌面尽量保持主要操作在一屏；手机自然纵向布局。

## 9. 视觉方向：斯普拉遁式，不是赛博朋克

保留原创，不复制任何游戏资产。已定稿的视觉参考：

```text
demos/clip-brain/design/mockup.html
```

该稿是设计参考，不是上线代码，不受 300 行停止线约束；其中包含的搜索框与主题标签
按 §1 已进入 v1 范围。落地时取其配色令牌、描边、圆角、硬阴影与半调语言。

**亮 / 暗两套都要交付**，令牌集中在 `theme.css`：

```text
             亮色              暗色
正文         #262336          #F2EFE6   暖白，不用纯白
背景         #FFFDF6          #1B1826   暖墨，不用纯黑
描边         #262336          #3E3752
主色         #FFE21F          #FFE21F   墨黄，两套共用
辅色         #22C9B6          #3FBFB0   青绿，暗色下降饱和
危险         #FF5C42          #FF6B54   仅危险操作
黄不可读时   #B8930A          #FFE21F   图标与强调字的深色替身
```

硬性要求：

- **同一层级只框一次**：面板不描边，只有卡片与小贴纸描边；卡片内部零边框，用留白与发丝线；
- 描边 2px，实心硬阴影只留给标识、上传键、批量胶囊三处；
- 卡片左侧 6px 色条区分状态：临时墨黄、收藏青绿；
- 倒计时醒目：等宽加粗，配一条 24h 消耗线，剩余不足时转珊瑚红；
- 卡片不显示创建日期；
- 滚动条隐形（宽度 0），条目区与文件区仍可滚动；
- 检索框不显示提示文案，只显示 `Alt Space`；
- 布局两栏：左投递、右内容流；主题导航横排于底部；标识锁右上角；
- 不设大块拖放虚线框，改为正文下一行的细「＋ 选择文件」按钮，拖拽落在正文框上；
- 文件名显示时**去掉后缀**，类型由前置徽章表示；
- 移除紫红霓虹、发光、终端黑客感和多色竞争；
- 文本与文件卡片先保证可读性，装饰不能盖过内容；
- 无 transition、无 animation 仍是硬约束，hover 为瞬时切换。

## 10. ZIP 与浏览器兼容

- 服务器只保存原始文件，不持久化 ZIP；
- 批量 ZIP 第一版继续在客户端生成；
- 选中内容严格小于 1 GiB；
- 小包走普通浏览器下载；
- Chrome/Edge 大包可走 File System Access API；
- Safari/iOS 不支持大包直存时必须明确提示，并保留单文件下载；不得假装成功；
- 核心上传、列表、复制、单文件下载、删除和 24h 同步必须兼容桌面 Chrome/Edge、macOS Safari 和 iOS Safari。

不要为兼容性引入服务端永久 ZIP 或匿名下载入口。

## 11. 自动测试

必须新增后端测试并保留前端契约测试。

后端至少覆盖：

- 未认证 401；错误 Origin 403；
- Bearer 明文不进入 DB/日志；
- 两个独立客户端以同一 account 登录时看到相同条目；
- 不同 account 互相不可见；
- 10000 字允许、10001 字拒绝；
- **20 文件允许、21 文件拒绝**；
- 单条总量 `1 GiB - 1 byte` 允许，`1 GiB` 拒绝；
- **账户累计超过 2 GiB 拒绝新建，且错误码与单条超限可区分**；
- **收藏条目不被过期清理删除；取消收藏后按当时时间重新计时，不立即消失**；
- **`view=favorite` 与 `view=temporary` 互斥且都按 account 隔离**；
- **关键词检索限本账号、覆盖正文与文件名；跨账号关键词命中必须为空**；
- **`PATCH` 只接受 `favorite` / `topic`，改他人条目返回 404 或 403**；
- 任意扩展名上传；路径穿越文件名安全；
- staging 失败完整回滚；
- 单文件删除不误删正文或其他文件；
- 批量删除只删显式 ID；
- 到期后 list/download 返回不可见，磁盘对象被清理；
- 启动清理 orphan staging；
- `Cache-Control: no-store`、attachment、nosniff。

前端契约至少覆盖：

- 无分页 DOM；
- 总数胶囊就是批量入口，且展开后**恰好三个**动作按钮；
- 无勾选时目标为全部，勾选后只作用于选择；
- **临 / ★ 门牌同时只渲染一个字，不存在并排分段器**；
- **站点标识只渲染一面，页面不同时出现两个站名**；
- **亮暗两套令牌齐备，且批量胶囊静止态与展开态在两套下都不同色**；
- **检索框无提示文案，只有快捷键文本**；
- **卡片不渲染创建日期**；
- 条目区和条目文件区都有滚动边界；
- 无 `window.confirm`、transition、animation；
- 正式模式不使用 IndexedDB 作为事实源；
- API 失败时不出现成功记录；
- 所有前端文件低于 300 行。

同时运行：

```powershell
py -3 -m unittest discover -s demos\clip-brain\tests -p "test_*.py" -v
Set-Location mcp
.\.venv\Scripts\python.exe -m pytest -q
Set-Location ..
docker compose config --quiet
```

## 12. 分阶段交付与部署

### 阶段 A：实现与本地测试

只在 `feat/clip-brain-backend` 施工。不得部署。

### 阶段 B：只读审查

输出：

- HEAD；
- 变更文件；
- 每个新增模块行数；
- 前后端测试结果；
- 安全边界；
- macOS/iOS 已知限制；
- 是否存在任何生产数据迁移。

### 阶段 C：受控部署（仅全部测试通过后）

Owner 已授权在该功能分支上联调正在运行的网站，但仍禁止合并。部署前必须：

1. 确认工作区干净；
2. 记录当前分支和 HEAD；
3. 备份将修改的 Nginx/compose 配置；
4. 不修改 Mastodon PostgreSQL、Redis 和媒体卷；
5. 只重启 `cmx-mcp-http` 与必要的 Nginx 容器；
6. 不执行全栈 down，不重启 db/redis/web/sidekiq/streaming；
7. 部署失败立即恢复旧配置和旧服务 HEAD。

部署后验证：

- 未登录访问 `/clipboard/` 回登录页；
- 已登录可双向切换；
- PC 新建后 Mac/手机刷新可见；
- Mac/手机新建后 PC 可见；
- 删除和 24h 过期跨设备一致；
- Mastodon 时间线没有产生任何 Clipboard status；
- `/api/v2/instance`、发布、语音和 MCP smoke 未回归；
- Nginx、MCP HTTP 日志无 token、正文和文件内容泄露。

完成后停止，不开 PR、不合并，等待 Owner 验收。