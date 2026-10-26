"""Small, shared update primitives. No database, broker or runtime side effects."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


class UpdateError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def strict_semver(version: str) -> str:
    if not isinstance(version, str) or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        raise UpdateError("invalid_version", "只支持正式语义版本 X.Y.Z")
    if len(version) > 64:
        raise UpdateError("invalid_version", "版本号过长")
    return version


def compare_versions(first: str, second: str) -> int:
    a, b = (tuple(map(int, strict_semver(v).split("."))) for v in (first, second))
    return (a > b) - (a < b)


def validate_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or len(value) > 500 or any(c in value for c in '\\:<>"|?*') or any(ord(c) < 32 for c in value):
        raise UpdateError("unsafe_package_path", "更新包包含不安全路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise UpdateError("unsafe_package_path", "更新包包含不安全路径")
    reserved = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)
    if any(part.rstrip(". ") != part or reserved.match(part) for part in path.parts):
        raise UpdateError("unsafe_package_path", "更新包包含不安全路径")
    return path


def validate_release_path(value: str) -> PurePosixPath:
    """Keep mutable configuration, user data and credential files outside releases."""
    path = validate_relative_path(value)
    parts = [part.casefold() for part in path.parts]
    protected = {".git", ".runtime", "var", "data", "backups", ".venv", "venv", "node_modules", "__pycache__"}
    if any(part in protected for part in parts) or any(part.startswith(".env") and part != ".env.example" for part in parts) or "." + path.suffix.lstrip(".").casefold() in {".log", ".db", ".sqlite", ".sqlite3", ".pyc", ".pyo"} or "/".join(parts) == "config/update.yaml":
        raise UpdateError("protected_release_path", "更新包包含运行数据、私密配置或依赖缓存")
    return path


def atomic_write(path: Path | str, content: bytes | str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise UpdateError("unsafe_state_path", "更新状态路径不安全")
    data = content.encode("utf-8") if isinstance(content, str) else content
    handle, temporary = tempfile.mkstemp(prefix="." + target.name + ".", dir=target.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(handle, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _canonical(payload: dict) -> bytes:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise UpdateError("invalid_update_state", "更新状态内容格式错误") from None


def _signing_key(secret: str | bytes) -> bytes:
    value = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not isinstance(value, bytes) or len(value) < 32:
        raise UpdateError("invalid_update_secret", "更新签名密钥未正确配置", 503)
    return hmac.new(value, b"price-monitor/update-state/v1", hashlib.sha256).digest()


def sign_document(payload: dict, secret: str | bytes) -> dict:
    if not isinstance(payload, dict):
        raise UpdateError("invalid_update_state", "更新状态必须为对象")
    signature = hmac.new(_signing_key(secret), _canonical(payload), hashlib.sha256).hexdigest()
    return {"version": 1, "payload": payload, "signature": signature}


def verify_document(envelope: dict, secret: str | bytes) -> dict:
    if not isinstance(envelope, dict) or set(envelope) != {"version", "payload", "signature"} or type(envelope["version"]) is not int or envelope["version"] != 1 or not isinstance(envelope["payload"], dict) or not isinstance(envelope["signature"], str):
        raise UpdateError("invalid_update_signature", "更新状态签名格式错误")
    expected = sign_document(envelope["payload"], secret)["signature"]
    if not hmac.compare_digest(expected, envelope["signature"]):
        raise UpdateError("invalid_update_signature", "更新状态签名验证失败")
    return envelope["payload"]


def write_signed_json(path: Path | str, payload: dict, secret: str | bytes) -> None:
    atomic_write(path, _canonical(sign_document(payload, secret)))


def read_signed_json(path: Path | str, secret: str | bytes) -> dict:
    source = Path(path)
    try:
        if source.is_symlink() or source.stat().st_size > 16 * 1024 * 1024:
            raise UpdateError("invalid_update_state", "更新状态文件不安全或过大")
        envelope = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise UpdateError("invalid_update_state", "更新状态文件无法读取") from None
    return verify_document(envelope, secret)


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_REQUIREMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9][A-Za-z0-9_,.-]*\])?(?:\s*(?:===|==|!=|~=|>=|<=|>|<)\s*[A-Za-z0-9][A-Za-z0-9.*+!_-]*(?:\s*,\s*(?:===|==|!=|~=|>=|<=|>|<)\s*[A-Za-z0-9][A-Za-z0-9.*+!_-]*)*)?")


def validate_requirements_text(content: str) -> list[str]:
    errors = []
    if len(content) > 1024 * 1024:
        return ["依赖文件过大"]
    for number, raw in enumerate(content.splitlines(), 1):
        line = re.split(r"\s+#", raw.strip(), maxsplit=1)[0].strip()
        if line and not line.startswith("#") and not _REQUIREMENT.fullmatch(line):
            errors.append(f"第 {number} 行包含不支持的依赖格式")
    return errors


def validate_requirements(path: Path | str) -> None:
    try:
        errors = validate_requirements_text(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        raise UpdateError("invalid_requirements", "更新依赖文件无法读取") from None
    if errors:
        raise UpdateError("invalid_requirements", "更新包依赖仅允许普通包名和版本约束")


def validate_manifest(root: Path | str, expected_version: str | None = None, *, allow_unmanaged: bool = False) -> dict:
    root = Path(root).resolve()
    source = root / "release-manifest.json"
    try:
        if source.is_symlink() or source.stat().st_size > 8 * 1024 * 1024:
            raise UpdateError("invalid_manifest", "更新包清单过大或路径不安全")
        manifest = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise UpdateError("invalid_manifest", "更新包缺少有效清单") from None
    if not isinstance(manifest, dict) or manifest.get("product") != "price-monitor":
        raise UpdateError("invalid_manifest", "更新包产品标识错误")
    version = strict_semver(manifest.get("version"))
    if 'revision' in manifest and (not isinstance(manifest['revision'], str)
            or not re.fullmatch(r'[0-9a-f]{40}', manifest['revision']) or manifest['revision'] == '0' * 40):
        raise UpdateError('invalid_manifest', '更新包的 Git 提交标识无效')
    if expected_version is not None and version != strict_semver(expected_version):
        raise UpdateError("manifest_version_mismatch", "更新包版本与目标版本不一致")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files or len(files) > 25000:
        raise UpdateError("invalid_manifest", "更新包文件清单无效")
    normalized = set()
    for name, expected in files.items():
        relative = validate_release_path(name)
        folded = str(relative).casefold()
        if folded in normalized or name == "release-manifest.json" or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise UpdateError("invalid_manifest", "更新包文件校验记录无效")
        normalized.add(folded)
        path = root.joinpath(*relative.parts)
        if not path.is_relative_to(root) or any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file() or sha256_file(path) != expected:
            raise UpdateError("manifest_checksum_mismatch", "更新包文件校验失败")
    if not allow_unmanaged:
        actual = set()
        for path in root.rglob("*"):
            if path.is_symlink():
                raise UpdateError("unsafe_package_path", "更新包包含链接文件")
            if path.is_file() and path != source:
                actual.add(path.relative_to(root).as_posix())
        if actual != set(files):
            raise UpdateError("manifest_files_mismatch", "更新包文件与清单不一致")
    required = ("backend/app/version.py", "backend/requirements.txt")
    if any(name not in files for name in required):
        raise UpdateError("invalid_manifest", "更新包缺少必要应用文件")
    try:
        version_source = (root / "backend/app/version.py").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise UpdateError("invalid_manifest", "更新包版本文件无法读取") from None
    declarations = re.findall(r"^VERSION\s*=\s*['\"]([^'\"]+)['\"]\s*$", version_source, re.M)
    if declarations != [version]:
        raise UpdateError("manifest_version_mismatch", "更新包应用版本与清单不一致")
    validate_requirements(root / "backend/requirements.txt")
    return manifest


def safe_extract_tar(archive: Path | str, destination: Path | str, *, max_bytes: int = 2 * 1024 ** 3,
                     max_files: int = 25000, max_file_bytes: int = 512 * 1024 ** 2) -> dict:
    archive, destination = Path(archive), Path(destination)
    if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
        raise UpdateError("unsafe_extract_target", "更新包解压目录必须为空")
    try:
        with tarfile.open(archive, mode="r:gz") as package:
            members, seen, total = [], set(), 0
            for member in package:
                name = member.name.rstrip("/") if member.isdir() else member.name
                relative = validate_release_path(name)
                folded = str(relative).casefold()
                if folded in seen or len(seen) >= max_files:
                    raise UpdateError("invalid_package", "更新包包含重复路径或文件过多")
                seen.add(folded)
                if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE) or member.sparse is not None or member.size < 0 or member.size > max_file_bytes:
                    raise UpdateError("invalid_package", "更新包包含不允许的文件类型或超大文件")
                total += member.size
                if total > max_bytes:
                    raise UpdateError("package_too_large", "更新包解压大小超过限制")
                members.append((member, relative))
            if total > max(archive.stat().st_size * 1000, 1024 * 1024):
                raise UpdateError("package_too_large", "更新包压缩比例超过限制")
            destination.mkdir(parents=True, exist_ok=True)
            root = destination.resolve()
            for member, relative in members:
                target = root.joinpath(*relative.parts)
                if not target.is_relative_to(root) or any(part.is_symlink() for part in (target, *target.parents)):
                    raise UpdateError("unsafe_package_path", "更新包路径不安全")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = package.extractfile(member)
                    if source is None:
                        raise UpdateError("invalid_package", "更新包文件无法读取")
                    with source, target.open("xb") as output:
                        remaining = member.size
                        while remaining:
                            chunk = source.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise UpdateError("invalid_package", "更新包文件不完整")
                            output.write(chunk)
                            remaining -= len(chunk)
                    os.chmod(target, 0o755 if member.mode & 0o100 else 0o644)
    except UpdateError:
        raise
    except (OSError, EOFError, tarfile.TarError, ValueError):
        raise UpdateError("invalid_package", "更新包无法安全解压") from None
    return validate_manifest(destination)
