# Clip Brain 后端同步与视觉收口：Codex 施工单

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
- AI、搜索、分类、预览、永久保存和转嘟文；
- 把 Mastodon Session Token 写入磁盘、SQLite、localStorage、日志或错误正文；
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
- 每条创建后固定 24 小时自动焚毁；
- PC、Mac、手机通过后端读取同一批数据；
- 单条可含文本、文件或两者；
- 文本最多 10000 个 Unicode code point；
- 每条最多 30 个文件；
- 文本 UTF-8 字节与文件字节合计必须严格小于 1 GiB；
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
├─ clipboard_store.py   # 独立 SQLite 元数据、事务和过期清理
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
  expires_at INTEGER NOT NULL,
  total_bytes INTEGER NOT NULL,
  file_count INTEGER NOT NULL
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

- SQLite 开启 WAL、foreign_keys 和 busy_timeout；
- `expires_at` 只由服务器计算为 `created_at + 86400`，忽略客户端传值；
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
GET    /clipboard-api/entries
POST   /clipboard-api/entries
DELETE /clipboard-api/entries/{entry_id}
DELETE /clipboard-api/entries/{entry_id}/files/{file_id}
POST   /clipboard-api/entries/delete-many
GET    /clipboard-api/entries/{entry_id}/files/{file_id}
```

`POST /entries` 使用 multipart：

- `text`：可空；
- `files`：0..30；
- 文本和文件不能同时为空；
- 总字节严格 `< 1073741824`；
- 超限在写入正式目录前拒绝；
- 成功返回完整 entry JSON。

`GET /entries`：

- 只返回未过期内容；
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

- 没有勾选时显示 `4 条`；悬停约 260ms 或点击后展开两个按钮，作用目标为当前全部 4 条；
- 勾选后显示 `已选 2 / 4`；批量动作只作用于勾选项；
- 触屏通过点击开合；
- 鼠标移入浮层不消失；
- 无 transition、animation 或浏览器原生 confirm；
- “全部焚毁”仍为按钮内二次确认；
- 改变勾选集合后立即解除已武装的焚毁确认。

两个动作仍只有：

```text
全部复制 / 下载
全部焚毁
```

### 8.2 滚动而非分页

- 删除上一页、下一页和页码；
- 右侧条目区单一滚动；
- 每条内部文件列表也有固定最大高度和独立滚动；
- `还有 16 个文件` 点击后只打开该条目的内部滚动文件区，不继续撑高整个页面；
- 桌面尽量保持主要操作在一屏；手机自然纵向布局。

## 9. 视觉方向：斯普拉遁式，不是赛博朋克

保留原创，不复制任何游戏资产。视觉关键词：

- 喷墨、贴纸、斜切块面、粗黑描边、不规则圆角；
- 暖白/墨黑作为主体；
- 主强调色为墨黄；
- 辅助色只留一种低饱和青绿；
- 红色只用于危险操作；
- 移除紫红霓虹、发光、终端黑客感和多色竞争；
- 允许 CSS 制作少量半调网点、墨滴剪影和胶带形状；
- 文本与文件卡片先保证可读性，装饰不能盖过内容；
- 无动画仍是硬约束。

建议颜色预算：

```text
背景：暖灰黑 / 暖白
主色：墨黄
辅色：低饱和青绿
危险：珊瑚红
其余均为灰阶
```

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
- 30 文件允许、31 文件拒绝；
- 总量 `1 GiB - 1 byte` 允许，`1 GiB` 拒绝；
- 任意扩展名上传；路径穿越文件名安全；
- staging 失败完整回滚；
- 单文件删除不误删正文或其他文件；
- 批量删除只删显式 ID；
- 到期后 list/download 返回不可见，磁盘对象被清理；
- 启动清理 orphan staging；
- `Cache-Control: no-store`、attachment、nosniff。

前端契约至少覆盖：

- 无分页 DOM；
- 总数胶囊就是批量入口；
- 无勾选时目标为全部，勾选后只作用于选择；
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