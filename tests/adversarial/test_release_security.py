from __future__ import annotations

import importlib.util
import hashlib
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
        item = zipfile.ZipInfo("imprint-3.0.1/link")
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
        item = tarfile.TarInfo("imprint-3.0.1/link")
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
    (root / ownership.MARKER).write_text("imprint-local:3.0.1\n", encoding="ascii")
    unknown = root / "unknown.txt"
    unknown.write_text("leave me", encoding="utf-8")
    with pytest.raises(SystemExit, match="unowned paths"):
        ownership.verify(root)
    assert unknown.read_text(encoding="utf-8") == "leave me"
    unknown.unlink()
    owned.write_text("changed", encoding="utf-8")
    with pytest.raises(SystemExit, match="changed since installation"):
        ownership.verify(root)


def test_ownership_manifest_ignores_and_removes_runtime_bytecode(tmp_path: Path) -> None:
    ownership = load("install_ownership_for_runtime_cache", "tools/install/install_ownership.py")
    root = tmp_path / "install"
    cache = root / "hooks" / "__pycache__"
    cache.mkdir(parents=True)
    (root / "hooks" / "bridge.py").write_text("pass\n", encoding="utf-8")
    bytecode = cache / "bridge.cpython-314.pyc"
    bytecode.write_bytes(b"before")
    ownership.record(root)
    (root / ownership.MARKER).write_text("imprint-local:3.0.1\n", encoding="ascii")

    entries = json.loads((root / ownership.MANIFEST).read_text(encoding="utf-8"))["entries"]
    assert not any("__pycache__" in entry["path"] for entry in entries)
    bytecode.write_bytes(b"after ordinary hook execution")
    ownership.uninstall(root)
    assert not root.exists()


def test_ownership_tool_accepts_only_closed_upgrade_versions(tmp_path: Path) -> None:
    ownership = load("install_ownership_for_upgrade", "tools/install/install_ownership.py")
    root = tmp_path / "legacy-install"
    root.mkdir()
    (root / "owned.txt").write_text("legacy", encoding="utf-8")
    ownership.record(root)
    manifest = root / ownership.MANIFEST
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["version"] = "3.0.0"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    (root / ownership.MARKER).write_text("imprint-local:3.0.0\n", encoding="ascii")
    ownership.verify(root, "3.0.0")
    with pytest.raises(SystemExit, match="unsupported install ownership version"):
        ownership.verify(root, "2.9.9")


def test_v300_recorded_runtime_bytecode_may_change_before_upgrade(tmp_path: Path) -> None:
    ownership = load("install_ownership_v300_cache", "tools/install/install_ownership.py")
    root = tmp_path / "install"
    cache = root / "hooks" / "__pycache__"
    cache.mkdir(parents=True)
    source = root / "hooks" / "bridge.py"
    source.write_text("pass\n", encoding="utf-8")
    bytecode = cache / "bridge.cpython-311.pyc"
    bytecode.write_bytes(b"recorded-v300-cache")
    ownership.record(root)
    manifest_path = root / ownership.MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "3.0.0"
    manifest["entries"].extend([
        ownership._entry(cache, root),
        ownership._entry(bytecode, root),
    ])
    manifest["entries"].sort(key=lambda item: item["path"])
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / ownership.MARKER).write_text("imprint-local:3.0.0\n", encoding="ascii")

    bytecode.write_bytes(b"changed-by-ordinary-hook-use")
    ownership.uninstall(root, "3.0.0")
    assert not root.exists()


def test_embedded_provenance_validates_revision_and_dist_hashes() -> None:
    verifier = load("verify_artifacts_for_provenance", "tools/release/verify_artifacts.py")
    wheel = b"wheel-bytes"
    sdist = b"sdist-bytes"
    revision = "a" * 40
    provenance = {
        "format": 1,
        "product": "imprint-local",
        "version": "3.0.1",
        "source_revision": revision,
        "source_tree_sha256": "b" * 64,
        "python_distributions": [
            {
                "fileName": "imprint_local-3.0.1-py3-none-any.whl",
                "sha256": hashlib.sha256(wheel).hexdigest(),
                "size": len(wheel),
            },
            {
                "fileName": "imprint_local-3.0.1.tar.gz",
                "sha256": hashlib.sha256(sdist).hexdigest(),
                "size": len(sdist),
            },
        ],
    }
    files = {
        "imprint-3.0.1/dist/imprint_local-3.0.1-py3-none-any.whl": wheel,
        "imprint-3.0.1/dist/imprint_local-3.0.1.tar.gz": sdist,
        "imprint-3.0.1/release/BUILD-PROVENANCE.json": json.dumps(provenance).encode(),
    }
    verifier.validate_provenance(files, revision, "b" * 64)
    with pytest.raises(RuntimeError, match="expected revision"):
        verifier.validate_provenance(files, "c" * 40)
    with pytest.raises(RuntimeError, match="source digest"):
        verifier.validate_provenance(files, revision, "c" * 64)
    files["imprint-3.0.1/dist/imprint_local-3.0.1-py3-none-any.whl"] = b"tampered"
    with pytest.raises(RuntimeError, match="digest mismatch"):
        verifier.validate_provenance(files, revision)


