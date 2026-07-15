[CmdletBinding()]
param(
    [string]$InstallRoot = $(if ($env:IMPRINT_INSTALL_ROOT) { $env:IMPRINT_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA "ImprintApp\app" }),
    [string]$Config = $(if ($env:IMPRINT_CONFIG) { $env:IMPRINT_CONFIG } else { Join-Path $env:APPDATA "Imprint\config.json" }),
    [string]$Settings = $(if ($env:CLAUDE_SETTINGS_PATH) { $env:CLAUDE_SETTINGS_PATH } else { Join-Path $env:USERPROFILE ".claude\settings.json" }),
    [switch]$PurgeConfig
)
$ErrorActionPreference = "Stop"
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$VolumeRoot = [IO.Path]::GetPathRoot($InstallRoot)
if ($InstallRoot -eq $VolumeRoot -or $InstallRoot -eq [IO.Path]::GetFullPath($env:USERPROFILE)) { throw "Refusing an unsafe install root: $InstallRoot" }
if (-not (Test-Path $InstallRoot -PathType Container)) { throw "Imprint install root does not exist: $InstallRoot" }
$RootItem = Get-Item $InstallRoot -Force
if (($RootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Refusing a reparse-point install root: $InstallRoot" }
$Marker = Join-Path $InstallRoot ".imprint-install-root"
if (-not (Test-Path $Marker -PathType Leaf) -or (Get-Content -Raw $Marker) -ne "imprint-local:3.0.0`n") { throw "Refusing to remove an install root without Imprint's ownership marker: $InstallRoot" }
$Python = Join-Path $InstallRoot "venv\Scripts\python.exe"
$Manager = Join-Path $InstallRoot "tools\manage_hooks.py"
$Ownership = Join-Path $InstallRoot "tools\install_ownership.py"
if (-not (Test-Path $Python -PathType Leaf) -or -not (Test-Path $Ownership -PathType Leaf)) { throw "Refusing uninstall because ownership tooling is missing." }
& $Python $Ownership verify --root $InstallRoot
if ($LASTEXITCODE -ne 0) { throw "Installed-file ownership verification failed; installation was left intact." }
if (Test-Path $Manager) {
    & $Python $Manager unregister --settings $Settings --python $Python --hooks-dir (Join-Path $InstallRoot "hooks")
    if ($LASTEXITCODE -ne 0) { throw "Managed hook removal failed; installation was left intact." }
}
& $Python $Ownership uninstall --root $InstallRoot
if ($LASTEXITCODE -ne 0) { throw "Conservative installed-file removal failed." }
if ($PurgeConfig -and (Test-Path $Config)) { Remove-Item $Config -Force }
Write-Host "Imprint code and managed hooks removed. Captured data was preserved."
