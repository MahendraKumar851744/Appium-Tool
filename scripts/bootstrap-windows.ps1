[CmdletBinding()]
param(
    [switch]$InstallMissingPrerequisites,
    [switch]$AcceptAndroidLicenses
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$commandLineToolsVersion = "15859902"
$commandLineToolsSha256 = "90AE805D20434428BFFCB699C290860F19BB5F66A67E6B330067E3DE801FB04A"
$commandLineToolsUrl = "https://dl.google.com/android/repository/commandlinetools-win-$($commandLineToolsVersion)_latest.zip"
$sdkRoot = Join-Path $env:LOCALAPPDATA "Android\Sdk"
$avdName = "Appium_Arm32_API30"
$systemImage = "system-images;android-30;google_apis_playstore;x86"

function Refresh-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Install-WithWinget {
    param([string]$Id)
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw "winget is required to install '$Id'. Install Microsoft App Installer, then rerun this script."
    }
    & winget.exe install --id $Id --exact --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "winget failed to install $Id."
    }
    Refresh-ProcessPath
}

function Require-Command {
    param([string]$Command, [string]$WingetId)
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        if (-not $InstallMissingPrerequisites) {
            throw "'$Command' is missing. Rerun with -InstallMissingPrerequisites."
        }
        Install-WithWinget $WingetId
    }
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "'$Command' is still unavailable after installation. Open a new terminal and rerun the command."
    }
}

function Add-UserPath {
    param([string]$PathEntry)
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($current -split ";" | Where-Object { $_ })
    if ($parts -notcontains $PathEntry) {
        [Environment]::SetEnvironmentVariable("Path", (($parts + $PathEntry) -join ";"), "User")
    }
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "This bootstrap script currently supports Windows only."
}

Require-Command "git.exe" "Git.Git"
Require-Command "node.exe" "OpenJS.NodeJS.LTS"
Require-Command "npm.cmd" "OpenJS.NodeJS.LTS"
Require-Command "python.exe" "Python.Python.3.11"
Require-Command "java.exe" "EclipseAdoptium.Temurin.17.JDK"

$nodeVersion = [version]((node --version).TrimStart("v"))
if ($nodeVersion -lt [version]"20.19.0") {
    throw "Node.js 20.19.0 or newer is required by Appium 3. Installed: $nodeVersion"
}

$javaCommand = Get-Command java.exe
$javaHome = Split-Path -Parent (Split-Path -Parent $javaCommand.Source)
$env:JAVA_HOME = $javaHome
[Environment]::SetEnvironmentVariable("JAVA_HOME", $javaHome, "User")

New-Item -ItemType Directory -Path $sdkRoot -Force | Out-Null
$sdkManager = Join-Path $sdkRoot "cmdline-tools\latest\bin\sdkmanager.bat"
if (-not (Test-Path $sdkManager)) {
    $downloadRoot = Join-Path $env:TEMP "appium-tool-android-tools"
    $archive = Join-Path $downloadRoot "command-line-tools.zip"
    $expanded = Join-Path $downloadRoot "expanded"
    if (Test-Path $downloadRoot) {
        $resolvedTemp = [System.IO.Path]::GetFullPath($env:TEMP)
        $resolvedDownload = [System.IO.Path]::GetFullPath($downloadRoot)
        if (-not $resolvedDownload.StartsWith($resolvedTemp)) {
            throw "Temporary tools directory resolved outside the system temp directory."
        }
        Remove-Item -LiteralPath $downloadRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Path $expanded -Force | Out-Null
    Write-Host "Downloading Android command-line tools..."
    Invoke-WebRequest -Uri $commandLineToolsUrl -OutFile $archive
    $actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
    if ($actualHash -ne $commandLineToolsSha256) {
        throw "Android command-line tools checksum mismatch. Expected $commandLineToolsSha256, got $actualHash."
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $expanded
    $latest = Join-Path $sdkRoot "cmdline-tools\latest"
    New-Item -ItemType Directory -Path $latest -Force | Out-Null
    Copy-Item -Path (Join-Path $expanded "cmdline-tools\*") -Destination $latest -Recurse -Force
}

$env:ANDROID_HOME = $sdkRoot
$env:ANDROID_SDK_ROOT = $sdkRoot
[Environment]::SetEnvironmentVariable("ANDROID_HOME", $sdkRoot, "User")
[Environment]::SetEnvironmentVariable("ANDROID_SDK_ROOT", $sdkRoot, "User")
foreach ($pathEntry in @(
    (Join-Path $sdkRoot "platform-tools"),
    (Join-Path $sdkRoot "emulator"),
    (Join-Path $sdkRoot "cmdline-tools\latest\bin")
)) {
    Add-UserPath $pathEntry
    if (($env:Path -split ";") -notcontains $pathEntry) {
        $env:Path = "$env:Path;$pathEntry"
    }
}

if (-not $AcceptAndroidLicenses) {
    throw "Android SDK licenses must be accepted explicitly. Rerun with -AcceptAndroidLicenses after reviewing Google's SDK terms."
}

Write-Host "Accepting Android SDK licenses as explicitly requested..."
1..100 | ForEach-Object { "y" } |
    & $sdkManager --licenses "--sdk_root=$sdkRoot" |
    Out-Host

Write-Host "Installing Android SDK, emulator, and ARMv7-compatible system image..."
& $sdkManager "--sdk_root=$sdkRoot" `
    "platform-tools" `
    "emulator" `
    "platforms;android-30" `
    "build-tools;35.0.0" `
    $systemImage

$avdManager = Join-Path $sdkRoot "cmdline-tools\latest\bin\avdmanager.bat"
$emulator = Join-Path $sdkRoot "emulator\emulator.exe"
if ((& $emulator -list-avds) -notcontains $avdName) {
    Write-Host "Creating AVD $avdName..."
    "no" | & $avdManager create avd --name $avdName --package $systemImage --device "pixel_4"
}

& (Join-Path $PSScriptRoot "setup.ps1")
& (Join-Path $PSScriptRoot "doctor.ps1")

Write-Host ""
Write-Host "Bootstrap complete. Run everything with:"
Write-Host "  .\scripts\run-all.ps1"
