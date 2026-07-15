#!/usr/bin/env python3
"""Fail-closed checksum and archive-boundary validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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


def archive_files(path: Path) -> dict[str, bytes]:
    """Return the exact regular-file payload, after the archive safety gate."""
    files: dict[str, bytes] = {}
    if path.suffix == ".zip":
        inspect_zip(path)
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                if not item.is_dir():
                    files[item.filename] = archive.read(item)
    else:
        inspect_tar(path)
        with tarfile.open(path, "r:gz") as archive:
            for item in archive.getmembers():
                if item.isreg():
                    source = archive.extractfile(item)
                    if source is None:
                        raise RuntimeError(f"tar member cannot be read: {item.name}")
                    files[item.name] = source.read()
    return files


def source_tree_digest(root: Path) -> str:
    """Recompute the builder's closed source-input digest from a checkout."""
    root = root.resolve(strict=True)
    allowlist_path = root / "release" / "allowlist.txt"
    values = [line.strip() for line in allowlist_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if values != sorted(values) or len(values) != len(set(values)):
        raise RuntimeError("source release allowlist is invalid")
    candidates = {
        root / ".gitignore",
        root / "pyproject.toml",
        root / "tools" / "release" / "package.py",
        *(root / value for value in values),
        *sorted((root / "src").rglob("*.py")),
    }
    if any(not path.is_file() for path in candidates):
        raise RuntimeError("source tree is missing a release input")
    digest_value = hashlib.sha256()
    for path in sorted(candidates, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        digest_value.update(relative + b"\0" + path.read_bytes() + b"\0")
    return digest_value.hexdigest()


def validate_provenance(
    files: dict[str, bytes], expected_revision: str | None,
    expected_source_digest: str | None = None,
) -> None:
    prefix = "imprint-3.0.1/"
    if not files or any(not name.startswith(prefix) for name in files):
        raise RuntimeError("archive top-level product/version boundary mismatch")
    provenance_name = prefix + "release/BUILD-PROVENANCE.json"
    try:
        provenance = json.loads(files[provenance_name].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("embedded build provenance is missing or invalid") from exc
    revision = provenance.get("source_revision")
    source_digest = provenance.get("source_tree_sha256")
    if (
        provenance.get("format") != 1
        or provenance.get("product") != "imprint-local"
        or provenance.get("version") != "3.0.1"
        or not isinstance(revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", revision) is None
        or not isinstance(source_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None
    ):
        raise RuntimeError("embedded build provenance identity is invalid")
    if expected_revision is not None and revision != expected_revision:
        raise RuntimeError("embedded source revision does not match expected revision")
    if expected_source_digest is not None and source_digest != expected_source_digest:
        raise RuntimeError("embedded source digest does not match expected source tree")
    distributions = provenance.get("python_distributions")
    if not isinstance(distributions, list):
        raise RuntimeError("embedded Python distribution provenance is invalid")
    expected_dist: dict[str, tuple[str, int]] = {}
    for item in distributions:
        if not isinstance(item, dict) or set(item) != {"fileName", "sha256", "size"}:
            raise RuntimeError("embedded Python distribution entry is invalid")
        name, wanted, size = item["fileName"], item["sha256"], item["size"]
        if (
            not isinstance(name, str)
            or PurePosixPath(name).name != name
            or not isinstance(wanted, str)
            or re.fullmatch(r"[0-9a-f]{64}", wanted) is None
            or not isinstance(size, int)
            or size < 0
            or name in expected_dist
        ):
            raise RuntimeError("embedded Python distribution entry is invalid")
        expected_dist[name] = (wanted, size)
    actual_dist = {
        name.removeprefix(prefix + "dist/"): content
        for name, content in files.items()
        if name.startswith(prefix + "dist/")
    }
    if set(expected_dist) != {
        "imprint_local-3.0.1-py3-none-any.whl",
        "imprint_local-3.0.1.tar.gz",
    }:
        raise RuntimeError("embedded Python distribution allowlist mismatch")
    if set(actual_dist) != set(expected_dist):
        raise RuntimeError("embedded Python distribution manifest mismatch")
    for name, content in actual_dist.items():
        wanted, size = expected_dist[name]
        if len(content) != size or hashlib.sha256(content).hexdigest() != wanted:
            raise RuntimeError(f"embedded Python distribution digest mismatch: {name}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default="release-artifacts")
    parser.add_argument("--expected-revision")
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args(argv[1:])
    root = Path(args.root)
    expected = {}
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 2 or re.fullmatch(r"[0-9a-f]{64}", parts[0]) is None or parts[1] in expected:
            raise RuntimeError("checksum manifest is malformed or contains duplicates")
        expected[parts[1]] = parts[0]
    archive_payloads: dict[str, dict[str, bytes]] = {}
    for name, wanted in expected.items():
        path = root / name
        if not path.is_file() or digest(path) != wanted:
            raise RuntimeError(f"checksum failure: {name}")
        archive_payloads[name] = archive_files(path)
    if set(expected) != {"imprint-3.0.1.zip", "imprint-3.0.1.tar.gz"}:
        raise RuntimeError("checksum allowlist mismatch")
    zip_files = archive_payloads["imprint-3.0.1.zip"]
    tar_files = archive_payloads["imprint-3.0.1.tar.gz"]
    if zip_files != tar_files:
        raise RuntimeError("ZIP and tar release payloads are not byte-equivalent")
    expected_source_digest = source_tree_digest(args.source_root) if args.source_root else None
    validate_provenance(zip_files, args.expected_revision, expected_source_digest)
    print("artifact verification: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
