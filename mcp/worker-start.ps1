[CmdletBinding()]
param([string]$BotId = "gpt")

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$Root = $PSScriptRoot
$Runtime = Join-Path $Root "runtime"
$Logs = Join-Path $Runtime "logs"
$PidFile = Join-Path $Runtime "cmx-worker-$BotId.pid"
$Executable = Join-Path $Root ".venv\Scripts\cmx-worker.exe"

if (-not (Test-Path -LiteralPath $Executable)) {
    throw "Worker executable is missing. Run mcp\install.ps1 first."
}
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

if (Test-Path -LiteralPath $PidFile) {
    $existingPid = 0
    if ([int]::TryParse((Get-Content -LiteralPath $PidFile -Raw).Trim(), [ref]$existingPid)) {
        $existing = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
        if ($existing) {
            $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$existingPid" -ErrorAction SilentlyContinue
            $related = $owner -and (
                $owner.ExecutablePath -eq $Executable `
                -or $owner.CommandLine -like "*cmx_mcp.workers*" `
                -or $owner.Name -eq "cmx-worker.exe"
            )
            if ($related) {
                Write-Host "CMX worker '$BotId' is already running (PID $existingPid)." -ForegroundColor Green
                exit 0
            }
            # PID reuse after a reboot: the recorded PID belongs to a stranger
            # now. Treat the PID file as stale so autostart keeps working.
            Write-Host "Stale PID file: PID $existingPid belongs to an unrelated process; continuing startup." -ForegroundColor Yellow
        }
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

$stamp = Get-Date -Format "yyyyMMdd"
$stdout = Join-Path $Logs "worker-$BotId-$stamp.log"
$stderr = Join-Path $Logs "worker-$BotId-$stamp.error.log"
$env:CMX_MCP_HOME = $Root
$process = Start-Process `
    -FilePath $Executable `
    -ArgumentList @("--bot", $BotId) `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -PassThru `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr
[System.IO.File]::WriteAllText($PidFile, [string]$process.Id)

# The worker has no health URL: a process still alive after the first poll
# attempt is the success signal.
Start-Sleep -Seconds 3
if ($process.HasExited) {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    $tail = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr | Select-Object -Last 5) -join " | " } else { "no error log" }
    throw "CMX worker '$BotId' exited immediately: $tail"
}

Write-Host "CMX worker '$BotId' started (PID $($process.Id)). Logs: $stdout" -ForegroundColor Green
