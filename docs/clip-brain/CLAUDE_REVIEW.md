# Clip Brain Claude Code 只读验收单

## 身份与边界

你是独立审查者，不是实现者。目标分支为 `feat/clip-brain-backend`。

范围以 [`V1_SCOPE.md`](./V1_SCOPE.md) 与 [`CODEX_BACKEND_HANDOFF.md`](./CODEX_BACKEND_HANDOFF.md)
为准（2026-07-29 已同步）。已作废的 `DEMO_SCOPE.md` 不再作为验收依据。

禁止：

- 修改任何文件；
- 自动修复、格式化或升级依赖；
- commit、push、开 PR、部署或合并；
- 启停生产 Mastodon、Nginx、MCP、compose 服务；
- 删除 Owner 的 IndexedDB 内容；
- 把真实私人域名写入新文档或测试输出。

发现问题只报告，不施工。

## Windows 目标目录

```text
D:\AI\PI-OS-clip-demo
```

## 第 1 阶段：安全更新并切到本地分支

```powershell
Set-Location "D:\AI\PI-OS-clip-demo"
git status --short --branch
```

如果工作区不干净，立即停止并报告。工作区干净时执行：

```powershell
git -c gc.auto=0 fetch origin

if (git branch --list feat/clip-brain-backend) {
  git switch feat/clip-brain-backend
  git merge --ff-only origin/feat/clip-brain-backend
} else {
  git switch --track -c feat/clip-brain-backend origin/feat/clip-brain-backend
}

git rev-parse HEAD
git status --short --branch
git worktree list
```

必须确认：

- 当前 worktree 位于 `feat/clip-brain-backend`；
- 原仓库的 `main` 分支仍然存在且未移动；
- 没有 merge、rebase 或生产部署。

如果 fetch 的自动清理提示 `.idx` 无法 unlink，选择 `n`；不得手动删除 `.git/objects/pack` 文件。

## 第 2 阶段：自动测试

```powershell
py -3 -m unittest discover -s demos\clip-brain\tests -p "test_*.py" -v
Set-Location mcp
.\.venv\Scripts\python.exe -m pytest -q
Set-Location ..
```

前端契约必须逐项确认：

- 所有前端文件与新增 Python 模块少于 300 行；
- 全部 JavaScript 语法通过；
- HTML 存在 `auth-gate.js`；站点标识**只渲染一面**，页面不同时出现两个站名；
- 页面不含分页 DOM、`select-page` 或 `selection-bar`；
- 总数胶囊是唯一批量入口，无独立“已选 N 条”胶囊；
- 鼠标进入胶囊可触发 260ms 延时展开，动作区为绝对定位浮层；
- 展开后**恰好三个**动作：全部复制 / 全部下载 / 全部焚毁；
- 批量胶囊静止态与展开态在**亮暗两套下都不同色**；
- 「临 / ★」门牌同时只渲染一个字，不存在并排分段器；
- 亮暗两套令牌齐备；检索框无提示文案，只有快捷键文本；卡片不渲染创建日期；
- 页面没有 `window.confirm`、transition 或 animation；
- 待上传文件区、条目区与每条文件区均可独立滚动；
- 正式模式不以 IndexedDB 为事实源，4173 本地 adapter 明确例外；
- API 失败时不出现成功记录。

后端必须逐项确认：

- 未认证 401、错误 Origin 403；Bearer 明文不进 DB 与日志；
- 跨 account 不可见；关键词检索限本账号；
- 10000/10001、20/21 文件、单条 1 GiB、账户累计 2 GiB 边界全部通过；
- 收藏条目不被过期清理删除，取消收藏后重新计时；
- staging 失败完整回滚；启动清理 orphan staging；
- 到期后 list/download 不可见且磁盘对象被清理；
- `Cache-Control: no-store`、attachment、nosniff。

同时确认基础设施：

- 小 ZIP 走浏览器下载，大 ZIP 走本地直存；ZIP CRC、内容与危险文件名清理通过；
- `compose.yml` 只读挂载 Clip Brain 静态目录；
- Nginx 存在 `/clipboard`、`/clipboard/`、`/clipboard-api/`、站点切换脚本和严格 CSP；
- `/clipboard-api/` 的 `client_max_body_size` 高于 1 GiB，使 413 由应用而非 Nginx 给出；
- `/clipboard/` 页面会复用 Mastodon Session；
- 不存在公网分享入口。

同时只做配置解析，不启动服务：

```powershell
docker compose config --quiet
```

## 第 3 阶段：本地静态页面 smoke

只在 4173 端口未被占用时启动：

```powershell
$listener = Get-NetTCPConnection -LocalPort 4173 -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
  $server = Start-Process py -ArgumentList @(
    '-3','-m','http.server','4173','--bind','127.0.0.1',
    '--directory','demos\clip-brain'
  ) -PassThru
  Start-Sleep -Seconds 2
}

$response = Invoke-WebRequest "http://127.0.0.1:4173/clipboard/" -UseBasicParsing
$response.StatusCode
$response.Content | Select-String 'auth-gate.js|archive-output.js|clipboard-client.js|theme.css'
```

如果本轮启动了 `$server`，检查结束后只停止该 PID：

```powershell
if ($server) { Stop-Process -Id $server.Id }
```

不得按进程名批量结束 Python。

## 第 4 阶段：只读 diff 审查

```powershell
git diff --stat a871628514efaa21f466af2240790ff6a8826d36..HEAD
git diff --name-only a871628514efaa21f466af2240790ff6a8826d36..HEAD
git status --short --branch
```

允许范围只有：

- `demos/clip-brain/`；
- `docs/clip-brain/`；
- `mcp/src/cmx_mcp/` 中新增的 `web_auth.py`、`clipboard_*.py`，
  以及 `remote.py` 里很薄的 route 注册／lifespan／请求体上限豁免改动；
- `mcp/tests/test_clipboard.py`；
- `compose.yml` 中 Clip Brain 只读挂载与 streaming healthcheck 对齐；
- `nginx/default.conf` 中 `/clipboard/`、`/clipboard-api/` 路由和同源入口注入；
- `CLAUDE.md` 中隐私收官提醒。

`remote.py` 若出现业务逻辑而非组装代码，直接判为阻断问题。

## 输出格式

报告必须包含：

1. 审查 HEAD 和当前本地分支；
2. 自动测试通过/失败数量与原始失败名；
3. `docker compose config --quiet` 结果；
4. 页面 HTTP smoke；
5. 每个前端文件行数；
6. diff 范围；
7. `main` 是否保留且未移动；
8. 是否发现阻断问题；
9. 明确写明“未编辑、未提交、未部署、未开 PR”。

浏览器真实悬停、Mastodon 品牌点击、生产登录跳转、下载记录、双标签页同步和 Windows 解压体验仍由 Owner 人工验收，不得冒充已经验证。
