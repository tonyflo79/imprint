"""Private local-storage permissions for Imprint-owned state."""

from __future__ import annotations

import os
import json
import stat
import subprocess
from pathlib import Path

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def secure_directory(path: Path) -> Path:
    """Create or tighten an Imprint-owned directory without following links."""
    target = Path(path)
    if target.is_symlink():
        raise OSError(f"refusing symlinked private directory: {target}")
    target.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    if os.name != "nt":
        os.chmod(target, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
    return target


def secure_file(path: Path) -> Path:
    """Tighten an existing Imprint-owned regular file."""
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise OSError(f"private file is not a regular file: {target}")
    if os.name != "nt":
        os.chmod(target, PRIVATE_FILE_MODE, follow_symlinks=False)
    return target


def secure_tree(root: Path) -> None:
    """Tighten every existing item in an Imprint-owned state tree."""
    base = secure_directory(root)
    for current, directories, files in os.walk(base, followlinks=False):
        current_path = Path(current)
        secure_directory(current_path)
        for name in directories:
            secure_directory(current_path / name)
        for name in files:
            secure_file(current_path / name)


def unsafe_posix_permissions(root: Path) -> tuple[str, ...]:
    """Return content-free relative paths that are group/world accessible."""
    if os.name == "nt":
        return ()
    base = Path(root)
    if not base.exists():
        return ()
    unsafe: list[str] = []
    candidates = [base, *base.rglob("*")]
    for path in candidates:
        if path.is_symlink():
            unsafe.append(str(path.relative_to(base)) or ".")
            continue
        if not (path.is_dir() or path.is_file()):
            continue
        mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
        if mode & 0o077:
            unsafe.append(str(path.relative_to(base)) or ".")
    return tuple(sorted(set(unsafe)))


def unsafe_windows_permissions(root: Path) -> tuple[str, ...]:
    """Return paths granting read/write access beyond the user and SYSTEM."""
    if os.name != "nt":
        return ()
    base = Path(root)
    if not base.exists():
        return ()
    candidates = [base, *base.rglob("*")]
    unsafe = [
        str(path.relative_to(base)) or "."
        for path in candidates
        if path.is_symlink() or not (path.is_dir() or path.is_file())
    ]
    inspectable = [str(path) for path in candidates if path.is_dir() or path.is_file()]
    script = r"""
$ErrorActionPreference = 'Stop'
$paths = [Console]::In.ReadToEnd() | ConvertFrom-Json
$current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed = @($current, 'S-1-5-18')
$unsafe = @()
foreach ($path in $paths) {
  $item = Get-Item -Force -LiteralPath $path
  $acl = Get-Acl -LiteralPath $path
  $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
  if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $allowed -notcontains $owner) {
    $unsafe += $path
    continue
  }
  $rules = $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
  foreach ($rule in $rules) {
    if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
        $allowed -notcontains $rule.IdentityReference.Value) {
      $unsafe += $path
      break
    }
  }
}
ConvertTo-Json -Compress -InputObject @($unsafe)
"""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            input=json.dumps(inspectable),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            return (".",)
        reported = json.loads(result.stdout or "[]")
        if not isinstance(reported, list) or any(not isinstance(item, str) for item in reported):
            return (".",)
        unsafe.extend(str(Path(item).relative_to(base)) or "." for item in reported)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return (".",)
    return tuple(sorted(set(unsafe)))


def unsafe_private_permissions(root: Path) -> tuple[str, ...]:
    """Dispatch to the platform's fail-closed private-state permission scan."""
    return unsafe_windows_permissions(root) if os.name == "nt" else unsafe_posix_permissions(root)
