from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile

import pytest

from app.update_common import (UpdateError, atomic_write, compare_versions, read_signed_json,
                               safe_extract_tar, sign_document, strict_semver, validate_manifest,
                               validate_relative_path, validate_requirements_text, verify_document,
                               validate_release_path, write_signed_json)


def release_files(version="1.2.0"):
    return {"backend/app/version.py": f'VERSION = "{version}"\n'.encode(),
            "backend/requirements.txt": b"httpx==0.28.1\nuvicorn[standard]>=0.50.0,<1\n"}


def package(tmp_path, files=None, members=None, manifest_version="1.2.0"):
    files = release_files() if files is None else files
    manifest = {"product": "price-monitor", "version": manifest_version,
                "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
    archive = tmp_path / "package.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for name, content in {**files, "release-manifest.json": json.dumps(manifest).encode()}.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            output.addfile(info, io.BytesIO(content))
        for info, content in members or []:
            output.addfile(info, io.BytesIO(content) if content else None)
    return archive


def test_versions_compare_numerically_and_require_stable_tags():
    assert strict_semver("1.10.0") == "1.10.0"
    assert compare_versions("1.10.0", "1.9.0") == 1
    assert compare_versions("1.1.0", "2.0.0") == -1
    assert compare_versions("1.1.0", "1.1.0") == 0


@pytest.mark.parametrize("value", ["v1.0.0", "1.0", "01.0.0", "1.0.0-rc1", "1.0.0+build", "1.0.0 ", None, 1])
def test_reject_unofficial_or_ambiguous_versions(value):
    with pytest.raises(UpdateError):
        strict_semver(value)


@pytest.mark.parametrize("value", ["../escape", "/escape", "a/../escape", "a//b", "a/./b", "C:/escape", "a\\b", "a/nul.txt", "a/COM1", "a/b.", "a/b ", "a/\x00b"])
def test_reject_cross_platform_unsafe_paths(value):
    with pytest.raises(UpdateError):
        validate_relative_path(value)


def test_signed_documents_detect_tampering_and_wrong_context(tmp_path):
    path = tmp_path / "nested" / "job.json"
    secret = "a" * 32
    payload = {"run_id": 12, "operation": "apply", "name": "更新"}
    write_signed_json(path, payload, secret)
    assert read_signed_json(path, secret) == payload
    envelope = sign_document(payload, secret)
    envelope["payload"]["run_id"] = 13
    with pytest.raises(UpdateError, match="签名"):
        verify_document(envelope, secret)
    with pytest.raises(UpdateError):
        read_signed_json(path, "b" * 32)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_atomic_write_replaces_whole_document(tmp_path):
    path = tmp_path / "state"
    atomic_write(path, "old")
    atomic_write(path, b"new")
    assert path.read_bytes() == b"new"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("text", [
    "-r other.txt", "-e .", "--index-url https://example.org", "thing @ https://example.org/a.whl",
    "git+https://example.org/repo", "./local.whl", "C:/local.whl", "thing==1.0 --extra-index-url https://example.org",
    "thing==1.0; python_version>'3'", "thing==1.0\\\n--find-links /local",
])
def test_dependency_validation_refuses_executable_or_network_overrides(text):
    assert validate_requirements_text(text)


def test_ordinary_requirements_allow_extras_versions_and_comments():
    assert validate_requirements_text("# note\nhttpx==0.28.1\ncelery[redis]>=5.6.3,<6 # checked\nPyYAML\n") == []


def test_safe_extract_validates_all_checksums_and_version(tmp_path):
    archive = package(tmp_path)
    root = tmp_path / "extracted"
    manifest = safe_extract_tar(archive, root)
    assert manifest["version"] == "1.2.0"
    assert validate_manifest(root, "1.2.0") == manifest
    (root / "backend/app/version.py").write_text('VERSION = "1.2.1"')
    with pytest.raises(UpdateError) as error:
        validate_manifest(root)
    assert error.value.code == "manifest_checksum_mismatch"


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE])
def test_safe_extract_rejects_all_links_and_devices(tmp_path, kind):
    info = tarfile.TarInfo("danger")
    info.type = kind
    info.linkname = "../../outside"
    archive = package(tmp_path, members=[(info, None)])
    with pytest.raises(UpdateError):
        safe_extract_tar(archive, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_safe_extract_rejects_traversal_before_writing(tmp_path):
    info = tarfile.TarInfo("../escape")
    info.size = 4
    archive = package(tmp_path, members=[(info, b"oops")])
    with pytest.raises(UpdateError):
        safe_extract_tar(archive, tmp_path / "output")
    assert not (tmp_path / "escape").exists()


def test_safe_extract_rejects_case_collisions_and_size_limit(tmp_path):
    info = tarfile.TarInfo("backend/APP/version.py")
    info.size = 4
    archive = package(tmp_path, members=[(info, b"oops")])
    with pytest.raises(UpdateError):
        safe_extract_tar(archive, tmp_path / "output")
    archive = package(tmp_path)
    with pytest.raises(UpdateError):
        safe_extract_tar(archive, tmp_path / "small", max_bytes=20)


def test_manifest_rejects_unlisted_files_and_version_mismatch(tmp_path):
    archive = package(tmp_path)
    root = tmp_path / "output"
    safe_extract_tar(archive, root)
    with pytest.raises(UpdateError) as mismatch:
        validate_manifest(root, "1.3.0")
    assert mismatch.value.code == "manifest_version_mismatch"
    (root / "extra.txt").write_text("not listed")
    with pytest.raises(UpdateError) as extra:
        validate_manifest(root)
    assert extra.value.code == "manifest_files_mismatch"


def test_manifest_rejects_package_version_disagreement(tmp_path):
    archive = package(tmp_path, manifest_version="1.3.0")
    with pytest.raises(UpdateError) as error:
        safe_extract_tar(archive, tmp_path / "output")
    assert error.value.code == "manifest_version_mismatch"


@pytest.mark.parametrize("name", [".env", ".env.production", "backend/.env", "var/session.json", "data/session.json",
                                  ".runtime/session.json", "config/update.yaml", ".git/config", "frontend/node_modules/x.js",
                                  "backend/__pycache__/code.pyc", "logs/server.log", "backend/account.db"])
def test_release_refuses_privacy_and_mutable_data_paths(name):
    with pytest.raises(UpdateError) as error:
        validate_release_path(name)
    assert error.value.code == "protected_release_path"


def test_release_accepts_public_example_configuration_and_frontend_build():
    for name in (".env.example", "config/update.example.yaml", "frontend/dist/assets/index-abc.js", "docker-compose.yml"):
        assert str(validate_release_path(name)) == name


def test_installed_manifest_can_ignore_unmanaged_data_without_reading_it(tmp_path):
    archive = package(tmp_path)
    root = tmp_path / "installed"
    safe_extract_tar(archive, root)
    (root / ".env").write_text("fixture-private-environment", encoding="utf-8")
    runtime = root / "data" / "session"
    runtime.mkdir(parents=True)
    (runtime / "sensitive.bin").write_bytes(b"fixture-session-value")
    before = (root / ".env").read_bytes(), (runtime / "sensitive.bin").read_bytes()
    manifest = validate_manifest(root, "1.2.0", allow_unmanaged=True)
    assert manifest["version"] == "1.2.0"
    assert ((root / ".env").read_bytes(), (runtime / "sensitive.bin").read_bytes()) == before
    with pytest.raises(UpdateError) as exact:
        validate_manifest(root, "1.2.0")
    assert exact.value.code == "manifest_files_mismatch"
    (root / "backend/app/version.py").write_text('VERSION = "9.9.9"', encoding="utf-8")
    with pytest.raises(UpdateError) as tampered:
        validate_manifest(root, "1.2.0", allow_unmanaged=True)
    assert tampered.value.code == "manifest_checksum_mismatch"
