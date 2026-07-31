[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot

$failures = New-Object System.Collections.Generic.List[string]
$identityDomainExpected = "pi.invalid"

function Get-EnvValue {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][string]$Key
  )
  if (-not (Test-Path -LiteralPath $Path)) { return $null }
  $escaped = [regex]::Escape($Key)
  $line = Get-Content -LiteralPath $Path | Where-Object { $_ -match "^$escaped=" } | Select-Object -Last 1
  if ($null -eq $line) { return $null }
  return ($line -replace "^$escaped=", "")
}

& docker info *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Docker Desktop: FAIL" -ForegroundColor Red
  exit 1
}

Write-Host "=== Domain configuration ===" -ForegroundColor Cyan
$identityDomain = Get-EnvValue -Path ".env.production" -Key "LOCAL_DOMAIN"
$webDomain = Get-EnvValue -Path ".env.production" -Key "WEB_DOMAIN"
$streamingBase = Get-EnvValue -Path ".env.production" -Key "STREAMING_API_BASE_URL"
$alternateDomains = Get-EnvValue -Path ".env.production" -Key "ALTERNATE_DOMAINS"

if ($identityDomain -eq $identityDomainExpected) {
  Write-Host "Permanent identity: $identityDomain (OK)" -ForegroundColor Green
} else {
  Write-Host "Permanent identity: FAIL - expected $identityDomainExpected, found $identityDomain" -ForegroundColor Red
  $failures.Add("LOCAL_DOMAIN is not fixed to pi.invalid")
}

if (-not [string]::IsNullOrWhiteSpace($webDomain) -and $webDomain -ne $identityDomainExpected) {
  Write-Host "Current web entrance: https://$webDomain" -ForegroundColor Green
} else {
  Write-Host "Current web entrance: FAIL - WEB_DOMAIN is missing or invalid" -ForegroundColor Red
  $failures.Add("WEB_DOMAIN is missing or invalid")
}

if ($streamingBase -eq "wss://$webDomain") {
  Write-Host "Streaming base: $streamingBase (OK)" -ForegroundColor Green
} else {
  Write-Host "Streaming base: FAIL - expected wss://$webDomain, found $streamingBase" -ForegroundColor Red
  $failures.Add("STREAMING_API_BASE_URL does not match WEB_DOMAIN")
}

if ([string]::IsNullOrWhiteSpace($alternateDomains)) {
  Write-Host "Alternate domains: none" -ForegroundColor Green
} else {
  Write-Host "Alternate domains (transition only): $alternateDomains" -ForegroundColor Yellow
}

Write-Host "`n=== Containers ===" -ForegroundColor Cyan
& docker compose --profile tunnel ps
if ($LASTEXITCODE -ne 0) {
  $failures.Add("docker compose ps failed")
}

Write-Host "`n=== Local health ===" -ForegroundColor Cyan
try {
  $local = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8080/_pi/health" -TimeoutSec 5
  if ($local.StatusCode -ne 200) { throw "HTTP $($local.StatusCode)" }
  Write-Host "Nginx: OK" -ForegroundColor Green
} catch {
  Write-Host "Nginx: FAIL - $($_.Exception.Message)" -ForegroundColor Red
  $failures.Add("local nginx health failed")
}

$mcpEnabled = Test-Path -LiteralPath ".\mcp\runtime\http-enabled"
if ($mcpEnabled) {
  try {
    # Nginx forwards this Host header to the loopback-only MCP service, whose
    # host allowlist intentionally accepts the configured public host rather
    # than the Nginx listener's 127.0.0.1:8080 address.
    $mcpHealth = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8080/_pi/mcp-health" -Headers @{ Host = $webDomain } -TimeoutSec 5
    if ($mcpHealth.StatusCode -ne 200) { throw "HTTP $($mcpHealth.StatusCode)" }
    Write-Host "Remote MCP (read only): OK" -ForegroundColor Green
  } catch {
    Write-Host "Remote MCP: FAIL - $($_.Exception.Message)" -ForegroundColor Red
    $failures.Add("remote MCP health failed")
  }
}

& docker compose exec -T web sh -lc "curl -fsS http://localhost:3000/health | grep -q OK"
if ($LASTEXITCODE -eq 0) {
  Write-Host "Mastodon web: OK" -ForegroundColor Green
} else {
  Write-Host "Mastodon web: FAIL" -ForegroundColor Red
  $failures.Add("Mastodon web health failed")
}

& docker compose exec -T streaming sh -lc "curl -fsS http://localhost:4000/api/v1/streaming/health | grep -q OK"
if ($LASTEXITCODE -eq 0) {
  Write-Host "Streaming: OK" -ForegroundColor Green
} else {
  Write-Host "Streaming: FAIL" -ForegroundColor Red
  $failures.Add("streaming health failed")
}

