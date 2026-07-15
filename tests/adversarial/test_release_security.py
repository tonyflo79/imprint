from __future__ import annotations

import importlib.util
import io
import json
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_zip_symlink_is_rejected_by_verifier_and_extractor(tmp_path: Path) -> None:
    verifier = load("verify_artifacts_for_zip", "tools/release/verify_artifacts.py")
    extractor = load("extract_safe_for_zip", "tools/release/extract_safe.py")
    archive = tmp_path / "hostile.zip"
    with zipfile.ZipFile(archive, "w") as output:
        item = zipfile.ZipInfo("imprint-3.0.0/link")
        item.create_system = 3
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(item, "../../outside")
    with pytest.raises(RuntimeError, match="link or special"):
        verifier.inspect_zip(archive)
    with pytest.raises(RuntimeError, match="link or special"):
        extractor.extract_zip(archive, tmp_path / "zip-output")
    assert not (tmp_path / "outside").exists()


def test_tar_link_and_traversal_are_rejected(tmp_path: Path) -> None:
    extractor = load("extract_safe_for_tar", "tools/release/extract_safe.py")
    link_archive = tmp_path / "link.tar.gz"
    with tarfile.open(link_archive, "w:gz") as output:
        item = tarfile.TarInfo("imprint-3.0.0/link")
        item.type = tarfile.SYMTYPE
        item.linkname = "../../outside"
        output.addfile(item)
    with pytest.raises(RuntimeError, match="link or special"):
        extractor.extract_tar(link_archive, tmp_path / "tar-link-output")

    traversal_archive = tmp_path / "traversal.tar.gz"
    with tarfile.open(traversal_archive, "w:gz") as output:
        payload = b"escape"
        item = tarfile.TarInfo("../outside")
        item.size = len(payload)
        output.addfile(item, io.BytesIO(payload))
    with pytest.raises(RuntimeError, match="unsafe archive path"):
        extractor.extract_tar(traversal_archive, tmp_path / "tar-traversal-output")
    assert not (tmp_path / "outside").exists()


def test_ownership_manifest_refuses_unknown_or_mutated_files(tmp_path: Path) -> None:
    ownership = load("install_ownership_for_test", "tools/install/install_ownership.py")
    root = tmp_path / "install"
    root.mkdir()
    owned = root / "owned.txt"
    owned.write_text("original", encoding="utf-8")
    ownership.record(root)
    (root / ownership.MARKER).write_text("imprint-local:3.0.0\n", encoding="ascii")
    unknown = root / "unknown.txt"
    unknown.write_text("leave me", encoding="utf-8")
    with pytest.raises(SystemExit, match="unowned paths"):
        ownership.verify(root)
    assert unknown.read_text(encoding="utf-8") == "leave me"
    unknown.unlink()
    owned.write_text("changed", encoding="utf-8")
    with pytest.raises(SystemExit, match="changed since installation"):
        ownership.verify(root)
