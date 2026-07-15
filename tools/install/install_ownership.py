#!/usr/bin/env python3
"""Record and enforce the exact files owned by an Imprint installation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath

MARKER = ".imprint-install-root"
MANIFEST = ".imprint-owned-files.json"
VERSION = "3.0.1"


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _root(value: str) -> Path:
    root = Path(value).expanduser().resolve(strict=True)
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise SystemExit(f"refusing unsafe install root: {root}")
    if root.is_symlink() or not root.is_dir():
        raise SystemExit(f"install root must be a real directory: {root}")
    return root


def _entry(path: Path, root: Path) -> dict[str, str]:
    relative = path.relative_to(root).as_posix()
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        return {"path": relative, "type": "symlink", "target": os.readlink(path)}
    if stat.S_ISREG(mode):
        return {"path": relative, "type": "file", "sha256": _digest(path)}
    if stat.S_ISDIR(mode):
        return {"path": relative, "type": "directory"}
    raise SystemExit(f"unsupported installed file type: {relative}")


def record(root: Path) -> None:
    entries = [
        _entry(path, root)
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        if path.name not in {MARKER, MANIFEST}
    ]
    payload = {"format": 1, "product": "imprint-local", "version": VERSION, "entries": entries}
    destination = root / MANIFEST
    temporary = root / f"{MANIFEST}.tmp"
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def _load(root: Path) -> list[dict[str, str]]:
    payload = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    if payload.get("format") != 1 or payload.get("product") != "imprint-local" or payload.get("version") != VERSION:
        raise SystemExit("ownership manifest identity mismatch")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise SystemExit("ownership manifest entries are invalid")
    return entries


def _is_runtime_cache(relative: str) -> bool:
    path = PurePosixPath(relative)
    return "__pycache__" in path.parts and (path.suffix == ".pyc" or path.name == "__pycache__")


def verify(root: Path) -> tuple[list[dict[str, str]], list[Path]]:
    marker = root / MARKER
    if not marker.is_file() or marker.read_text(encoding="ascii") != f"imprint-local:{VERSION}\n":
        raise SystemExit("ownership marker is missing or invalid")
    entries = _load(root)
    expected = {entry.get("path") for entry in entries}
    if None in expected or len(expected) != len(entries):
        raise SystemExit("ownership manifest contains missing or duplicate paths")
    actual_paths = [path for path in root.rglob("*") if path.name not in {MARKER, MANIFEST}]
    unrecorded = [path for path in actual_paths if path.relative_to(root).as_posix() not in expected]
    unexpected = [path for path in unrecorded if not _is_runtime_cache(path.relative_to(root).as_posix())]
    if unexpected:
        rendered = "\n".join(f"  {path.relative_to(root).as_posix()}" for path in unexpected[:25])
        raise SystemExit(f"refusing uninstall because unowned paths exist:\n{rendered}")
    for entry in entries:
        relative = entry["path"]
        path = root / PurePosixPath(relative)
        if not path.exists() and not path.is_symlink():
            raise SystemExit(f"owned path is missing: {relative}")
        actual = _entry(path, root)
        if actual != entry:
            raise SystemExit(f"owned path changed since installation: {relative}")
    return entries, [path for path in unrecorded if _is_runtime_cache(path.relative_to(root).as_posix())]


def uninstall(root: Path) -> None:
    entries, caches = verify(root)
    # Verification completes before the first mutation. Files/symlinks go first.
    for path in sorted(caches, key=lambda item: len(item.parts), reverse=True):
        if path.is_file() or path.is_symlink():
            path.unlink()
    for entry in reversed(entries):
        path = root / PurePosixPath(entry["path"])
        if entry["type"] in {"file", "symlink"}:
            path.unlink()
    for path in sorted(caches, key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            path.rmdir()
    for entry in sorted((item for item in entries if item["type"] == "directory"), key=lambda item: len(PurePosixPath(item["path"]).parts), reverse=True):
        (root / PurePosixPath(entry["path"])).rmdir()
    (root / MANIFEST).unlink()
    (root / MARKER).unlink()
    root.rmdir()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("record", "verify", "uninstall"))
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = _root(args.root)
    if args.action == "record":
        record(root)
    elif args.action == "verify":
        verify(root)
    else:
        uninstall(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
