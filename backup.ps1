[CmdletBinding()]
param(
  [switch]$SkipMedia
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath ".env") -or -not (Test-Path -LiteralPath ".env.production")) {
  throw "PI OS is not configured."
}

& docker info *> $null
if ($LASTEXITCODE -ne 0) {
  throw "Docker Desktop is not running."
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$snapshotName = "pi-os-$stamp"
$snapshotPath = Join-Path $PSScriptRoot "backups\$snapshotName"
New-Item -ItemType Directory -Path $snapshotPath -Force | Out-Null

Write-Host "Ensuring database is running..." -ForegroundColor Cyan
& docker compose up -d db redis
if ($LASTEXITCODE -ne 0) { throw "Could not start database services." }

Write-Host "Dumping PostgreSQL..." -ForegroundColor Cyan
& docker compose exec -T db pg_dump -U mastodon -d mastodon_production -Fc -f "/backups/$snapshotName/database.dump"
if ($LASTEXITCODE -ne 0) { throw "PostgreSQL backup failed." }

Copy-Item -LiteralPath ".env" -Destination (Join-Path $snapshotPath ".env")
Copy-Item -LiteralPath ".env.production" -Destination (Join-Path $snapshotPath ".env.production")
Copy-Item -LiteralPath "compose.yml" -Destination (Join-Path $snapshotPath "compose.yml")

$versionOutput = & docker compose run --rm --no-deps web bin/tootctl --version 2>&1
if ($LASTEXITCODE -eq 0) {
  $versionOutput | Set-Content -LiteralPath (Join-Path $snapshotPath "mastodon-version.txt") -Encoding UTF8
}

@(
  "created_at=$([DateTime]::UtcNow.ToString('o'))",
  "snapshot=$snapshotName",
  "contains_plaintext_secrets=true"
) | Set-Content -LiteralPath (Join-Path $snapshotPath "manifest.txt") -Encoding UTF8

if (-not $SkipMedia) {
  if (Test-Path -LiteralPath ".\data\media") {
    $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
    if ($tar) {
      Write-Host "Archiving uploaded media..." -ForegroundColor Cyan
      & $tar.Source -czf (Join-Path $snapshotPath "media.tar.gz") -C (Join-Path $PSScriptRoot "data\media") .
      if ($LASTEXITCODE -ne 0) { throw "Media archive failed." }
    } else {
      Write-Warning "tar.exe is unavailable; database and secrets were backed up, but media was skipped."
    }
  }
}

Write-Host "Backup completed: $snapshotPath" -ForegroundColor Green
Write-Warning "This snapshot contains plaintext secrets and private data. Move a copy to an encrypted offline location."
