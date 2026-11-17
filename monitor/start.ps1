<#
.SYNOPSIS
    Start the wg-monitor app in one step.

.DESCRIPTION
    1. Checks that the AWS profile has a valid session; if not, runs `aws login`
       (a browser window opens - pick the "Console Dot Dream" session).
    2. If the app is already running on the port, stops (with -Restart) or leaves it.
    3. Starts monitor\wgmon.py with Python 3.12, which opens the browser itself.

.EXAMPLE
    pwsh -File monitor\start.ps1
    pwsh -File monitor\start.ps1 -Restart
    pwsh -File monitor\start.ps1 -CreateShortcut   # adds a "WG Monitor" desktop shortcut
#>
[CmdletBinding()]
param(
    [string]$AwsProfile = "wg",
    [int]$Port = 8765,
    [switch]$Restart,
    [switch]$CreateShortcut
)

$ErrorActionPreference = "Stop"
$app = Join-Path $PSScriptRoot "wgmon.py"

if ($CreateShortcut) {
    $lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "WG Monitor.lnk"
    $pwsh = (Get-Command pwsh).Source
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $s.TargetPath = $pwsh
    $s.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $s.WorkingDirectory = Split-Path $PSScriptRoot -Parent
    $s.Save()
    Write-Host "Shortcut created: $lnk"
    return
}

function Fail($msg) {
    Write-Host $msg -ForegroundColor Red
    Read-Host "Press Enter to close" | Out-Null
    exit 1
}

# --- prerequisites ------------------------------------------------------------
foreach ($cmd in "py", "aws", "ssh") {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { Fail "'$cmd' is not on PATH." }
}
& py -3.12 -c "import sys" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "Python 3.12 not found (py -3.12)." }

$env:AWS_PROFILE = $AwsProfile

# --- AWS session ----------------------------------------------------------------
Write-Host "Checking AWS session for profile '$AwsProfile'..."
& aws sts get-caller-identity --output text --query Account 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Session missing or expired - opening AWS login (pick 'Console Dot Dream')." -ForegroundColor Yellow
    & aws login --profile $AwsProfile
    & aws sts get-caller-identity --output text --query Account 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "AWS login did not succeed." }
}
Write-Host "AWS session OK." -ForegroundColor Green

# --- already running? -------------------------------------------------------------
$existing = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    $procId = $existing[0].OwningProcess
    if ($Restart) {
        Write-Host "Stopping the running app (PID $procId)..."
        Stop-Process -Id $procId -Force
        Start-Sleep -Milliseconds 500
    } else {
        Write-Host "wg-monitor is already running on port $Port (PID $procId)." -ForegroundColor Yellow
        Write-Host "Use its existing browser tab (the URL holds the token), or rerun with -Restart."
        Read-Host "Press Enter to close" | Out-Null
        exit 0
    }
}

# --- start -------------------------------------------------------------------------
Write-Host "Starting wg-monitor (Ctrl+C or close this window to stop)..."
& py -3.12 $app --port $Port --profile $AwsProfile
