# Clip Brain v1 功能范围

> 取代已作废的 `DEMO_SCOPE.md`（v0 纯本地 IndexedDB Demo）。
> 2026-07-29 按 Owner 决策同步。边界见 [`PRODUCT_BOUNDARY.md`](./PRODUCT_BOUNDARY.md)，
> 施工顺序见 [`CODEX_BACKEND_HANDOFF.md`](./CODEX_BACKEND_HANDOFF.md)。

## 目标

把静态 Clip Brain Demo 升级为 CMX 同域下的真正影子网站：复用 Mastodon 登录态，
PC / Mac / 手机读取同一后端数据，条目默认 24 小时焚毁、可 ★ 收藏保留。

## 功能清单

### 身份与同步

1. 复用当前 Mastodon 网页登录态；未登录访问 `/clipboard/` 回到 `/auth/sign_in`；
2. 前端 token 只在 JS 内存，不进 SQLite、磁盘、localStorage 或日志；
3. 服务端为唯一事实源；同一账号的两台设备看到同一批内容；
4. 不同账号内容互不可见；
5. 页面获得焦点或 visibility 变为 visible 时刷新；可见时每 5 秒轻量轮询，隐藏即停；
6. `BroadcastChannel` 只用于同浏览器标签页通知重新拉取；
7. 后端不可用时明确显示「后端未连接」，禁止显示假成功。

### 条目

8. 一条记录包含文本、文件或两者，两者不能同时为空；
9. 文本上限 10000 个 Unicode code point；
10. 每条最多 **20** 个文件，任意类型；
11. 每条文本 UTF-8 字节 + 文件字节合计**严格小于 1 GiB**；
12. 账户总量上限 **2 GiB**（含收藏条目）；用量 > 1.5 GiB 时才显示容量计；达上限拒绝新建；
13. `expires_at = created_at + 86400`，由服务器计算，忽略客户端传值。
    每天焚毁大量行会让 SQLite 文件单调增长——**删除的空间不会自动归还**，
    所以建库时开启 `PRAGMA auto_vacuum = INCREMENTAL`，每轮过期清理后执行
    `PRAGMA incremental_vacuum`，并同步删除磁盘上的空对象目录；
14. 文件显示原始文件名（**去掉后缀**，类型由前置徽章表示）与大小；
15. 保存前移除单个文件；保存后删除单个文件，且不误删正文或其他文件。

### 临时 / 收藏

16. 顶栏「临 / ★」是门牌正反面：只出现一个字，悬停变色，**单击翻面**切换视图；
17. ★ 收藏清除 `expires_at`，该条不再进入焚毁队列；取消收藏则按当时时间重新起算 24 小时；
18. 收藏条目卡片以浅青底与青色左色条区分，倒计时位显示「不焚毁」；
19. 收藏不解除 2 GiB 总配额约束。

### 主题与检索

20. 主题标签**手动**指定，不做自动分类、自动打标或自动总结；
21. 类型筛选（文本 / 图片）由服务端按已有元数据判定；
22. 关键词检索覆盖当前视图内的正文与文件名，限当前账号；不做语义或向量检索；
23. `Alt + Space` 聚焦检索框；检索框不显示提示文案，只显示快捷键。

### 批量与下载

24. 右上角「N 条」胶囊是**唯一批量入口**，无独立「已选 N 条」胶囊；
25. 悬停约 260ms 或点击后瞬时展开，鼠标移入浮层不收起，触屏以点击开合；
26. 展开后**三个动作**：全部复制 / 全部下载 ／分隔线／ 全部焚毁；
27. 无勾选时作用于当前视图全部；有勾选时胶囊显示「已选 N / M」且只作用于勾选项；
28. 改变勾选集合后立即解除已武装的焚毁确认；
29. 「全部焚毁」为同一按钮二次点击确认，服务端在一个事务内只删显式 ID；
30. 双击正文即复制，无复制按钮；
31. 单条与多选 ZIP 仍在浏览器本地生成：不满 256 MiB 走普通下载，超过则走 File System Access API 直存；
32. Safari / iOS 不支持大包直存时必须明确提示并保留单文件下载，不得假装成功；
33. 打包内容按 UTF-8 文本字节 + 文件字节计量，必须严格小于 1 GiB。

### 视觉

34. 原创墨黄斯普拉遁式：粗描边贴纸、不规则墨迹、半调网点；不使用现成游戏角色、Logo、字体或贴图；
35. **亮 / 暗两套主题**均需交付，切换项在底部导航；主题偏好可存 localStorage（token 与文件 Blob 一律不可）；
36. 卡片左侧色条区分状态：临时黄、收藏青；剩余时间不足时 24h 消耗线转珊瑚红；
37. 倒计时醒目：等宽加粗，亮色用琥珀 `#B8930A`、暗色用墨黄 `#FFE21F`；
38. 卡片不显示创建日期，只显示倒计时；
39. 滚动条隐形（宽度 0），条目区与文件区仍可滚动；
40. 布局两栏：左投递、右内容流，主题导航横排于底部，标识锁在右上角；
41. 无 `transition`、无 `animation`，页面与浮层均立即渲染。

