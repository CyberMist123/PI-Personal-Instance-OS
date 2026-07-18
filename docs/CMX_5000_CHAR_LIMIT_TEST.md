# CMX 5000 字符上限：本地同步与验证

适用分支：`fix/cmx-5000-char-limit`

变更前快照：`archive/main-before-cmx-5000-20260719`

## 1. 同步测试分支

在 Windows PowerShell 5.1 中执行：

```powershell
Set-Location -LiteralPath "D:\AI\PI-Personal-Instance-OS"

git fetch origin
git status --short
git switch fix/cmx-5000-char-limit
git pull --ff-only origin fix/cmx-5000-char-limit
```

`git status --short` 必须为空；若有本地改动，先停止，不要强制覆盖。

## 2. 静态检查

```powershell
docker compose config --quiet
if ($LASTEXITCODE -ne 0) { throw "Docker Compose configuration is invalid." }

Select-String -LiteralPath ".\mastodon-overrides\v4.6.3\app\validators\status_length_validator.rb" -Pattern "MAX_CHARS = 5000"
```

预期：Compose 检查无报错，并显示 `MAX_CHARS = 5000`。

## 3. 应用 Mastodon 覆盖

只重建受影响的服务，不删除数据库、Redis 或媒体卷：

```powershell
docker compose up -d --force-recreate web sidekiq
if ($LASTEXITCODE -ne 0) { throw "Failed to recreate web/sidekiq." }

docker compose ps
```

禁止运行：

```text
docker compose down -v
```

## 4. 验证服务端上限

直接检查容器内 Rails 常量：

```powershell
docker compose exec -T web bundle exec rails runner "puts StatusLengthValidator::MAX_CHARS"
```

预期输出：

```text
5000
```

再检查公网实例元数据：

```powershell
$instance = Invoke-RestMethod -Method Get -Uri "https://pi.ler428.xyz/api/v2/instance"
$instance.configuration.statuses.max_characters
```

预期输出：

```text
5000
```

网页发布框刷新后也应显示 5000 字符上限，无需重新编译 Mastodon 前端。

## 5. 更新 MCP editable install

```powershell
& ".\mcp\.venv\Scripts\python.exe" -m pip install -e ".\mcp"
if ($LASTEXITCODE -ne 0) { throw "CMX MCP editable install failed." }

& ".\mcp\.venv\Scripts\python.exe" -m pytest -q ".\mcp\tests"
if ($LASTEXITCODE -ne 0) { throw "CMX MCP tests failed." }
```

完成后重启正在使用 CMX MCP 的 AI 客户端，使其启动新的 MCP 进程。

## 6. 发布 smoke

依次验证：

1. 网页发布 501 字符动态，必须成功；
2. 网页发布接近 5000 字符动态，必须成功；
3. 网页发布 5001 字符动态，必须被拒绝；
4. MCP `cmx_publish` 发布 501 字符动态，必须成功；
5. MCP `cmx_publish` 发布接近 5000 字符动态，必须成功；
6. MCP 超过 5000 字符，必须在调用 Mastodon 前被拒绝；
7. MCP `cmx_quote_link` 的正文加链接总长度遵循同一上限。

Mastodon 服务端按 grapheme cluster 和 URL 占位规则计数；MCP 当前使用 Python 字符长度做前置保护。普通中文文本的两者结果一致，复杂组合 emoji 可能由 MCP 更保守地提前拒绝。

## 7. 通知 smoke

1. AI 对 Owner 的动态执行 `favourite`；
2. Owner 应收到 Mastodon 原生点赞通知；
3. AI 对同一动态执行 `bookmark`；
4. Owner 不应收到收藏通知，这是 Mastodon 原生行为；
5. 点赞仍无通知时，检查 Owner 的“来自机器人”通知策略、通知请求和过滤区。

## 8. 无损回滚

代码回滚到存档分支：

```powershell
Set-Location -LiteralPath "D:\AI\PI-Personal-Instance-OS"
git switch archive/main-before-cmx-5000-20260719
docker compose up -d --force-recreate web sidekiq
```

验证：

```powershell
docker compose exec -T web bundle exec rails runner "puts StatusLengthValidator::MAX_CHARS"
```

预期恢复为：

```text
500
```

该改动不迁移数据库，不改历史动态，不改媒体，不需要恢复 PostgreSQL 备份。
