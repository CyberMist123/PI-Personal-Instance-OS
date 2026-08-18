[CmdletBinding()]
param()

# CMX loopback MCP (127.0.0.1:8766) liveness watchdog.
# Probes health; only starts the service when the probe fails, so a healthy
# instance is never disturbed (http-start.ps1 would throw "port already in use"
# if called while the server is up but the pid file is stale). Registered as a
# per-user Scheduled Task repeating every few minutes. Silent, non-admin.

$ErrorActionPreference = "SilentlyContinue"
$Root = $PSScriptRoot
$Start = Join-Path $Root "http-start.ps1"
$Port = 8766

try {
    $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/_cmx/mcp-health" -TimeoutSec 5
    if ($r.StatusCode -eq 200) { exit 0 }
} catch {}

# Unhealthy or unreachable -> (re)start. http-start.ps1 is idempotent and logs
# its own outcome under runtime\logs\.
& powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $Start
exit $LASTEXITCODE
