# Clip Brain Claude Code 只读验收单

## 身份与边界

你是独立审查者，不是实现者。目标分支为 `demo/clip-brain-site-link`。

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

if (git branch --list demo/clip-brain-site-link) {
  git switch demo/clip-brain-site-link
  git merge --ff-only origin/demo/clip-brain-site-link
} else {
  git switch --track -c demo/clip-brain-site-link origin/demo/clip-brain-site-link
}

git rev-parse HEAD
git status --short --branch
git worktree list
```

必须确认：

- 当前 worktree 位于 `demo/clip-brain-site-link`；
- 原仓库的 `main` 分支仍然存在且未移动；
- 没有 merge、rebase 或生产部署。

如果 fetch 的自动清理提示 `.idx` 无法 unlink，选择 `n`；不得手动删除 `.git/objects/pack` 文件。

## 第 2 阶段：自动测试

```powershell
py -3 -m unittest discover -s demos\clip-brain\tests -p "test_*.py" -v
```

必须逐项确认：

- 所有前端文件少于 300 行；
- 全部 JavaScript 语法通过；
- HTML 存在 Mastodon / Clipboard 双入口和 `auth-gate.js`；
- 页面不含 `select-page` 或 `selection-bar`；
- 勾选后仅在标题右侧显示紧凑的“已选 N 条”；
- 鼠标进入整个选择胶囊可触发 260ms 延时展开，动作区为绝对定位浮层；
- 选择菜单只有“全部复制/下载”和“全部焚毁”两个动作；
- 页面没有 `window.confirm`、transition 或 animation；
- 左侧待上传文件区与右侧条目区均可独立滚动；
- 小 ZIP 走浏览器下载，大 ZIP 走本地直存；
- ZIP CRC、内容与危险文件名清理通过；
- 1 GiB 严格边界通过；
- `compose.yml` 只读挂载 Clip Brain 静态目录；
- Nginx 存在 `/clipboard`、`/clipboard/`、站点切换脚本和严格 CSP；
- `/clipboard/` 页面会复用 Mastodon Session；4173 本地 Demo 明确例外；
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
$response.Content | Select-String 'auth-gate.js|selection-trigger|archive-output.js|compact-layout.css'
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
- `compose.yml` 中 Clip Brain 只读挂载；
- `nginx/default.conf` 中 `/clipboard/` 路由和同源入口注入；
- `CLAUDE.md` 中隐私收官提醒。

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
