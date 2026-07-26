[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$Root = $PSScriptRoot
$Runtime = Join-Path $Root "runtime"
$Logs = Join-Path $Runtime "logs"
$PidFile = Join-Path $Runtime "cmx-mcp-http.pid"
$Executable = Join-Path $Root ".venv\Scripts\cmx-mcp-http.exe"
$Port = 8766

if (-not (Test-Path -LiteralPath $Executable)) {
    throw "Remote MCP executable is missing. Run mcp\install.ps1 first."
}
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

if (Test-Path -LiteralPath $PidFile) {
    $existingPid = 0
    if ([int]::TryParse((Get-Content -LiteralPath $PidFile -Raw).Trim(), [ref]$existingPid)) {
        $existing = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
        if ($existing) {
            try {
                $health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/_cmx/mcp-health" -TimeoutSec 3
                if ($health.StatusCode -eq 200) {
                    Write-Host "CMX remote MCP is already running (PID $existingPid)." -ForegroundColor Green
                    exit 0
                }
            } catch {}
            $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$existingPid" -ErrorAction SilentlyContinue
            $related = $owner -and (
                $owner.ExecutablePath -eq $Executable `
                -or $owner.CommandLine -like "*cmx_mcp.remote*" `
                -or $owner.Name -eq "cmx-mcp-http.exe"
            )
            if ($related) {
                throw "PID file points to a running process, but MCP health failed: $existingPid"
            }
            # PID reuse after a reboot: the recorded PID belongs to a stranger
            # now. Treat the PID file as stale so autostart keeps working.
            Write-Host "Stale PID file: PID $existingPid belongs to an unrelated process; continuing startup." -ForegroundColor Yellow
        }
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    throw "TCP port $Port is already used by PID $($listener.OwningProcess)."
}

$stamp = Get-Date -Format "yyyyMMdd"
$stdout = Join-Path $Logs "http-$stamp.log"
$stderr = Join-Path $Logs "http-$stamp.error.log"
$env:CMX_MCP_HOME = $Root
$process = Start-Process `
    -FilePath $Executable `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -PassThru `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr
[System.IO.File]::WriteAllText($PidFile, [string]$process.Id)

$ready = $false
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 500
    if ($process.HasExited) { break }
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/_cmx/mcp-health" -TimeoutSec 2
        if ($response.StatusCode -eq 200) { $ready = $true; break }
    } catch {}
}
if (-not $ready) {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    $tail = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr | Select-Object -Last 5) -join " | " } else { "no error log" }
    throw "CMX remote MCP did not become healthy: $tail"
}

Write-Host "CMX profiled remote MCP started on loopback port $Port (PID $($process.Id))." -ForegroundColor Green
