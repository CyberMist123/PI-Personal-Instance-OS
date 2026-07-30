[CmdletBinding()]
param()

# One-stop connection console: pick a resident, get it authorized, and print
# the exact client-side steps. Everything here is a wrapper around the same
# scripts and cmx-admin commands the owner would otherwise type by hand; no
# new privilege, no new storage, no domain hard-coded in the repository.

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
    $bots = Get-Bots
    if ($RemoteOnly) {
        $bots = @($bots | Where-Object { $_.enabled -and $_.remote_profile -ne "disabled" })
    } else {
        $bots = @($bots | Where-Object { $_.enabled })
    }
    if ($bots.Count -eq 0) {
        if ($RemoteOnly) {
            Write-Host "没有开启远程 profile 的居民。先用 一键新居民.bat 建一个，或把 remote_profile 改成 reader/social。" -ForegroundColor Yellow
        } else {
            Write-Host "还没有可用居民。先运行根目录 一键新居民.bat。" -ForegroundColor Yellow
        }
        return $null
    }
    if ($bots.Count -eq 1) {
        Write-Host ("居民：" + $bots[0].id + "（" + $bots[0].display_name + "，remote_profile=" + $bots[0].remote_profile + "）") -ForegroundColor DarkGray
        return $bots[0]
    }
    Write-Host ""
    Write-Host "选择居民：" -ForegroundColor Cyan
    for ($i = 0; $i -lt $bots.Count; $i++) {
        Write-Host ("  [" + ($i + 1) + "] " + $bots[$i].id + "  " + $bots[$i].display_name + "  remote_profile=" + $bots[$i].remote_profile)
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
    # Remote MCP must be enabled (starts with PI OS) and healthy before an
    # invite is worth minting: otherwise the client fails at discovery and the
    # single-use code is burned for nothing.
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
        Write-Host ("提示：" + $bot.id + " 的 remote_profile 是 " + $bot.remote_profile + "，只能拿到只读权限。要让它能发帖，先改成 social。") -ForegroundColor Yellow
    }

    $existing = Get-ExistingClients -BotId $bot.id
    if ($existing.Count -gt 0) {
        Write-Host ""
        Write-Host ("这个居民已有 " + $existing.Count + " 个网页端授权记录。") -ForegroundColor DarkGray
        Write-Host "注意：已连上的旧 connector 无法通过刷新扩权。如果它现在只能读，必须在网页端删掉再重新添加。" -ForegroundColor Yellow
    }

    $lines = & $Admin invite-new --bot $bot.id --scopes $scopes
    if ($LASTEXITCODE -ne 0) { throw "邀请码生成失败。" }
    $invite = ($lines -join "`n") | ConvertFrom-Json

    Write-Section "把下面两样东西填进网页端"
    Write-Host "  MCP 服务器地址：" -NoNewline
    Write-Host $invite.resource -ForegroundColor Green
    Write-Host "  一次性邀请码：  " -NoNewline
    Write-Host $invite.invite_code -ForegroundColor Green
    Write-Host ("  权限：" + ($invite.scopes -join " ") + "    有效期：" + $invite.expires_in_hours + " 小时，单次有效") -ForegroundColor DarkGray
    Copy-ToClipboard -Text $invite.invite_code -Label "邀请码"

    Write-Host ""
    Write-Host "步骤：" -ForegroundColor Cyan
    Write-Step 1 "打开网页客户端的「设置 → 连接器 / Connectors」，选择添加自定义 MCP 连接器。"
    Write-Step 2 ("服务器地址填上面那条 " + $invite.resource + "，认证方式选 OAuth（不要填 API Key）。")
    Write-Step 3 "保存后点「连接 / Connect」，浏览器会跳到 CMX 的邀请码页面。"
    Write-Step 4 "把刚才复制的邀请码粘进去提交，页面会自动跳回客户端完成授权。"
    Write-Step 5 "回到客户端确认工具列表已刷新；写权限工具是 cmx_post 和 cmx_interact。"
    Write-Host ""
    Write-Host "说明：邀请码只显示这一次，数据库只存哈希；输错 5 次这次连接作废，重新跑本菜单即可。" -ForegroundColor DarkGray
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

function Invoke-ResidentReauth {
    Write-Section "重新授权居民的 Mastodon Token（浏览器 OAuth，存进 DPAPI）"
    $bot = Select-Bot
    if ($null -eq $bot) { return }

    Write-Host ""
    Write-Host "接下来会打开浏览器。请用「这个居民自己的」Mastodon 账号登录并点授权，不要用 Owner 账号。" -ForegroundColor Yellow
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
    # Assign first: a function returning an array reaches `foreach` as one item.
    $bots = Get-Bots
    foreach ($bot in $bots) {
        $state = "启用"
        if (-not $bot.enabled) { $state = "停用" }
        Write-Host ("  " + $bot.id.PadRight(12) + " " + $state + "  本机 profile=" + $bot.profile + "  远程 profile=" + $bot.remote_profile)
    }

    Write-Host ""
    Write-Host "未使用的邀请码：" -ForegroundColor Cyan
    $lines = & $Admin invite-list
    if ($LASTEXITCODE -eq 0) {
        $invites = @(($lines -join "`n") | ConvertFrom-Json)
        $active = @($invites | Where-Object { $_.status -eq "active" })
        if ($active.Count -eq 0) {
            Write-Host "  （没有，需要时从菜单 1 生成）" -ForegroundColor DarkGray
        } else {
            foreach ($item in $active) {
                Write-Host ("  " + $item.bot_id + "  " + ($item.scopes -join " "))
            }
        }
    }
}

function Show-Menu {
    Write-Host ""
    Write-Host "================ CMX 连接中心 ================" -ForegroundColor Cyan
    Write-Host "  1  网页端 MCP 授权（ChatGPT 等）—— 生成邀请码 + 分步指南"
    Write-Host "  2  Claude Code 本机接入"
    Write-Host "  3  设置 Owner 文件柜上传口令"
    Write-Host "  4  重新授权居民的 Mastodon Token"
    Write-Host "  5  状态体检"
    Write-Host "  6  重启远程 MCP 服务"
    Write-Host "  0  退出"
    Write-Host "=============================================" -ForegroundColor Cyan
}

while ($true) {
    Show-Menu
    $choice = Read-Host "输入编号"
    try {
        switch ($choice) {
            "1" { Invoke-WebConnect }
            "2" { Invoke-ClaudeCodeConnect }
            "3" { Invoke-FileboxPass }
            "4" { Invoke-ResidentReauth }
            "5" { Show-Status }
            "6" { Restart-RemoteService }
            "0" { break }
            default { Write-Host "没有这个编号。" -ForegroundColor Yellow }
        }
    } catch {
        Write-Host ""
        Write-Host ("出错了：" + $_.Exception.Message) -ForegroundColor Red
    }
    if ($choice -eq "0") { break }
}

Write-Host ""
Write-Host "再见。" -ForegroundColor DarkGray
