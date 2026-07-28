# Clip Brain Claude Code 只读验收单

## 身份与边界

你是独立审查者，不是实现者。目标分支为 `demo/clip-brain-v0`。

禁止：

- 修改任何文件；
- 自动修复、格式化或升级依赖；
- commit、push、开 PR、部署或合并；
- 修改 Mastodon、MCP、Nginx、compose、生产配置或运行数据；
- 删除 Owner 的 IndexedDB 内容；
- 把真实私人域名写入新文档或测试输出。

发现问题只报告，不施工。

## Windows 目标目录

```text
D:\AI\PI-OS-clip-demo
```

## 第 1 阶段：安全更新 worktree

```powershell
Set-Location "D:\AI\PI-OS-clip-demo"
git status --short --branch
```

- 如果工作区不干净，立即停止并报告，不覆盖任何本地改动。
- 如果干净，继续：

```powershell
git fetch origin
git switch --detach origin/demo/clip-brain-v0
git rev-parse HEAD
git status --short --branch
```

## 第 2 阶段：自动测试

```powershell
py -3 -m unittest discover -s demos\clip-brain\tests -p "test_*.py" -v
```

必须逐项确认：

- 所有前端文件少于 300 行；
- `storage.js`、`archive.js`、`view.js`、`downloads.js`、`bulk.js`、`app.js` 语法通过；
- HTML 存在 Mastodon / Clipboard 双入口；
- 未选择时批量动作隐藏；选择后只出现两个动作：自适应复制/下载、全部焚毁；
- CSS 没有 transition 或 animation；
- 测试生成的 ZIP 能被 Python 正常读取，CRC 与内容一致；
- 危险文件名不会形成 ZIP 路径逃逸；
- 选中内容等于 1 GiB 时严格拒绝；
- 纯文本选择识别为复制模式；含文件选择识别为下载模式；
- 批量焚毁只把明确选择的 ID 一次性交给 `removeMany`。

## 第 3 阶段：静态页面 smoke

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
$response.Content | Select-String 'bulk-actions|bulk-action|bulk-destroy|downloads.js|bulk.js'
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

确认改动仅限：

- `demos/clip-brain/`
- `docs/clip-brain/`
- `CLAUDE.md` 中隐私收官提醒

## 输出格式

报告必须包含：

1. 审查 HEAD；
2. 自动测试通过/失败数量与原始失败名；
3. 页面 HTTP smoke；
4. 每个前端文件行数；
5. diff 范围；
6. 是否发现阻断问题；
7. 明确写明“未编辑、未提交、未部署、未开 PR”。

浏览器真实点击、文件选择器、复制权限、两个标签页实时同步和 Windows 解压体验仍由 Owner 人工验收，不得冒充已经验证。
