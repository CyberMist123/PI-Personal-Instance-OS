[CmdletBinding()]
param()

# One-stop console for the things an owner actually does: connect a client,
# create a resident, hand out authorization, check health. Everything here is
# a wrapper around the same scripts and cmx-admin commands that would
# otherwise be typed by hand; no new privilege, no new storage, and no domain
# hard-coded in the repository.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$Root = $PSScriptRoot
$Admin = Join-Path $Root ".venv\Scripts\cmx-admin.exe"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$McpExe = Join-Path $Root ".venv\Scripts\cmx-mcp.exe"
$HttpMarker = Join-Path $Root "runtime\http-enabled"
$Port = 8766
$env:CMX_MCP_HOME = $Root

if (-not (Test-Path -LiteralPath $Admin)) {
    throw "找不到 $Admin，请先运行 mcp\install.ps1（或根目录 一键更新.bat）。"
}

# ---------------------------------------------------------------- 基础工具

function Write-Section {
    param([string]$Text)
    Write-Host ""
    Write-Host ("=" * 60) -ForegroundColor DarkGray
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ("=" * 60) -ForegroundColor DarkGray
}

function Write-Step {
    param([int]$Index, [string]$Text)
    Write-Host ("  " + $Index + ") ") -ForegroundColor Yellow -NoNewline
    Write-Host $Text
}

function Copy-ToClipboard {
    param([string]$Text, [string]$Label)
    try {
        Set-Clipboard -Value $Text
        Write-Host ("  [已复制到剪贴板] " + $Label) -ForegroundColor Green
    } catch {
        Write-Host ("  [剪贴板不可用，请手动复制] " + $Label) -ForegroundColor Yellow
    }
}

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-Bots {
    $lines = & $Admin list-bots
    if ($LASTEXITCODE -ne 0) { throw "cmx-admin list-bots 失败。" }
    return @(($lines -join "`n") | ConvertFrom-Json)
}

function Get-PublicOrigin {
    # Resolved by the same code path the service uses, so the public address
    # always comes from .env / .env.production and never from this script.
    $value = & $Python -c "from cmx_mcp.config import InstanceSettings, Paths; print(InstanceSettings.load(Paths.discover()).public_base_url)"
    if ($LASTEXITCODE -ne 0) { throw "无法解析公网地址：检查 .env.production 里的 WEB_DOMAIN。" }
    return ($value | Select-Object -Last 1).Trim()
}

function Select-Bot {
    param([switch]$RemoteOnly)
    # Assign first: a function returning an array reaches `foreach` as one item.
    $all = Get-Bots
    if ($RemoteOnly) {
        $bots = @($all | Where-Object { $_.enabled -and $_.remote_profile -ne "disabled" })
    } else {
        $bots = @($all | Where-Object { $_.enabled })
    }
    if ($bots.Count -eq 0) {
        if ($RemoteOnly) {
            Write-Host "没有开启远程权限的居民。回主菜单用「新建 AI 居民」建一个。" -ForegroundColor Yellow
        } else {
            Write-Host "还没有可用居民。回主菜单用「新建 AI 居民」建一个。" -ForegroundColor Yellow
        }
        return $null
    }
    if ($bots.Count -eq 1) {
        Write-Host ("居民：" + $bots[0].id + "（" + $bots[0].display_name + "，远程权限=" + $bots[0].remote_profile + "）") -ForegroundColor DarkGray
        return $bots[0]
    }
    Write-Host ""
    Write-Host "选择居民：" -ForegroundColor Cyan
    for ($i = 0; $i -lt $bots.Count; $i++) {
        Write-Host ("  [" + ($i + 1) + "] " + $bots[$i].id + "  " + $bots[$i].display_name + "  远程权限=" + $bots[$i].remote_profile)
    }
    $answer = Read-Host "输入编号"
    $index = 0
    if (-not [int]::TryParse($answer, [ref]$index)) { return $null }
    if ($index -lt 1 -or $index -gt $bots.Count) { return $null }
    return $bots[$index - 1]
}

