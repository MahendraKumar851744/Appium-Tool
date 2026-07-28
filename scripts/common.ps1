$script:ProjectRoot = Split-Path -Parent $PSScriptRoot
$script:AvdName = "Appium_Arm32_API30"
$script:RequiredAbi = "armeabi-v7a"
$script:RuntimeRoot = Join-Path $script:ProjectRoot ".runtime"
$script:Python = Join-Path $script:RuntimeRoot "python\Scripts\python.exe"
$script:AppiumRoot = Join-Path $script:ProjectRoot "tooling\appium"
$script:AppiumEntryPoint = Join-Path $script:AppiumRoot "node_modules\appium\build\lib\main.js"
$script:Apk = Join-Path $script:ProjectRoot "assets\apps\message.apk"
$env:PYTHONDONTWRITEBYTECODE = "1"

function Get-AndroidSdkRoot {
    $candidates = @(
        $env:ANDROID_SDK_ROOT,
        $env:ANDROID_HOME,
        (Join-Path $env:LOCALAPPDATA "Android\Sdk"),
        (Join-Path $env:USERPROFILE "AppData\Local\Android\Sdk")
    ) | Where-Object { $_ }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    throw "Android SDK not found. Run .\scripts\bootstrap-windows.ps1."
}

function Get-SdkTool {
    param([Parameter(Mandatory = $true)][string]$RelativePath)
    $path = Join-Path (Get-AndroidSdkRoot) $RelativePath
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required Android SDK tool not found: $path"
    }
    return $path
}

function Get-CompatibleDevice {
    $adb = Get-SdkTool "platform-tools\adb.exe"
    $lines = & $adb devices | Select-String "^\S+\s+device$"
    foreach ($line in $lines) {
        $serial = ($line.Line -split "\s+")[0]
        $abiList = "$(& $adb -s $serial shell getprop ro.product.cpu.abilist 2>$null)".Trim()
        if ($abiList -match "(^|,)$([regex]::Escape($script:RequiredAbi))(,|$)") {
            return $serial
        }
    }
    return $null
}

function Wait-ForCompatibleDevice {
    param([int]$TimeoutSeconds = 180)
    $adb = Get-SdkTool "platform-tools\adb.exe"
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $serial = Get-CompatibleDevice
        if ($serial) {
            $booted = "$(& $adb -s $serial shell getprop sys.boot_completed 2>$null)".Trim()
            if ($booted -eq "1") {
                return $serial
            }
        }
        Write-Host "Waiting for the compatible Android emulator to boot..."
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    throw "No $script:RequiredAbi-compatible emulator became ready within $TimeoutSeconds seconds."
}

function Test-AppiumReady {
    try {
        $status = Invoke-RestMethod -Uri "http://127.0.0.1:4723/status" -TimeoutSec 3
        return [bool]$status.value.ready
    } catch {
        return $false
    }
}