& docker compose exec -T sidekiq sh -lc "ps aux | grep '[s]idekiq' >/dev/null"
if ($LASTEXITCODE -eq 0) {
  Write-Host "Sidekiq: OK" -ForegroundColor Green
} else {
  Write-Host "Sidekiq: FAIL" -ForegroundColor Red
  $failures.Add("Sidekiq worker is not running")
}

$token = Get-EnvValue -Path ".env" -Key "CLOUDFLARE_TUNNEL_TOKEN"
if (-not [string]::IsNullOrWhiteSpace($token) -and $token -ne "MISSING" -and -not [string]::IsNullOrWhiteSpace($webDomain)) {
  Write-Host "`n=== Public web entrance ===" -ForegroundColor Cyan

  try {
    $public = Invoke-WebRequest -UseBasicParsing -Uri "https://$webDomain/_pi/health" -TimeoutSec 15
    if ($public.StatusCode -ne 200) { throw "HTTP $($public.StatusCode)" }
    Write-Host "Tunnel and Nginx: OK" -ForegroundColor Green
  } catch {
    Write-Host "Tunnel and Nginx: FAIL - $($_.Exception.Message)" -ForegroundColor Red
    $failures.Add("public Cloudflare health failed")
  }

  try {
    $instanceResponse = Invoke-WebRequest -UseBasicParsing -Uri "https://$webDomain/api/v2/instance" -TimeoutSec 15
    if ($instanceResponse.StatusCode -ne 200) { throw "HTTP $($instanceResponse.StatusCode)" }
    $instance = $instanceResponse.Content | ConvertFrom-Json

    if ($instance.domain -ne $identityDomainExpected) {
      throw "instance.domain is $($instance.domain), expected $identityDomainExpected"
    }
    $reportedStreaming = [string]$instance.configuration.urls.streaming
    if ($reportedStreaming -ne "wss://$webDomain") {
      throw "reported streaming URL is $reportedStreaming, expected wss://$webDomain"
    }

    Write-Host "Instance identity + current web metadata: OK" -ForegroundColor Green
  } catch {
    Write-Host "Instance metadata: FAIL - $($_.Exception.Message)" -ForegroundColor Red
    $failures.Add("public instance identity/web metadata failed")
  }

  try {
    $streamPublic = Invoke-WebRequest -UseBasicParsing -Uri "https://$webDomain/api/v1/streaming/health" -TimeoutSec 15
    if ($streamPublic.StatusCode -ne 200) { throw "HTTP $($streamPublic.StatusCode)" }
    Write-Host "Public streaming route: OK" -ForegroundColor Green
  } catch {
    Write-Host "Public streaming route: FAIL - $($_.Exception.Message)" -ForegroundColor Red
    $failures.Add("public streaming route failed")
  }

  if ($mcpEnabled) {
    try {
      $mcpPublic = Invoke-WebRequest -UseBasicParsing -Uri "https://$webDomain/_pi/mcp-health" -TimeoutSec 15
      if ($mcpPublic.StatusCode -ne 200) { throw "HTTP $($mcpPublic.StatusCode)" }
      Write-Host "Public remote MCP route: OK" -ForegroundColor Green
    } catch {
      Write-Host "Public remote MCP route: FAIL - $($_.Exception.Message)" -ForegroundColor Red
      $failures.Add("public remote MCP route failed")
    }
  }
}

Write-Host "`n=== Git safety ===" -ForegroundColor Cyan
if (Get-Command git -ErrorAction SilentlyContinue) {
  $trackedSensitive = @(& git ls-files -- .env .env.production .pi-os-initialized data backups logs .cloudflared 2>$null)
  if ($trackedSensitive.Count -eq 0) {
    Write-Host "Runtime secrets/data tracked by Git: none" -ForegroundColor Green
  } else {
    Write-Host "Sensitive paths are tracked: $($trackedSensitive -join ', ')" -ForegroundColor Red
    $failures.Add("sensitive runtime paths are tracked by Git")
  }
} else {
  Write-Warning "git is unavailable; skipped tracked-file safety check"
}

Write-Host ""
if ($failures.Count -eq 0) {
  Write-Host "PI OS smoke check passed." -ForegroundColor Green
  Write-Host "Browser-only checks still required: sign in, read old content, publish text + image, and confirm live updates."
  exit 0
}

Write-Host "PI OS smoke check failed:" -ForegroundColor Red
$failures | ForEach-Object { Write-Host "- $_" -ForegroundColor Red }
exit 1
