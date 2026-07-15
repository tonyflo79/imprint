#!/usr/bin/env python3
"""Fail-closed checksum and archive-boundary validation."""

from __future__ import annotations

import hashlib
import stat
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

MAX_FILES = 1000
MAX_TOTAL = 50 * 1024 * 1024
MAX_MEMBER = 20 * 1024 * 1024


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def safe_name(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts


def inspect_zip(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = [item.filename for item in members]
        if len(names) > MAX_FILES or len(set(names)) != len(names):
            raise RuntimeError("ZIP member count or uniqueness check failed")
        if any(not safe_name(name) for name in names):
            raise RuntimeError("ZIP contains an unsafe path")
        for item in members:
            kind = stat.S_IFMT(item.external_attr >> 16)
            if kind not in {stat.S_IFREG, stat.S_IFDIR}:
                raise RuntimeError("ZIP contains a link or special file")
        if any(item.file_size > MAX_MEMBER for item in members) or sum(item.file_size for item in members) > MAX_TOTAL:
            raise RuntimeError("ZIP expansion limit exceeded")


def inspect_tar(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [item.name for item in members]
        if len(names) > MAX_FILES or len(set(names)) != len(names):
            raise RuntimeError("tar member count or uniqueness check failed")
        if any(not safe_name(name) for name in names):
            raise RuntimeError("tar contains an unsafe path")
        if any(item.issym() or item.islnk() or item.isdev() for item in members):
            raise RuntimeError("tar contains a link or device")
        if any(item.size > MAX_MEMBER for item in members) or sum(item.size for item in members) > MAX_TOTAL:
            raise RuntimeError("tar expansion limit exceeded")


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else "release-artifacts")
    expected = {}
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        wanted, name = line.split(None, 1)
        expected[name.strip()] = wanted
    for name, wanted in expected.items():
        path = root / name
        if not path.is_file() or digest(path) != wanted:
            raise RuntimeError(f"checksum failure: {name}")
        inspect_zip(path) if path.suffix == ".zip" else inspect_tar(path)
    if set(expected) != {"imprint-3.0.0.zip", "imprint-3.0.0.tar.gz"}:
        raise RuntimeError("checksum allowlist mismatch")
    print("artifact verification: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
