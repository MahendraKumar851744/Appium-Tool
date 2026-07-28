[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
Set-Location $script:ProjectRoot

if (Test-AppiumReady) {
    Write-Host "Appium is already ready at http://127.0.0.1:4723"
    exit 0
}

$node = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $node) {
    throw "Node.js was not found. Run the bootstrap script first."
}
if (-not (Test-Path $script:AppiumEntryPoint)) {
    throw "Local Appium is not installed. Run .\scripts\setup.ps1 first."
}

New-Item -ItemType Directory -Path $script:RuntimeRoot -Force | Out-Null
$appiumArguments = "`"$script:AppiumEntryPoint`" --address 127.0.0.1 --port 4723 --base-path /"
$process = Start-Process `
    -FilePath $node.Source `
    -ArgumentList $appiumArguments `
    -WorkingDirectory $script:AppiumRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $script:RuntimeRoot "appium.stdout.log") `
    -RedirectStandardError (Join-Path $script:RuntimeRoot "appium.stderr.log") `
    -PassThru
$process.Id | Set-Content -Path (Join-Path $script:RuntimeRoot "appium.pid")

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    if (Test-AppiumReady) {
        Write-Host "Appium ready at http://127.0.0.1:4723 (PID $($process.Id))"
        exit 0
    }
    if ($process.HasExited) {
        $errorLog = Get-Content (Join-Path $script:RuntimeRoot "appium.stderr.log") -Raw -ErrorAction SilentlyContinue
        throw "Appium exited during startup. $errorLog"
    }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $deadline)

throw "Appium did not become ready within $TimeoutSeconds seconds."