def test_windows_uninstaller_stages_cleanup_outside_owned_venv() -> None:
    script = (ROOT / "install" / "uninstall.ps1").read_text(encoding="utf-8")
    assert "sys._base_executable" in script
    assert "cleanup interpreter is inside the owned install root" in script
    external_verify = script.index("& $BasePython -I -S $StagedOwnership verify --root $InstallRoot")
    unregister = script.index("$Manager unregister")
    uninstall = script.index("& $BasePython -I -S $StagedOwnership uninstall --root $InstallRoot")
    assert external_verify < unregister < uninstall
    assert "& $Python $Ownership uninstall --root $InstallRoot" not in script


def test_windows_installer_sets_private_owner_before_acl_grants() -> None:
    script = (ROOT / "install" / "install.ps1").read_text(encoding="utf-8")
    owner = script.index('/setowner "*$Sid"')
    grants = script.index("/inheritance:r /grant:r")
    assert owner < grants


def test_windows_acl_inspection_is_utf8_and_fail_closed() -> None:
    script = (ROOT / "src" / "imprint" / "permissions.py").read_text(encoding="utf-8")
    assert 'shutil.which("pwsh.exe") or shutil.which("powershell.exe")' in script
    assert '[Console]::InputEncoding = $utf8' in script
    assert '[Console]::OutputEncoding = $utf8' in script
    assert 'encoding="utf-8"' in script
    assert 'return ("<acl-inspection-failed>",)' in script


def test_release_provenance_covers_every_shipped_and_build_input() -> None:
    package = load("package_for_provenance_test", "tools/release/package.py")
    verifier = load("verify_source_tree_digest", "tools/release/verify_artifacts.py")
    allowlist = verifier.git_allowlist(ROOT, "HEAD")
    relative = {path.relative_to(ROOT).as_posix() for path in package.release_inputs(allowlist)}
    assert set(allowlist) <= relative
    assert {".gitignore", "pyproject.toml", "tools/release/package.py"} <= relative
    assert {path.relative_to(ROOT).as_posix() for path in (ROOT / "src").rglob("*.py")} <= relative
    script = (ROOT / "tools" / "release" / "package.py").read_text(encoding="utf-8")
    assert "refusing a release build from a dirty worktree" in script
    assert verifier.source_tree_digest(ROOT) == package.source_digest(allowlist)


