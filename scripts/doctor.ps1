[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "common.ps1")
Set-Location $script:ProjectRoot
$failures = 0

function Check-Command {
    param([string]$Name, [scriptblock]$Version)
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) {
        Write-Host "[FAIL] $Name is not available." -ForegroundColor Red
        $script:failures++
        return
    }
    $value = & $Version
    Write-Host "[ OK ] $Name - $value" -ForegroundColor Green
}

Check-Command "python.exe" { python --version }
Check-Command "node.exe" { node --version }
Check-Command "npm.cmd" { npm.cmd --version }
Check-Command "java.exe" { (java -version 2>&1 | Select-Object -First 1) }

try {
    $sdk = Get-AndroidSdkRoot
    Write-Host "[ OK ] Android SDK - $sdk" -ForegroundColor Green
    foreach ($tool in @(
        "platform-tools\adb.exe",
        "emulator\emulator.exe"
    )) {
        [void](Get-SdkTool $tool)
        Write-Host "[ OK ] $tool" -ForegroundColor Green
    }
    $sdkManagerCandidates = @(
        (Join-Path $sdk "cmdline-tools\latest\bin\sdkmanager.bat"),
        (Join-Path $sdk "tools\bin\sdkmanager.bat")
    )
    $sdkManager = $sdkManagerCandidates |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
    if ($sdkManager) {
        Write-Host "[ OK ] sdkmanager - $sdkManager" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] sdkmanager was not found." -ForegroundColor Red
        $failures++
    }
    $emulator = Get-SdkTool "emulator\emulator.exe"
    if ((& $emulator -list-avds) -contains $script:AvdName) {
        Write-Host "[ OK ] AVD - $script:AvdName" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] AVD '$script:AvdName' is missing." -ForegroundColor Red
        $failures++
    }
} catch {
    Write-Host "[FAIL] $($_.Exception.Message)" -ForegroundColor Red
    $failures++
}

if (Test-Path $script:Python) {
    Write-Host "[ OK ] Python virtual environment" -ForegroundColor Green
} else {
    Write-Host "[FAIL] Python virtual environment is missing." -ForegroundColor Red
    $failures++
}
if (Test-Path $script:AppiumEntryPoint) {
    Write-Host "[ OK ] Local Appium server" -ForegroundColor Green
} else {
    Write-Host "[FAIL] Local Appium server is missing." -ForegroundColor Red
    $failures++
}
$driverPackage = Join-Path $script:AppiumRoot "node_modules\appium-uiautomator2-driver\package.json"
if (Test-Path $driverPackage) {
    Write-Host "[ OK ] UiAutomator2 Appium driver" -ForegroundColor Green
} else {
    Write-Host "[FAIL] UiAutomator2 Appium driver is missing." -ForegroundColor Red
    $failures++
}
if (Test-Path $script:Apk) {
    Write-Host "[ OK ] APK file" -ForegroundColor Green
} else {
    Write-Host "[FAIL] APK file is missing." -ForegroundColor Red
    $failures++
}
if (Test-Path $script:Python) {
    & $script:Python -c "import os; os.environ['APPIUM_TOOL_SERVICE_TOKEN']='doctor-service'; os.environ['APPIUM_TOOL_ADMIN_TOKEN']='doctor-admin'; from starlette.testclient import TestClient; from appium_tool import create_app; assert TestClient(create_app()).get('/health').status_code == 200"
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[ OK ] Appium Tool import and health route" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] Appium Tool health check failed." -ForegroundColor Red
        $failures++
    }
}

if ($failures -gt 0) {
    Write-Host "$failures required check(s) failed." -ForegroundColor Red
    exit 1
}
Write-Host "All required checks passed." -ForegroundColor Green