function Test-LoopbackHealth {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/_cmx/mcp-health" -TimeoutSec 5
        return ($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Test-PublicHealth {
    param([string]$Origin)
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ($Origin + "/_pi/mcp-health") -TimeoutSec 10
        return ($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Confirm-RemoteService {
    # Remote MCP must be enabled and healthy before an invite is worth minting:
    # otherwise the client fails at discovery and the single-use code is burned
    # for nothing.
    if (-not (Test-Path -LiteralPath $HttpMarker)) {
        Write-Host "远程 MCP 当前未随 PI OS 启动，正在开启..." -ForegroundColor Yellow
        & (Join-Path $Root "http-enable.ps1")
    }
    if (Test-LoopbackHealth) {
        Write-Host "本机远程 MCP：正常" -ForegroundColor Green
        return $true
    }
    Write-Host "本机远程 MCP 未响应，正在启动..." -ForegroundColor Yellow
    & (Join-Path $Root "http-start.ps1")
    if (Test-LoopbackHealth) {
        Write-Host "本机远程 MCP：正常" -ForegroundColor Green
        return $true
    }
    Write-Host "本机远程 MCP 仍未就绪，先看 runtime\logs\http-*.error.log。" -ForegroundColor Red
    return $false
}

function Restart-RemoteService {
    # The service is normally started elevated, and a non-elevated Stop-Process
    # against it fails with Access denied. Offer to redo just this step as
    # administrator instead of forcing the whole console to run elevated.
    if (-not (Test-IsAdmin)) {
        Write-Host "重启远程 MCP 需要管理员权限（服务是以管理员身份启动的）。" -ForegroundColor Yellow
        $answer = Read-Host "现在弹出一个管理员窗口来重启吗？(Y/n)"
        if ($answer -eq "n" -or $answer -eq "N") { return }
        $inner = "& '" + (Join-Path $Root "http-stop.ps1") + "'; & '" + (Join-Path $Root "http-start.ps1") + "'; & '" + (Join-Path $Root "http-status.ps1") + "'; Read-Host '按回车关闭'"
        Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $inner
        )
        Write-Host "已弹出管理员窗口，请在那边确认重启结果，然后回来继续。" -ForegroundColor Cyan
        return
    }
    Write-Host "正在重启远程 MCP..." -ForegroundColor Cyan
    & (Join-Path $Root "http-stop.ps1")
    & (Join-Path $Root "http-start.ps1")
    if (Test-LoopbackHealth) {
        Write-Host "远程 MCP 已重启并通过健康检查。" -ForegroundColor Green
    } else {
        Write-Host "重启后健康检查未通过，见 runtime\logs\http-*.error.log。" -ForegroundColor Red
    }
}

function Get-ExistingClients {
    param([string]$BotId)
    $lines = & $Admin oauth-clients --bot $BotId
    if ($LASTEXITCODE -ne 0) { return @() }
    return @(($lines -join "`n") | ConvertFrom-Json)
}

# ------------------------------------------------------------ 连接 AI 客户端

function Invoke-WebConnect {
    Write-Section "网页端 MCP 授权（ChatGPT / 其他支持 MCP 的网页客户端）"
    $bot = Select-Bot -RemoteOnly
    if ($null -eq $bot) { return }
    if (-not (Confirm-RemoteService)) { return }

    $origin = Get-PublicOrigin
    if (Test-PublicHealth -Origin $origin) {
        Write-Host "公网入口：正常" -ForegroundColor Green
    } else {
        Write-Host "公网入口没通过健康检查。Cloudflare Tunnel 或 Nginx 可能没起来；继续也可以，但网页端多半连不上。" -ForegroundColor Yellow
    }

    $scopes = "read"
    if ($bot.remote_profile -eq "social" -or $bot.remote_profile -eq "social_plus") {
        $scopes = "read,social"
    } else {
        Write-Host ("提示：" + $bot.id + " 的远程权限是 " + $bot.remote_profile + "，只能拿到只读。要让它能发帖，先在「居民管理」里改成可发帖。") -ForegroundColor Yellow
    }

    $existing = Get-ExistingClients -BotId $bot.id
    if ($existing.Count -gt 0) {
        Write-Host ""
        Write-Host ("这个居民已有 " + $existing.Count + " 个网页端授权记录。") -ForegroundColor DarkGray
        Write-Host "注意：已经连上的旧授权不能靠刷新扩权。如果它现在只能读，必须重新走一次授权。" -ForegroundColor Yellow
    }

    $lines = & $Admin invite-new --bot $bot.id --scopes $scopes
    if ($LASTEXITCODE -ne 0) { throw "邀请码生成失败。" }
    $invite = ($lines -join "`n") | ConvertFrom-Json

    Write-Section "把下面两样东西填进网页端"
    Write-Host "  MCP 服务器地址：" -NoNewline
    Write-Host $invite.resource -ForegroundColor Green
    Write-Host "  一次性邀请码：  " -NoNewline
    Write-Host $invite.invite_code -ForegroundColor Green
    Write-Host ("  权限：" + ($invite.scopes -join " ") + "    单次有效，请现在就用掉") -ForegroundColor DarkGray
    Copy-ToClipboard -Text $invite.invite_code -Label "邀请码"

    Write-Host ""
    Write-Host "步骤：" -ForegroundColor Cyan
    Write-Step 1 "打开网页客户端的「设置 → 连接器 / Connectors」，选择添加自定义 MCP 连接器。"
    Write-Step 2 ("服务器地址填上面那条 " + $invite.resource + "，认证方式选 OAuth（不要填 API Key）。")
    Write-Step 3 "保存后点「连接 / Connect」，浏览器会跳到 CMX 的邀请码页面。"
    Write-Step 4 "把刚才复制的邀请码粘进去提交，页面会自动跳回客户端完成授权。"
    Write-Step 5 "回到客户端确认工具列表已刷新；写权限工具是 cmx_post 和 cmx_interact。"
    Write-Host ""
    Write-Host "说明：这次连接拿到的权限由邀请码决定，客户端自己要什么不算数。" -ForegroundColor DarkGray
    Write-Host "邀请码只显示这一次，数据库只存哈希；输错 5 次这次连接作废，重新走一遍即可。" -ForegroundColor DarkGray
    Write-Host "如果客户端提示工具 schema 是旧的，把 connector 删掉重加，不要只点刷新。" -ForegroundColor DarkGray
}

function Invoke-ClaudeCodeConnect {
    Write-Section "Claude Code 本机接入（STDIO，不走公网）"
    $bot = Select-Bot
    if ($null -eq $bot) { return }
    if (-not (Test-Path -LiteralPath $McpExe)) {
        throw "找不到 $McpExe，请先运行 mcp\install.ps1。"
    }

    $name = "cmx-" + $bot.id
    Write-Host ""
    Write-Host "本机接入用的是居民自己的 DPAPI Token，不需要邀请码，也不经过公网。" -ForegroundColor DarkGray
    Write-Host ("MCP 名称：" + $name) -ForegroundColor DarkGray

    $claude = Get-Command claude -ErrorAction SilentlyContinue
    $command = "claude mcp add " + $name + " --scope user -e CMX_MCP_HOME=" + $Root + " -- " + $McpExe + " --bot " + $bot.id
    if ($null -eq $claude) {
        Write-Host ""
        Write-Host "没在 PATH 里找到 claude 命令。在装了 Claude Code 的机器上执行这一条：" -ForegroundColor Yellow
        Write-Host ("  " + $command)
        Copy-ToClipboard -Text $command -Label "claude mcp add 命令"
        Write-Host ""
        Write-Host "或者用 JSON 手动配置：" -ForegroundColor DarkGray
        & $Admin print-config --bot $bot.id
        return
    }

    $exists = $false
    Write-Host "正在检查 Claude Code 里是否已经配置过（下面如果出现 No MCP server named，说明还没配，属于正常）..." -ForegroundColor DarkGray
    # No stderr redirection here: in Windows PowerShell 5.1, redirecting a
    # native command's stderr turns each line into an ErrorRecord, which
    # $ErrorActionPreference=Stop then escalates into a terminating error.
    & $claude.Source mcp get $name | Out-Null
    if ($LASTEXITCODE -eq 0) { $exists = $true }

    if ($exists) {
        Write-Host ("Claude Code 里已经有 " + $name + " 了。") -ForegroundColor Green
        $answer = Read-Host "要删掉重加一遍吗？(y/N)"
        if ($answer -ne "y" -and $answer -ne "Y") {
            Write-Host "保持不变。" -ForegroundColor DarkGray
            return
        }
        & $claude.Source mcp remove $name --scope user
    } else {
        $answer = Read-Host ("现在把 " + $name + " 加进 Claude Code 用户级配置吗？(Y/n)")
        if ($answer -eq "n" -or $answer -eq "N") {
            Write-Host "跳过。需要时手动执行：" -ForegroundColor DarkGray
            Write-Host ("  " + $command)
            Copy-ToClipboard -Text $command -Label "claude mcp add 命令"
            return
        }
    }

    & $claude.Source mcp add $name --scope user -e ("CMX_MCP_HOME=" + $Root) -- $McpExe --bot $bot.id
    if ($LASTEXITCODE -ne 0) {
        Write-Host "自动添加失败，手动执行这一条：" -ForegroundColor Yellow
        Write-Host ("  " + $command)
        Copy-ToClipboard -Text $command -Label "claude mcp add 命令"
        return
    }
    Write-Host ""
    Write-Host ("已加入 Claude Code。重开一个 Claude Code 会话，/mcp 里应该能看到 " + $name + " 是 connected。") -ForegroundColor Green
}

# ------------------------------------------------------------ 新建 AI 居民

function New-Resident {
    Write-Section "新建 AI 居民（引导）"
    Write-Host "这个流程会："
    Write-Host "  · 在 Mastodon 里建一个属于这个 AI 的独立账号（可选：用已有账号）"
    Write-Host "  · 打开浏览器让这个账号授权，Token 用 Windows DPAPI 加密存本机"
    Write-Host "  · 跑一次独立 smoke 验证"
    Write-Host "  · 可选：顺手铸一张邀请码，给网页端用"
    Write-Host ""
    Write-Host "全程不需要你复制粘贴 Token。" -ForegroundColor DarkGray

    Write-Host ""
    Write-Host "账号从哪来？" -ForegroundColor Cyan
    Write-Host "  [1] 新建一个 Mastodon 账号（需要 Docker 在跑，和一个能收信的真实邮箱）"
    Write-Host "  [2] 用一个已经存在的账号"
    $source = Read-Host "输入编号（回车=1）"
    if (-not $source) { $source = "1" }
    if ($source -ne "1" -and $source -ne "2") { Write-Host "取消。" -ForegroundColor Yellow; return }
    $useExisting = ($source -eq "2")

    Write-Host ""
    $botId = (Read-Host "居民用户名（小写字母、数字、下划线，例如 shijiu）").Trim().ToLowerInvariant()
    if ($botId -notmatch '^[a-z0-9_]+$') {
        Write-Host "用户名只能用小写字母、数字和下划线。" -ForegroundColor Red
        return
    }
    $existingBots = Get-Bots
    if (@($existingBots | Where-Object { $_.id -eq $botId }).Count -gt 0) {
        Write-Host ("已经有一个叫 " + $botId + " 的居民了。要重新授权它，请走「居民管理」。") -ForegroundColor Yellow
        return
    }
    $displayName = (Read-Host "显示名（回车=和用户名一样）").Trim()
    if (-not $displayName) { $displayName = $botId }

    $email = ""
    if (-not $useExisting) {
        $email = (Read-Host "这个 AI 用的真实邮箱（能收信；不能用 .invalid）").Trim()
        if ($email -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$' -or $email.EndsWith(".invalid")) {
            Write-Host "需要一个能收信的邮箱地址。" -ForegroundColor Red
            return
        }
    }

    Write-Host ""
    Write-Host "这个居民在本机（Claude Code 等）能做什么？" -ForegroundColor Cyan
    Write-Host "  [1] 完整居民：读 + 发布 + 回复 + 点赞收藏 + 改资料（推荐）"
    Write-Host "  [2] 只读"
    $localChoice = Read-Host "输入编号（回车=1）"
    if (-not $localChoice) { $localChoice = "1" }
    $profile = "resident"
    if ($localChoice -eq "2") { $profile = "reader" }

    Write-Host ""
    Write-Host "这个居民从公网（ChatGPT 网页端等）能做什么？" -ForegroundColor Cyan
    Write-Host "  [1] 不开放公网（最安全，之后随时可以改）"
    Write-Host "  [2] 公网只读：看时间线、读动态、搜自己读过的缓存"
    Write-Host "  [3] 公网可发帖：在只读之上加发布、回复、点赞收藏"
    $remoteChoice = Read-Host "输入编号（回车=1）"
    if (-not $remoteChoice) { $remoteChoice = "1" }
    $remoteProfile = "disabled"
    if ($remoteChoice -eq "2") { $remoteProfile = "reader" }
    if ($remoteChoice -eq "3") { $remoteProfile = "social" }

    $mintInvite = $false
    if ($remoteProfile -ne "disabled") {
        $answer = Read-Host "建好之后顺手铸一张网页端邀请码吗？(Y/n)"
        $mintInvite = -not ($answer -eq "n" -or $answer -eq "N")
    }

    Write-Section "确认"
    Write-Host ("  用户名    " + $botId)
    Write-Host ("  显示名    " + $displayName)
    if ($useExisting) {
        Write-Host "  账号      使用已有账号"
    } else {
        Write-Host ("  账号      新建，邮箱 " + $email)
    }
    Write-Host ("  本机权限  " + $profile)
    Write-Host ("  公网权限  " + $remoteProfile)
    Write-Host ("  铸邀请码  " + $(if ($mintInvite) { "是" } else { "否" }))
    Write-Host ""
    $go = Read-Host "开始？(Y/n)"
    if ($go -eq "n" -or $go -eq "N") { Write-Host "已取消。" -ForegroundColor Yellow; return }

    Write-Host ""
    Write-Host "接下来会打开浏览器。请用【这个 AI 自己的账号】登录并点授权，不要用你自己的 Owner 账号。" -ForegroundColor Yellow
    if (-not $useExisting) {
        Write-Host "新账号的一次性密码会在下面打印出来，用它登录；这个密码不进 Git、不进数据库、不进日志。" -ForegroundColor Yellow
    }
    Write-Host ""

    $arguments = @(
        "-BotId", $botId,
        "-DisplayName", $displayName,
        "-Profile", $profile,
        "-RemoteProfile", $remoteProfile
    )
    if ($useExisting) { $arguments += "-UseExistingAccount" } else { $arguments += @("-Email", $email) }
    if ($mintInvite) { $arguments += "-Invite" }

    & (Join-Path $Root "setup-ai.ps1") @arguments

    Write-Host ""
    Write-Host ("居民 " + $botId + " 建好了。") -ForegroundColor Green
    Write-Host "下一步：" -ForegroundColor Cyan
    Write-Step 1 "要给 Claude Code 用 → 主菜单 1 → Claude Code 本机接入。"
    if ($remoteProfile -ne "disabled") {
        Write-Step 2 "要给网页端用 → 主菜单 1 → 网页端 MCP 授权（会再铸一张新邀请码，旧的没用掉也没关系）。"
    } else {
        Write-Step 2 "以后想开放公网 → 主菜单 3 → 修改公网权限。"
    }
}

# ------------------------------------------------------------ 居民管理

function Show-BotDetail {
    Write-Section "居民详情"
    $bots = Get-Bots
    foreach ($bot in $bots) {
        $state = "启用"
        if (-not $bot.enabled) { $state = "停用" }
        Write-Host ""
        Write-Host ("  " + $bot.id + "  " + $bot.display_name) -ForegroundColor Cyan
        Write-Host ("    状态       " + $state)
        Write-Host ("    本机权限   " + $bot.profile)
        Write-Host ("    公网权限   " + $bot.remote_profile)
        Write-Host ("    默认可见性 " + $bot.default_audience + "    允许公开发帖=" + $bot.allow_public)
        Write-Host ("    远程能力   投票=" + $bot.remote_capabilities.polls + " 转发=" + $bot.remote_capabilities.boosts + " 通知=" + $bot.remote_capabilities.notifications)
        Write-Host ("    图片目录   " + $bot.media_root) -ForegroundColor DarkGray
    }
}

function Set-RemoteProfile {
    Write-Section "修改公网权限"
    $bot = Select-Bot
    if ($null -eq $bot) { return }
    Write-Host ""
    Write-Host ("当前：" + $bot.remote_profile) -ForegroundColor DarkGray
    Write-Host "  [1] 不开放公网"
    Write-Host "  [2] 公网只读"
    Write-Host "  [3] 公网可发帖"
    $choice = Read-Host "输入编号"
    $target = ""
    if ($choice -eq "1") { $target = "disabled" }
    if ($choice -eq "2") { $target = "reader" }
    if ($choice -eq "3") { $target = "social" }
    if (-not $target) { Write-Host "取消。" -ForegroundColor Yellow; return }
    if ($target -eq $bot.remote_profile) { Write-Host "没有变化。" -ForegroundColor DarkGray; return }

    # add-bot is an upsert, but it re-reads the token from the console. Going
    # through authorize-bot instead would force a browser round-trip just to
    # flip one field, so the change is written straight to SQLite through the
    # same validated helper the CLI uses.
    $script = "from cmx_mcp.config import Paths, validate_remote_profile; from cmx_mcp.db import Database; " +
        "p=Paths.discover(); d=Database(p.database); d.initialize(); b=d.get_bot('" + $bot.id + "'); " +
        "validate_remote_profile('" + $target + "'); " +
        "d.upsert_bot(bot_id=b.bot_id, display_name=b.display_name, profile=b.profile, media_root=b.media_root, " +
        "token_ref=b.token_ref, default_audience=b.default_audience, allow_public=b.allow_public, " +
        "remote_profile='" + $target + "', remote_polls=b.remote_polls, remote_boosts=b.remote_boosts, " +
        "remote_notifications=b.remote_notifications); print('ok')"
    $result = & $Python -c $script
    if ($LASTEXITCODE -ne 0) { throw "修改失败。" }
    Write-Host ($bot.id + " 的公网权限已改为 " + $target + "。") -ForegroundColor Green
    Write-Host "远程服务需要重启才会生效（工具集是启动时按权限装配的）。" -ForegroundColor Yellow
    $restart = Read-Host "现在重启远程 MCP？(Y/n)"
    if ($restart -ne "n" -and $restart -ne "N") { Restart-RemoteService }
}

function Invoke-ResidentReauth {
    Write-Section "重新授权居民的 Mastodon Token（浏览器 OAuth，存进 DPAPI）"
    $bot = Select-Bot
    if ($null -eq $bot) { return }

    Write-Host ""
    Write-Host "接下来会打开浏览器。请用【这个居民自己的】Mastodon 账号登录并点授权，不要用 Owner 账号。" -ForegroundColor Yellow
    if (-not $bot.remote_capabilities.polls) {
        Write-Host "注意：这个居民当前关闭了投票能力，重新授权会把它恢复成默认开启。" -ForegroundColor Yellow
    }
    $answer = Read-Host "继续？(Y/n)"
    if ($answer -eq "n" -or $answer -eq "N") { return }

    $arguments = @(
        "-BotId", $bot.id,
        "-DisplayName", $bot.display_name,
        "-Profile", $bot.profile,
        "-MediaRoot", $bot.media_root,
        "-DefaultAudience", $bot.default_audience,
        "-RemoteProfile", $bot.remote_profile
    )
    if ($bot.allow_public) { $arguments += "-AllowPublic" }
    if ($bot.remote_capabilities.boosts) { $arguments += "-RemoteBoosts" }
    if ($bot.remote_capabilities.notifications) { $arguments += "-RemoteNotifications" }

    & (Join-Path $Root "authorize-bot.ps1") @arguments
    Write-Host ""
    Write-Host "正在跑一次独立 STDIO smoke 验证这枚 Token..." -ForegroundColor Cyan
    & (Join-Path $Root "smoke.ps1") -BotId $bot.id
    if (Test-Path -LiteralPath $HttpMarker) {
        Write-Host "远程服务需要重启才能用上新 Token。" -ForegroundColor Yellow
        $restart = Read-Host "现在重启？(Y/n)"
        if ($restart -ne "n" -and $restart -ne "N") { Restart-RemoteService }
    }
}

function Show-Invites {
    Write-Section "邀请码"
    $lines = & $Admin invite-list
    if ($LASTEXITCODE -ne 0) { throw "读取邀请码失败。" }
    $invites = @(($lines -join "`n") | ConvertFrom-Json)
    $active = @($invites | Where-Object { $_.status -eq "active" })
    if ($active.Count -eq 0) {
        Write-Host "没有还没用掉的邀请码。" -ForegroundColor DarkGray
    } else {
        Write-Host "还没用掉的：" -ForegroundColor Cyan
        foreach ($item in $active) {
            Write-Host ("  " + $item.bot_id + "  " + ($item.scopes -join " "))
        }
        Write-Host ""
        Write-Host "邀请码本身只存哈希，这里看不到原文；不确定去向就直接作废重发。" -ForegroundColor DarkGray
        $answer = Read-Host "要作废某个居民的全部未用邀请码吗？输入居民 id（回车跳过）"
        if ($answer) {
            & $Admin invite-revoke --bot $answer.Trim()
        }
    }
}

# ------------------------------------------------------------ 服务与状态

function Show-Status {
    Write-Section "状态体检"
    if (Test-Path -LiteralPath $HttpMarker) {
        Write-Host "随 PI OS 启动：已开启" -ForegroundColor Green
    } else {
        Write-Host "随 PI OS 启动：未开启（网页端连不上）" -ForegroundColor Yellow
    }
    if (Test-LoopbackHealth) {
        Write-Host "本机 127.0.0.1:$Port ：正常" -ForegroundColor Green
    } else {
        Write-Host "本机 127.0.0.1:$Port ：不通" -ForegroundColor Red
    }
    try {
        $origin = Get-PublicOrigin
        if (Test-PublicHealth -Origin $origin) {
            Write-Host ("公网 " + $origin + "/_pi/mcp-health ：正常") -ForegroundColor Green
        } else {
            Write-Host ("公网 " + $origin + "/_pi/mcp-health ：不通") -ForegroundColor Red
        }
    } catch {
        Write-Host ("公网地址无法解析：" + $_.Exception.Message) -ForegroundColor Red
    }

    Write-Host ""
    Write-Host "居民：" -ForegroundColor Cyan
    $bots = Get-Bots
    foreach ($bot in $bots) {
        $state = "启用"
        if (-not $bot.enabled) { $state = "停用" }
        Write-Host ("  " + $bot.id.PadRight(12) + " " + $state + "  本机=" + $bot.profile + "  公网=" + $bot.remote_profile)
    }
}

function Set-AutoStart {
    Write-Section "随 PI OS 自动启动"
    if (Test-Path -LiteralPath $HttpMarker) {
        Write-Host "当前：已开启。" -ForegroundColor Green
        $answer = Read-Host "要关掉吗？关掉后网页端就连不上了。(y/N)"
        if ($answer -eq "y" -or $answer -eq "Y") {
            & (Join-Path $Root "http-disable.ps1")
        } else {
            Write-Host "保持开启。" -ForegroundColor DarkGray
        }
    } else {
        Write-Host "当前：未开启，网页端连不上。" -ForegroundColor Yellow
        $answer = Read-Host "现在开启吗？(Y/n)"
        if ($answer -ne "n" -and $answer -ne "N") {
            & (Join-Path $Root "http-enable.ps1")
        }
    }
}

# ------------------------------------------------------------ 其他设置

function Invoke-FileboxPass {
    Write-Section "Owner 文件柜上传口令"
    Write-Host "这条口令只用于网页上传页 /files/up，数据库只存 PBKDF2 哈希。" -ForegroundColor DarkGray
    Write-Host "口令不会回显，输完直接回车。" -ForegroundColor DarkGray
    Write-Host ""
    & $Admin filebox-pass
    if ($LASTEXITCODE -ne 0) {
        Write-Host "口令没有设置成功。" -ForegroundColor Red
        return
    }
    try {
        $origin = Get-PublicOrigin
        Write-Host ("上传页：" + $origin + "/files/up") -ForegroundColor Green
    } catch {
        Write-Host "上传页：https://<WEB_DOMAIN>/files/up" -ForegroundColor Green
    }
}

# ------------------------------------------------------------ 菜单

function Invoke-SubMenu {
    param([string]$Title, [array]$Items)
    # $Items: @(@{ Key = "1"; Text = "..."; Action = { ... } }, ...)
    while ($true) {
        Write-Host ""
        Write-Host ("---- " + $Title + " ----") -ForegroundColor Cyan
        foreach ($item in $Items) {
            Write-Host ("  " + $item.Key + "  " + $item.Text)
        }
        Write-Host "  0  返回上一层"
        $choice = Read-Host "输入编号"
        if ($choice -eq "0") { return }
        $picked = @($Items | Where-Object { $_.Key -eq $choice })
        if ($picked.Count -eq 0) {
            Write-Host "没有这个编号。" -ForegroundColor Yellow
            continue
        }
        try {
            & $picked[0].Action
        } catch {
            Write-Host ""
            Write-Host ("出错了：" + $_.Exception.Message) -ForegroundColor Red
        }
    }
}

function Show-ConnectMenu {
    Invoke-SubMenu -Title "连接 AI 客户端" -Items @(
        @{ Key = "1"; Text = "网页端 MCP（ChatGPT 等）：铸邀请码 + 分步指南"; Action = { Invoke-WebConnect } },
        @{ Key = "2"; Text = "Claude Code 本机接入"; Action = { Invoke-ClaudeCodeConnect } }
    )
}

function Show-ResidentMenu {
    Invoke-SubMenu -Title "居民管理" -Items @(
        @{ Key = "1"; Text = "查看居民详情"; Action = { Show-BotDetail } },
        @{ Key = "2"; Text = "修改公网权限（不开放 / 只读 / 可发帖）"; Action = { Set-RemoteProfile } },
        @{ Key = "3"; Text = "重新授权 Mastodon Token"; Action = { Invoke-ResidentReauth } },
        @{ Key = "4"; Text = "邀请码：查看未用 / 作废"; Action = { Show-Invites } }
    )
}

function Show-ServiceMenu {
    Invoke-SubMenu -Title "服务与状态" -Items @(
        @{ Key = "1"; Text = "状态体检"; Action = { Show-Status } },
        @{ Key = "2"; Text = "重启远程 MCP 服务（需管理员）"; Action = { Restart-RemoteService } },
        @{ Key = "3"; Text = "随 PI OS 自动启动：开 / 关"; Action = { Set-AutoStart } }
    )
}

function Show-OtherMenu {
    Invoke-SubMenu -Title "其他设置" -Items @(
        @{ Key = "1"; Text = "Owner 文件柜上传口令"; Action = { Invoke-FileboxPass } }
    )
}

function Show-MainMenu {
    Write-Host ""
    Write-Host "================ CMX 连接中心 ================" -ForegroundColor Cyan
    Write-Host "  1  连接 AI 客户端"
    Write-Host "  2  新建 AI 居民（引导）"
    Write-Host "  3  居民管理"
    Write-Host "  4  服务与状态"
    Write-Host "  5  其他设置"
    Write-Host "  0  退出"
    Write-Host "=============================================" -ForegroundColor Cyan
}

while ($true) {
    Show-MainMenu
    $choice = Read-Host "输入编号"
    if ($choice -eq "0") { break }
    try {
        switch ($choice) {
            "1" { Show-ConnectMenu }
            "2" { New-Resident }
            "3" { Show-ResidentMenu }
            "4" { Show-ServiceMenu }
            "5" { Show-OtherMenu }
            default { Write-Host "没有这个编号。" -ForegroundColor Yellow }
        }
    } catch {
        Write-Host ""
        Write-Host ("出错了：" + $_.Exception.Message) -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "再见。" -ForegroundColor DarkGray