def _git_bound_release_files(verifier) -> dict[str, bytes]:
    prefix = "imprint-3.0.1/"
    files = {
        prefix + relative: verifier.git_blob(ROOT, "HEAD", relative)
        for relative in verifier.git_allowlist(ROOT, "HEAD")
    }
    sources = {
        relative.removeprefix("src/"): verifier.git_blob(ROOT, "HEAD", relative)
        for relative in verifier.git_source_paths(ROOT, "HEAD")
    }
    wheel_output = io.BytesIO()
    dist_info = "imprint_local-3.0.1.dist-info/"
    with zipfile.ZipFile(wheel_output, "w") as wheel:
        def add(name: str, content: bytes | str) -> None:
            item = zipfile.ZipInfo(name)
            item.create_system = 3
            item.external_attr = (stat.S_IFREG | 0o644) << 16
            wheel.writestr(item, content)
        for name, content in sources.items():
            add(name, content)
        add(dist_info + "licenses/LICENSE", verifier.git_blob(ROOT, "HEAD", "LICENSE"))
        add(dist_info + "METADATA", "Metadata-Version: 2.4\nName: imprint-local\nVersion: 3.0.1\nRequires-Python: >=3.10\n")
        add(dist_info + "WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        add(dist_info + "entry_points.txt", "[console_scripts]\nimprint = imprint.cli:main\n")
        add(dist_info + "top_level.txt", "imprint\n")
        add(dist_info + "RECORD", "")
    files[prefix + "dist/imprint_local-3.0.1-py3-none-any.whl"] = wheel_output.getvalue()

    sdist_output = io.BytesIO()
    sdist_prefix = "imprint_local-3.0.1/"
    sdist_payload = {
        **{sdist_prefix + "src/" + name: content for name, content in sources.items()},
        **{sdist_prefix + name: verifier.git_blob(ROOT, "HEAD", name) for name in ("LICENSE", "README.md", "pyproject.toml")},
        sdist_prefix + "PKG-INFO": b"Metadata-Version: 2.4\nName: imprint-local\nVersion: 3.0.1\nRequires-Python: >=3.10\n",
        sdist_prefix + "setup.cfg": b"[egg_info]\ntag_build = \ntag_date = 0\n\n",
    }
    for name in ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt", "requires.txt", "top_level.txt"):
        sdist_payload[sdist_prefix + "src/imprint_local.egg-info/" + name] = b""
    with tarfile.open(fileobj=sdist_output, mode="w:gz") as sdist:
        for name, content in sorted(sdist_payload.items()):
            item = tarfile.TarInfo(name)
            item.size = len(content)
            sdist.addfile(item, io.BytesIO(content))
    files[prefix + "dist/imprint_local-3.0.1.tar.gz"] = sdist_output.getvalue()
    files[prefix + "release/BUILD-PROVENANCE.json"] = b"{}"
    files[prefix + "release/SBOM.spdx.json"] = b"{}"
    return files


def test_git_binding_accepts_exact_independent_source_payloads() -> None:
    verifier = load("verify_git_bound_baseline", "tools/release/verify_artifacts.py")
    verifier.validate_source_bindings(_git_bound_release_files(verifier), ROOT, "HEAD")


@pytest.mark.parametrize("relative", ["hooks/_bridge.py", "install/install.sh"])
def test_git_binding_rejects_mutated_public_hook_or_installer(relative: str) -> None:
    verifier = load("verify_git_bound_outer", "tools/release/verify_artifacts.py")
    files = _git_bound_release_files(verifier)
    files["imprint-3.0.1/" + relative] += b"\nmalicious mutation\n"
    with pytest.raises(RuntimeError, match="differs from Git blob"):
        verifier.validate_source_bindings(files, ROOT, "HEAD")


def test_git_binding_rejects_mutated_wheel_python_payload() -> None:
    verifier = load("verify_git_bound_wheel", "tools/release/verify_artifacts.py")
    files = _git_bound_release_files(verifier)
    key = "imprint-3.0.1/dist/imprint_local-3.0.1-py3-none-any.whl"
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(files[key])) as source, zipfile.ZipFile(output, "w") as target:
        for item in source.infolist():
            content = source.read(item)
            if item.filename == "imprint/backup.py":
                content += b"\n# malicious wheel mutation\n"
            target.writestr(item, content)
    files[key] = output.getvalue()
    with pytest.raises(RuntimeError, match="wheel Python payload"):
        verifier.validate_source_bindings(files, ROOT, "HEAD")


def test_git_binding_rejects_mutated_sdist_python_payload() -> None:
    verifier = load("verify_git_bound_sdist", "tools/release/verify_artifacts.py")
    files = _git_bound_release_files(verifier)
    key = "imprint-3.0.1/dist/imprint_local-3.0.1.tar.gz"
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(files[key]), mode="r:gz") as source:
        records = []
        for item in source.getmembers():
            extracted = source.extractfile(item) if item.isfile() else None
            content = extracted.read() if extracted is not None else None
            if item.name.endswith("/src/imprint/backup.py"):
                content = (content or b"") + b"\n# malicious sdist mutation\n"
                item.size = len(content)
            records.append((item, content))
    with tarfile.open(fileobj=output, mode="w:gz") as target:
        for item, content in records:
            target.addfile(item, io.BytesIO(content) if content is not None else None)
    files[key] = output.getvalue()
    with pytest.raises(RuntimeError, match="sdist payload"):
        verifier.validate_source_bindings(files, ROOT, "HEAD")
