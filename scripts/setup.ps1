[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
Set-Location $script:ProjectRoot

$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    throw "Python was not found. Run .\scripts\bootstrap-windows.ps1 -InstallMissingPrerequisites -AcceptAndroidLicenses"
}
if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) {
    throw "Node.js was not found. Run the Windows bootstrap script first."
}
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    throw "npm.cmd was not found. Reinstall the Node.js LTS package."
}

if (-not (Test-Path $script:Python)) {
    New-Item -ItemType Directory -Path $script:RuntimeRoot -Force | Out-Null
    & $pythonCommand.Source -m venv (Join-Path $script:RuntimeRoot "python")
}

& $script:Python -m ensurepip --upgrade
& $script:Python -m pip install --upgrade pip
& $script:Python -m pip install --editable .
$eggInfo = Join-Path $script:ProjectRoot "src\appium_tool.egg-info"
if (Test-Path -LiteralPath $eggInfo) {
    Remove-Item -LiteralPath $eggInfo -Recurse -Force
}

Push-Location $script:AppiumRoot
try {
    if (Test-Path "package-lock.json") {
        & npm.cmd ci
    } else {
        & npm.cmd install
    }

    if (-not (Test-Path "node_modules\appium-uiautomator2-driver\package.json")) {
        throw "The pinned UiAutomator2 Appium driver was not installed."
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Project dependencies are ready."
