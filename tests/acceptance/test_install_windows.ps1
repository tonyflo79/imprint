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
New-Item -ItemType Directory -Force -Path $env:USERPROFILE | Out-Null
try {
    $Unowned = Join-Path $env:LOCALAPPDATA "Unowned App"
    New-Item -ItemType Directory -Force -Path $Unowned | Out-Null
    Set-Content (Join-Path $Unowned "sentinel.txt") "must-survive"
    $refused = $false
    try { & (Join-Path $ArtifactRoot "install\uninstall.ps1") -InstallRoot $Unowned -Config $Config -Settings $Settings } catch { $refused = $true }
    if (-not $refused -or -not (Test-Path (Join-Path $Unowned "sentinel.txt"))) { throw "Uninstaller accepted or damaged an unowned root." }
    $Wheel = Get-ChildItem (Join-Path $ArtifactRoot "dist") -Filter "imprint_local-3.0.0-*.whl" | Select-Object -First 1
    $ValidWheel = "$($Wheel.FullName).valid"
    Move-Item $Wheel.FullName $ValidWheel
    Set-Content $Wheel.FullName "not-a-wheel"
    $failed = $false
    try { & (Join-Path $ArtifactRoot "install\install.ps1") -InstallRoot $InstallRoot -Config $Config -Settings $Settings -DataRoot $Data } catch { $failed = $true }
    Remove-Item $Wheel.FullName -Force
    Move-Item $ValidWheel $Wheel.FullName
    if (-not $failed -or (Test-Path (Join-Path $InstallRoot ".imprint-install-root"))) { throw "Failed install left an ownership marker." }
    & (Join-Path $ArtifactRoot "install\install.ps1") -InstallRoot $InstallRoot -Config $Config -Settings $Settings -DataRoot $Data
    & (Join-Path $ArtifactRoot "install\install.ps1") -InstallRoot $InstallRoot -Config $Config -Settings $Settings -DataRoot $Data
    $Python = Join-Path $InstallRoot "venv\Scripts\python.exe"
    & $Python (Join-Path $ArtifactRoot "tests\acceptance\artifact_lifecycle.py") --data-root $Data --config $Config
    if ($LASTEXITCODE -ne 0) { throw "Synthetic lifecycle failed." }
    & $Python (Join-Path $InstallRoot "tools\manage_hooks.py") status --settings $Settings --python $Python --hooks-dir (Join-Path $InstallRoot "hooks")
    if ($LASTEXITCODE -ne 0) { throw "Hook idempotency check failed." }
    $Unknown = Join-Path $InstallRoot "unowned-sentinel.txt"
    Set-Content $Unknown "unowned"
    $refused = $false
    try { & (Join-Path $ArtifactRoot "install\uninstall.ps1") -InstallRoot $InstallRoot -Config $Config -Settings $Settings } catch { $refused = $true }
    if (-not $refused -or -not (Test-Path $Unknown) -or (Get-Content -Raw $Settings) -notmatch "imprint-local-managed-hook") { throw "Uninstaller did not fail closed on an unowned file." }
    Remove-Item $Unknown -Force
    & (Join-Path $ArtifactRoot "install\uninstall.ps1") -InstallRoot $InstallRoot -Config $Config -Settings $Settings
    if (Test-Path $InstallRoot) { throw "Application directory survived uninstall." }
    if (-not (Test-Path (Join-Path $Data "default\acceptance-data-sentinel.txt"))) { throw "Captured data was not preserved." }
    if ((Get-Content -Raw $Settings) -match "imprint-local-managed-hook") { throw "Managed hooks survived uninstall." }
    Write-Host "artifact lifecycle: PASS"
} finally {
    Remove-Item $TestRoot -Recurse -Force -ErrorAction SilentlyContinue
}
