[CmdletBinding()]
param(
    [string]$InstallRoot = $(if ($env:IMPRINT_INSTALL_ROOT) { $env:IMPRINT_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA "ImprintApp\app" }),
    [string]$Config = $(if ($env:IMPRINT_CONFIG) { $env:IMPRINT_CONFIG } else { Join-Path $env:APPDATA "Imprint\config.json" }),
    [string]$Settings = $(if ($env:CLAUDE_SETTINGS_PATH) { $env:CLAUDE_SETTINGS_PATH } else { Join-Path $env:USERPROFILE ".claude\settings.json" }),
    [string]$DataRoot = $(if ($env:IMPRINT_DATA_ROOT) { $env:IMPRINT_DATA_ROOT } else { Join-Path $env:LOCALAPPDATA "Imprint" }),
    [string]$Operator = "default",
    [string]$Python = "python",
    [switch]$NoHooks
)
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ArtifactRoot = Split-Path -Parent $ScriptDir

if ($Operator -notmatch '^[a-z0-9][a-z0-9-]*$') { throw "Operator must use lowercase letters, digits, and hyphens." }
& $Python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 'Imprint requires Python 3.10+')"
if ($LASTEXITCODE -ne 0) { throw "Python 3.10 or newer is required." }
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$VolumeRoot = [IO.Path]::GetPathRoot($InstallRoot)
if ($InstallRoot -eq $VolumeRoot -or $InstallRoot -eq [IO.Path]::GetFullPath($env:USERPROFILE)) { throw "Refusing an unsafe install root: $InstallRoot" }
$Marker = Join-Path $InstallRoot ".imprint-install-root"
if (Test-Path $InstallRoot) {
    $item = Get-Item $InstallRoot -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Refusing a reparse-point install root: $InstallRoot" }
    $children = @(Get-ChildItem $InstallRoot -Force)
    if ($children.Count -gt 0 -and ((-not (Test-Path $Marker -PathType Leaf)) -or (Get-Content -Raw $Marker) -ne "imprint-local:3.0.0`n")) {
        throw "Refusing a non-empty install root not owned by Imprint: $InstallRoot"
    }
}
$Wheel = Get-ChildItem -Path (Join-Path $ArtifactRoot "dist") -Filter "imprint_local-3.0.0-*.whl" | Select-Object -First 1
if (-not $Wheel) { throw "The release artifact is incomplete: dist/imprint_local-3.0.0-*.whl is missing." }

$StateRoot = Join-Path ([IO.Path]::GetTempPath()) ("imprint-install-state-" + [guid]::NewGuid())
$BackupRoot = "$InstallRoot.imprint-backup.$PID"
if (Test-Path $BackupRoot) { throw "Refusing to overwrite stale install backup: $BackupRoot" }
New-Item -ItemType Directory -Path $StateRoot | Out-Null
function Save-StateFile([string]$Path, [string]$Name) {
    if (Test-Path $Path -PathType Leaf) { Copy-Item $Path (Join-Path $StateRoot $Name) }
    else { New-Item -ItemType File -Path (Join-Path $StateRoot "$Name.absent") | Out-Null }
}
function Restore-StateFile([string]$Path, [string]$Name) {
    if (Test-Path (Join-Path $StateRoot "$Name.absent")) { Remove-Item $Path -Force -ErrorAction SilentlyContinue }
    else { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null; Copy-Item -Force (Join-Path $StateRoot $Name) $Path }
}
Save-StateFile $Config "config"
Save-StateFile $Settings "settings"
$Succeeded = $false
try {
    if (Test-Path $InstallRoot) { Move-Item $InstallRoot $BackupRoot }
    New-Item -ItemType Directory -Force -Path $InstallRoot, (Split-Path -Parent $Config), $DataRoot | Out-Null
    & $Python -m venv (Join-Path $InstallRoot "venv")
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the isolated Imprint environment." }
    $VenvPython = Join-Path $InstallRoot "venv\Scripts\python.exe"
    & $VenvPython -m pip install --disable-pip-version-check --no-index --force-reinstall $Wheel.FullName
    if ($LASTEXITCODE -ne 0) { throw "Unable to install the Imprint wheel." }

    $HookTarget = Join-Path $InstallRoot "hooks"
    $ToolTarget = Join-Path $InstallRoot "tools"
    Copy-Item (Join-Path $ArtifactRoot "hooks") $HookTarget -Recurse
    New-Item -ItemType Directory -Force -Path $ToolTarget | Out-Null
    Copy-Item (Join-Path $ArtifactRoot "tools\install\manage_hooks.py") (Join-Path $ToolTarget "manage_hooks.py")
    Copy-Item (Join-Path $ArtifactRoot "tools\install\install_ownership.py") (Join-Path $ToolTarget "install_ownership.py")

    $ConfigValue = @{}
    if (Test-Path $Config) {
        $parsed = Get-Content -Raw $Config | ConvertFrom-Json -AsHashtable
        if ($parsed) { $ConfigValue = $parsed }
    }
    $ConfigValue["config_version"] = "3.0.0"
    $ConfigValue["data_root"] = [IO.Path]::GetFullPath($DataRoot)
    $ConfigValue["operator_slug"] = $Operator
    $ConfigValue["hooks_dir"] = [IO.Path]::GetFullPath($HookTarget)
    if (-not $ConfigValue.ContainsKey("node_id")) { $ConfigValue["node_id"] = "primary" }
    if (-not $ConfigValue.ContainsKey("compiler")) { $ConfigValue["compiler"] = $true }
    if (-not $ConfigValue.ContainsKey("context_budget_bytes")) { $ConfigValue["context_budget_bytes"] = 32768 }
    if (-not $ConfigValue.ContainsKey("experimental")) { $ConfigValue["experimental"] = @{ digest = $false; profile_learning = $false } }
    $TempConfig = "$Config.imprint-tmp"
    $ConfigValue | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 $TempConfig
    Move-Item -Force $TempConfig $Config

    if (-not $NoHooks) {
        & $VenvPython (Join-Path $ToolTarget "manage_hooks.py") register --settings $Settings --python $VenvPython --hooks-dir $HookTarget
        if ($LASTEXITCODE -ne 0) { throw "Managed hook registration failed." }
    }
    $env:IMPRINT_CONFIG = $Config
    $Imprint = Join-Path $InstallRoot "venv\Scripts\imprint.exe"
    $Version = & $Imprint version
    if ($LASTEXITCODE -ne 0 -or $Version -ne "3.0.0") { throw "Installed Imprint failed its version check." }
    $Ownership = Join-Path $ToolTarget "install_ownership.py"
    & $VenvPython $Ownership record --root $InstallRoot
    if ($LASTEXITCODE -ne 0) { throw "Unable to record installed-file ownership." }
    if (Test-Path $BackupRoot) {
        & $VenvPython $Ownership uninstall --root $BackupRoot
        if ($LASTEXITCODE -ne 0) { throw "Unable to remove the verified previous installation." }
    }
    [IO.File]::WriteAllText($Marker, "imprint-local:3.0.0`n", [Text.Encoding]::ASCII)
    $Succeeded = $true
    Write-Host "Imprint 3.0.0 installed. Data root: $DataRoot. No telemetry is enabled."
} catch {
    if (Test-Path $InstallRoot) { Remove-Item $InstallRoot -Recurse -Force }
    if (Test-Path $BackupRoot) { Move-Item $BackupRoot $InstallRoot }
    Restore-StateFile $Config "config"
    Restore-StateFile $Settings "settings"
    throw
} finally {
    Remove-Item $StateRoot -Recurse -Force -ErrorAction SilentlyContinue
}