## 明确不做

- 公网分享码、二维码、匿名下载或公开链接（Issue #26 已取消）；
- AI、MCP、识图、语义检索、自动分类、自动打标、自动总结；
- 文件预览、在线编辑、版本历史；
- 转嘟文，或把 Clipboard 内容写入 Mastodon status / media / PostgreSQL；
- 无上限的永久保存（2 GiB 硬顶）；
- `docker compose down -v`、清卷、prune、修改 `LOCAL_DOMAIN` 或触碰 Mastodon 数据卷。

## 代码停止线

```text
mcp/src/cmx_mcp/
├─ web_auth.py            Mastodon 网页 Bearer 验证 → 最小 account identity
├─ clipboard_store.py     独立 SQLite、事务、收藏与过期清理
├─ clipboard_search.py    FTS 索引与关键词查询
├─ clipboard_files.py     staging、原子落盘、安全文件名、删除
├─ clipboard_api.py       Starlette routes、输入校验、响应
└─ remote.py              仅调用 build_clipboard_routes(...) 并加入 lifespan

demos/clip-brain/clipboard/
├─ index.html             页面骨架
├─ theme.css              亮/暗令牌
├─ styles.css             基础视觉
├─ components.css         卡片、文件、贴纸
├─ toolbar.css            门牌、批量入口、检索
├─ clipboard-client.js    后端 adapter（正式模式）
├─ clipboard-client-local.js  IndexedDB adapter（仅 127.0.0.1:4173）
├─ auth-gate.js           登录态与内存 token
├─ compose.js             投递区与待传文件
├─ archive.js             ZIP 编码与流式写入
├─ archive-output.js      浏览器下载 / 本地直存分流
├─ downloads.js           复制与单文件下载
├─ destructive.js         按钮内二次确认
├─ bulk.js                批量复制、打包与焚毁
├─ toolbar.js             门牌、批量浮层、检索、主题
├─ view.js                渲染
└─ app.js                 状态与交互编排
```

- 任一前端文件或新增 Python 模块达到 300 行前必须先拆分；
- `remote.py` 只允许很薄的 route 注册/组装改动；
- 双模式 adapter 不得散落进 `app.js`；
- 新功能超出上方清单时先停工，不顺手扩 scope。

## 自动验收

```powershell
py -3 -m unittest discover -s demos\clip-brain\tests -p "test_*.py" -v
Set-Location mcp
.\.venv\Scripts\python.exe -m pytest -q
Set-Location ..
docker compose config --quiet
```

后端至少覆盖：未认证 401；错误 Origin 403；Bearer 明文不进 DB/日志；同账号两客户端一致；
跨账号不可见；10000 允许 / 10001 拒绝；20 文件允许 / 21 拒绝；单条 `1 GiB - 1 byte` 允许 / `1 GiB` 拒绝；
账户累计超 2 GiB 拒绝新建；任意扩展名；路径穿越文件名安全；staging 失败完整回滚；
单文件删除不误删其他；批量删除只删显式 ID；收藏条目不被过期清理删除、取消收藏后重新计时；
关键词检索限本账号且覆盖正文与文件名；到期后 list/download 不可见且磁盘对象被清理；
启动清理 orphan staging；`Cache-Control: no-store`、attachment、nosniff。

前端契约至少覆盖：无分页 DOM；总数胶囊为唯一批量入口且展开后**三个**动作；
无勾选时目标为全部、勾选后只作用于选择；门牌同时只渲染一个字；
条目区与条目文件区都有滚动边界；无 `window.confirm`、`transition`、`animation`；
亮暗两套令牌齐备；正式模式不使用 IndexedDB 作为事实源；API 失败时不出现成功记录；
所有前端文件低于 300 行。

## Owner 人工验收

- [ ] 未登录访问 `/clipboard/` 回登录页；
- [ ] PC 新建后 Mac / 手机刷新可见，反向亦然；
- [ ] 删除与 24h 过期跨设备一致；
- [ ] ★ 收藏后跨设备不再焚毁；
- [ ] 门牌单击可在临时 / 收藏间翻面，且同时只显示一面；
- [ ] 右上角标识与长毛象标识位置对应，点一下切站；
- [ ] 「N 条」悬停即变色并弹出，亮暗两套都有明显变化；
- [ ] 展开后三个动作齐全，焚毁需二次点击；
- [ ] 双击正文可复制；
- [ ] ≤256 MiB 的 ZIP 进入浏览器下载记录，>256 MiB 走本地直存；
- [ ] iOS Safari 大包提示明确，未假装成功；
- [ ] 亮 / 暗切换在手机与桌面均正常；
- [ ] Mastodon 时间线未出现任何 Clip Brain 内容；
- [ ] `/api/v2/instance`、发布、语音与 MCP smoke 未回归。
