[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot

$failures = New-Object System.Collections.Generic.List[string]

& docker info *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Docker Desktop: FAIL" -ForegroundColor Red
  exit 1
}

Write-Host "=== Containers ===" -ForegroundColor Cyan
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

$domainLine = if (Test-Path ".env.production") {
  Get-Content ".env.production" | Where-Object { $_ -match "^LOCAL_DOMAIN=" } | Select-Object -Last 1
} else { $null }
$domain = if ($domainLine) { $domainLine -replace "^LOCAL_DOMAIN=", "" } else { "" }
$tokenLine = if (Test-Path ".env") {
  Get-Content ".env" | Where-Object { $_ -match "^CLOUDFLARE_TUNNEL_TOKEN=" } | Select-Object -Last 1
} else { $null }
$token = if ($tokenLine) { $tokenLine -replace "^CLOUDFLARE_TUNNEL_TOKEN=", "" } else { "" }

if (-not [string]::IsNullOrWhiteSpace($token) -and $token -ne "MISSING" -and -not [string]::IsNullOrWhiteSpace($domain)) {
  Write-Host "`n=== Public path ===" -ForegroundColor Cyan

  try {
    $public = Invoke-WebRequest -UseBasicParsing -Uri "https://$domain/_pi/health" -TimeoutSec 15
    if ($public.StatusCode -ne 200) { throw "HTTP $($public.StatusCode)" }
    Write-Host "Tunnel and Nginx: OK" -ForegroundColor Green
  } catch {
    Write-Host "Tunnel and Nginx: FAIL - $($_.Exception.Message)" -ForegroundColor Red
    $failures.Add("public Cloudflare health failed")
  }

  try {
    $instance = Invoke-WebRequest -UseBasicParsing -Uri "https://$domain/api/v2/instance" -TimeoutSec 15
    if ($instance.StatusCode -ne 200) { throw "HTTP $($instance.StatusCode)" }
    Write-Host "Public Mastodon API discovery: OK" -ForegroundColor Green
  } catch {
    Write-Host "Public Mastodon API discovery: FAIL - $($_.Exception.Message)" -ForegroundColor Red
    $failures.Add("public Mastodon API discovery failed")
  }

  try {
    $streamPublic = Invoke-WebRequest -UseBasicParsing -Uri "https://$domain/api/v1/streaming/health" -TimeoutSec 15
    if ($streamPublic.StatusCode -ne 200) { throw "HTTP $($streamPublic.StatusCode)" }
    Write-Host "Public streaming route: OK" -ForegroundColor Green
  } catch {
    Write-Host "Public streaming route: FAIL - $($_.Exception.Message)" -ForegroundColor Red
    $failures.Add("public streaming route failed")
  }
}

Write-Host "`n=== Git safety ===" -ForegroundColor Cyan
if (Get-Command git -ErrorAction SilentlyContinue) {
  $trackedSensitive = @(& git ls-files -- .env .env.production .pi-os-initialized data backups .cloudflared 2>$null)
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
  exit 0
}

Write-Host "PI OS smoke check failed:" -ForegroundColor Red
$failures | ForEach-Object { Write-Host "- $_" -ForegroundColor Red }
exit 1
