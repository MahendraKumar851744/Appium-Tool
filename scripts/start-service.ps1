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

$envFile = Join-Path $script:ProjectRoot ".env"
if (Test-Path -LiteralPath $envFile) {
    Get-Content -LiteralPath $envFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            return
        }
        $name, $value = $line.Split("=", 2)
        [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), "Process")
    }
}

$env:APPIUM_TOOL_HOST = $HostAddress
$env:APPIUM_TOOL_PORT = "$Port"
& $script:Python -m appium_tool
