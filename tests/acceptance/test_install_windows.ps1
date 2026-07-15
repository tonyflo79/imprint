$ErrorActionPreference = "Stop"
$ArtifactRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$TestRoot = Join-Path ([IO.Path]::GetTempPath()) ("imprint acceptance " + [guid]::NewGuid())
$env:USERPROFILE = Join-Path $TestRoot "Empty Home"
$env:APPDATA = Join-Path $env:USERPROFILE "App Data"
$env:LOCALAPPDATA = Join-Path $env:USERPROFILE "Local App Data"
$InstallRoot = Join-Path $env:LOCALAPPDATA "Imprint App\app"
$Config = Join-Path $env:APPDATA "Imprint\config.json"
$Settings = Join-Path $env:USERPROFILE ".claude\settings.json"
$Data = Join-Path $env:LOCALAPPDATA "Imprint Data"
    $Launcher = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\imprint.cmd"
    New-Item -ItemType Directory -Force -Path $env:USERPROFILE | Out-Null
try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Config), $Data | Out-Null
    $ConfigAclBefore = (Get-Acl (Split-Path -Parent $Config)).Sddl
    $DataAclBefore = (Get-Acl $Data).Sddl
    $Unowned = Join-Path $env:LOCALAPPDATA "Unowned App"
    New-Item -ItemType Directory -Force -Path $Unowned | Out-Null
    Set-Content (Join-Path $Unowned "sentinel.txt") "must-survive"
    $refused = $false
    try { & (Join-Path $ArtifactRoot "install\uninstall.ps1") -InstallRoot $Unowned -Config $Config -Settings $Settings } catch { $refused = $true }
    if (-not $refused -or -not (Test-Path (Join-Path $Unowned "sentinel.txt"))) { throw "Uninstaller accepted or damaged an unowned root." }
    $Wheel = Get-ChildItem (Join-Path $ArtifactRoot "dist") -Filter "imprint_local-3.0.1-*.whl" | Select-Object -First 1
    $ValidWheel = "$($Wheel.FullName).valid"
    Move-Item $Wheel.FullName $ValidWheel
    Set-Content $Wheel.FullName "not-a-wheel"
    $failed = $false
    try { & (Join-Path $ArtifactRoot "install\install.ps1") -InstallRoot $InstallRoot -Config $Config -Settings $Settings -DataRoot $Data } catch { $failed = $true }
    Remove-Item $Wheel.FullName -Force
    Move-Item $ValidWheel $Wheel.FullName
    if (-not $failed -or (Test-Path (Join-Path $InstallRoot ".imprint-install-root"))) { throw "Failed install left an ownership marker." }
    if ((Get-Acl (Split-Path -Parent $Config)).Sddl -ne $ConfigAclBefore -or (Get-Acl $Data).Sddl -ne $DataAclBefore) { throw "Failed install did not restore external ACLs." }
    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    Set-Content -Encoding ascii (Join-Path $InstallRoot "legacy-owned.txt") "legacy"
    & python (Join-Path $ArtifactRoot "tools\install\install_ownership.py") record --root $InstallRoot
    $LegacyManifest = Join-Path $InstallRoot ".imprint-owned-files.json"
    $LegacyValue = Get-Content -Raw $LegacyManifest | ConvertFrom-Json -AsHashtable
    $LegacyValue["version"] = "3.0.0"
    $LegacyValue | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 $LegacyManifest
    [IO.File]::WriteAllText((Join-Path $InstallRoot ".imprint-install-root"), "imprint-local:3.0.0`n", [Text.Encoding]::ASCII)
    & (Join-Path $ArtifactRoot "install\install.ps1") -InstallRoot $InstallRoot -Config $Config -Settings $Settings -DataRoot $Data
    if (Test-Path (Join-Path $InstallRoot "legacy-owned.txt")) { throw "3.0.0 owned application survived upgrade." }
    & (Join-Path $ArtifactRoot "install\install.ps1") -InstallRoot $InstallRoot -Config $Config -Settings $Settings -DataRoot $Data
    $Version = & $Launcher version
    if ($LASTEXITCODE -ne 0 -or $Version -ne "3.0.1") { throw "Owned launcher was not callable." }
    & $Launcher --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Owned launcher help failed." }
    $BackupPattern = (Split-Path -Leaf $InstallRoot) + ".imprint-backup.*"
    if (@(Get-ChildItem (Split-Path -Parent $InstallRoot) -Filter $BackupPattern -Force).Count -ne 0) { throw "Reinstall left a stale backup." }
    $Python = Join-Path $InstallRoot "venv\Scripts\python.exe"
    & $Python (Join-Path $ArtifactRoot "tests\acceptance\artifact_lifecycle.py") --data-root $Data --config $Config
    if ($LASTEXITCODE -ne 0) { throw "Synthetic lifecycle failed." }
    & $Python (Join-Path $InstallRoot "tools\manage_hooks.py") status --settings $Settings --python $Python --hooks-dir (Join-Path $InstallRoot "hooks")
    if ($LASTEXITCODE -ne 0) { throw "Hook idempotency check failed." }
    $Junction = Join-Path $env:LOCALAPPDATA "Imprint Junction"
    New-Item -ItemType Junction -Path $Junction -Target $InstallRoot | Out-Null
    $refused = $false
    try { & (Join-Path $ArtifactRoot "install\uninstall.ps1") -InstallRoot $Junction -Config $Config -Settings $Settings } catch { $refused = $true }
    Remove-Item $Junction -Force
    if (-not $refused -or -not (Test-Path $InstallRoot)) { throw "Uninstaller accepted or damaged a reparse-point root." }
    $Unknown = Join-Path $InstallRoot "unowned-sentinel.txt"
    Set-Content $Unknown "unowned"
    $SettingsHash = (Get-FileHash $Settings -Algorithm SHA256).Hash
    $MarkerHash = (Get-FileHash (Join-Path $InstallRoot ".imprint-install-root") -Algorithm SHA256).Hash
    $ManifestHash = (Get-FileHash (Join-Path $InstallRoot ".imprint-owned-files.json") -Algorithm SHA256).Hash
    $refused = $false
    try { & (Join-Path $ArtifactRoot "install\uninstall.ps1") -InstallRoot $InstallRoot -Config $Config -Settings $Settings } catch { $refused = $true }
    if (-not $refused -or -not (Test-Path $Unknown) -or (Get-Content -Raw $Settings) -notmatch "imprint-local-managed-hook") { throw "Uninstaller did not fail closed on an unowned file." }
    if ((Get-FileHash $Settings -Algorithm SHA256).Hash -ne $SettingsHash -or (Get-FileHash (Join-Path $InstallRoot ".imprint-install-root") -Algorithm SHA256).Hash -ne $MarkerHash -or (Get-FileHash (Join-Path $InstallRoot ".imprint-owned-files.json") -Algorithm SHA256).Hash -ne $ManifestHash) { throw "Failed unowned-file refusal mutated installation state." }
    Remove-Item $Unknown -Force
    $OwnedTool = Join-Path $InstallRoot "tools\manage_hooks.py"
    $OwnedBytes = [IO.File]::ReadAllBytes($OwnedTool)
    Add-Content $OwnedTool "# mutation"
    $refused = $false
    try { & (Join-Path $ArtifactRoot "install\uninstall.ps1") -InstallRoot $InstallRoot -Config $Config -Settings $Settings } catch { $refused = $true }
    if (-not $refused -or (Get-Content -Raw $Settings) -notmatch "imprint-local-managed-hook") { throw "Uninstaller did not fail closed on a mutated owned file." }
    [IO.File]::WriteAllBytes($OwnedTool, $OwnedBytes)
    & (Join-Path $ArtifactRoot "install\uninstall.ps1") -InstallRoot $InstallRoot -Config $Config -Settings $Settings
    if (Test-Path $InstallRoot) { throw "Application directory survived uninstall." }
    if (Test-Path $Launcher) { throw "Owned launcher survived uninstall." }
    if (-not (Test-Path (Join-Path $Data "default\acceptance-data-sentinel.txt"))) { throw "Captured data was not preserved." }
    if ((Get-Content -Raw $Settings) -match "imprint-local-managed-hook") { throw "Managed hooks survived uninstall." }
    if (-not (Test-Path $Config)) { throw "Default uninstall removed configuration." }
    & (Join-Path $ArtifactRoot "install\install.ps1") -InstallRoot $InstallRoot -Config $Config -Settings $Settings -DataRoot $Data
    Set-Content -Encoding ascii $Launcher "@echo off`r`necho unowned`r`n"
    & (Join-Path $ArtifactRoot "install\uninstall.ps1") -InstallRoot $InstallRoot -Config $Config -Settings $Settings -PurgeConfig
    if ((Test-Path $Config) -or (Test-Path $InstallRoot)) { throw "Purge-config uninstall left code or configuration." }
    if (-not (Test-Path $Launcher) -or (& $Launcher) -ne "unowned") { throw "Uninstall removed or changed an unowned launcher." }
    Remove-Item $Launcher -Force
    if (-not (Test-Path (Join-Path $Data "default\acceptance-data-sentinel.txt"))) { throw "Purge-config uninstall removed captured data." }
    Write-Host "artifact lifecycle: PASS"
} finally {
    Remove-Item $TestRoot -Recurse -Force -ErrorAction SilentlyContinue
}
