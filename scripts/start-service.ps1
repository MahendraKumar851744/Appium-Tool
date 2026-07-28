[CmdletBinding()]
param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
Set-Location $script:ProjectRoot

if (-not (Test-Path $script:Python)) {
    throw "Python environment is missing. Run .\scripts\setup.ps1 first."
}

$env:APPIUM_TOOL_HOST = $HostAddress
$env:APPIUM_TOOL_PORT = "$Port"
& $script:Python -m appium_tool
