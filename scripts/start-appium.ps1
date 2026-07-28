[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
Set-Location $script:AppiumRoot
if (-not (Test-Path $script:AppiumEntryPoint)) {
    throw "Local Appium is not installed. Run .\scripts\setup.ps1 first."
}
& node.exe $script:AppiumEntryPoint --address 127.0.0.1 --port 4723 --base-path /
