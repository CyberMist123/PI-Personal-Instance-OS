[CmdletBinding()]
param(
    [string]$PythonCommand = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$Root = $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$Runtime = Join-Path $Root "runtime"

function Invoke-NativeSafe {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory = $Root
    )

    $stdout = [System.IO.Path]::GetTempFileName()
    $stderr = [System.IO.Path]::GetTempFileName()
    try {
        $process = Start-Process `
            -FilePath $FilePath `
            -ArgumentList $Arguments `
            -WorkingDirectory $WorkingDirectory `
            -NoNewWindow `
            -Wait `
            -PassThru `
            -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr

        $outText = Get-Content -LiteralPath $stdout -Raw -ErrorAction SilentlyContinue
        $errText = Get-Content -LiteralPath $stderr -Raw -ErrorAction SilentlyContinue
        if ($outText) { Write-Host $outText.TrimEnd() }
        if ($errText) { Write-Host $errText.TrimEnd() }

        if ($process.ExitCode -ne 0) {
            throw "Native command failed with exit code $($process.ExitCode): $FilePath $($Arguments -join ' ')"
        }
    }
    finally {
        Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
    }
}

function Resolve-Python {
    if ($PythonCommand) {
        return @{ File = $PythonCommand; Prefix = @() }
    }

    $py = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($version in @("-3.13", "-3.12", "-3.11")) {
            $stdout = [System.IO.Path]::GetTempFileName()
            $stderr = [System.IO.Path]::GetTempFileName()
            try {
                $process = Start-Process `
                    -FilePath $py.Source `
                    -ArgumentList @($version, "--version") `
                    -NoNewWindow -Wait -PassThru `
                    -RedirectStandardOutput $stdout `
                    -RedirectStandardError $stderr
                if ($process.ExitCode -eq 0) {
                    return @{ File = $py.Source; Prefix = @($version) }
                }
            }
            finally {
                Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
            }
        }
    }

    $python = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($python) {
        $stdout = [System.IO.Path]::GetTempFileName()
        $stderr = [System.IO.Path]::GetTempFileName()
        try {
            $process = Start-Process `
                -FilePath $python.Source `
                -ArgumentList @("--version") `
                -NoNewWindow -Wait -PassThru `
                -RedirectStandardOutput $stdout `
                -RedirectStandardError $stderr
            $versionText = ((Get-Content -LiteralPath $stdout -Raw -ErrorAction SilentlyContinue) + " " +
                (Get-Content -LiteralPath $stderr -Raw -ErrorAction SilentlyContinue))
            if ($process.ExitCode -eq 0 -and $versionText -match "Python 3\.(1[1-9]|[2-9][0-9])") {
                return @{ File = $python.Source; Prefix = @() }
            }
        }
        finally {
            Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
        }
    }

    throw "Python 3.11 or newer was not found."
}

Write-Progress -Activity "CMX MCP installation" -Status "1/6 Resolve Python" -PercentComplete 10
$Python = Resolve-Python
Write-Host "Python launcher: $($Python.File) $($Python.Prefix -join ' ')"

Write-Progress -Activity "CMX MCP installation" -Status "2/6 Create virtual environment" -PercentComplete 25
if (-not (Test-Path -LiteralPath $Venv)) {
    Invoke-NativeSafe -FilePath $Python.File -Arguments @($Python.Prefix + @("-m", "venv", $Venv))
}
$VenvPython = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Virtual environment Python is missing: $VenvPython"
}

Write-Progress -Activity "CMX MCP installation" -Status "3/6 Upgrade packaging tools" -PercentComplete 40
Invoke-NativeSafe -FilePath $VenvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")

Write-Progress -Activity "CMX MCP installation" -Status "4/6 Install CMX MCP" -PercentComplete 60
Invoke-NativeSafe -FilePath $VenvPython -Arguments @("-m", "pip", "install", "-e", $Root)

Write-Progress -Activity "CMX MCP installation" -Status "5/6 Initialize SQLite" -PercentComplete 80
$Admin = Join-Path $Venv "Scripts\cmx-admin.exe"
Invoke-NativeSafe -FilePath $Admin -Arguments @("init")

Write-Progress -Activity "CMX MCP installation" -Status "6/6 Verify files" -PercentComplete 95
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
foreach ($required in @(
    $Admin,
    (Join-Path $Venv "Scripts\cmx-mcp.exe"),
    (Join-Path $Runtime "cmx.sqlite3")
)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required file is missing after installation: $required"
    }
}

Write-Progress -Activity "CMX MCP installation" -Completed
Write-Host ""
Write-Host "CMX MCP installation completed." -ForegroundColor Green
Write-Host "Next:"
Write-Host "  powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Root\add-bot.ps1`" -BotId fable -DisplayName Fable"
