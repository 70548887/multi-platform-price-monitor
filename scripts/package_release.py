"""Build an allowlisted, reproducible release without local state or Git history.

This is deliberately a file allowlist, not a copy of the working tree followed
by exclusions. ``frontend/dist`` is the only generated directory admitted.
"""
from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile


PRODUCT = "price-monitor"
VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
TREE_ROOTS = ("backend/app", "frontend/src", "frontend/public", "deploy")
ROOT_FILES = {
    "README.md", ".env.example", ".dockerignore", "docker-compose.yml",
    "backend/Dockerfile", "backend/requirements.txt",
    "frontend/Dockerfile", "frontend/package.json", "frontend/package-lock.json",
    "frontend/index.html", "frontend/vite.config.ts", "frontend/tsconfig.json",
    "frontend/tsconfig.app.json", "frontend/tsconfig.node.json", "frontend/.prettierrc.json",
    "config/update.example.yaml",
}
SCRIPT_SUFFIXES = {".py", ".ps1", ".sh"}
FORBIDDEN_PARTS = {
    ".git", ".runtime", "var", "data", "backups", "backup", "node_modules", ".venv", "venv",
    "__pycache__", ".pytest_cache", ".cache", ".idea", ".vscode", ".cursor", ".codex",
}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx", ".dump", ".bak"}


def version_from_source(root: Path) -> str:
    """Read a literal version without importing application code or its secrets."""
    source = root / "backend/app/version.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for item in tree.body:
        names = [target.id for target in item.targets if isinstance(target, ast.Name)] if isinstance(item, ast.Assign) else []
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            names = [item.target.id]
        if any(name in {"VERSION", "__version__"} for name in names):
            value = ast.literal_eval(item.value)
            if isinstance(value, str) and VERSION_RE.fullmatch(value):
                return value
    raise ValueError("backend/app/version.py must define a literal X.Y.Z VERSION")


def managed_path(name: str) -> bool:
    """The same boundary is used by the host updater when replacing code."""
    path = PurePosixPath(name)
    if not name or path.is_absolute() or "\\" in name or ":" in name or any(part in {"", ".", ".."} for part in name.split("/")):
        return False
    if any(part.lower() in FORBIDDEN_PARTS for part in path.parts):
        return False
    if path.name.startswith(".env") and name != ".env.example":
        return False
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return False
    if name in ROOT_FILES or name == "release-manifest.json":
        return True
    if any(name.startswith(prefix + "/") for prefix in TREE_ROOTS):
        return True
    if name.startswith("frontend/dist/"):
        return not path.name.endswith(".map")
    return len(path.parts) == 2 and path.parts[0] == "scripts" and path.suffix in SCRIPT_SUFFIXES


def collect_files(root: Path, *, require_dist: bool = True) -> dict[str, Path]:
    root = root.resolve()
    files: dict[str, Path] = {}
    candidates: list[Path] = [root / name for name in sorted(ROOT_FILES)]
    for name in (*TREE_ROOTS, "scripts", "frontend/dist"):
        directory = root / name
        if directory.exists():
            if directory.is_symlink():
                raise ValueError("Release inputs cannot be links")
            candidates.extend(directory.rglob("*"))
    for file in sorted(candidates):
        relative = file.relative_to(root).as_posix()
        if not managed_path(relative) or relative == "release-manifest.json":
            continue
        if file.is_symlink() or any(parent.is_symlink() for parent in file.parents if parent != root and root in parent.parents):
            raise ValueError("Release inputs cannot be links")
        if file.is_file():
            if not file.resolve().is_relative_to(root):
                raise ValueError("Release input escaped root")
            files[relative] = file
    required = {"backend/app/main.py", "backend/app/version.py", "backend/requirements.txt", "frontend/package-lock.json", "docker-compose.yml", "scripts/update_host.py"}
    if require_dist:
        required.add("frontend/dist/index.html")
    missing = sorted(required - files.keys())
    if missing:
        raise ValueError("Missing release inputs: " + ", ".join(missing))
    return files


def build_release(root: Path, output_dir: Path, *, version: str | None = None, require_dist: bool = True) -> tuple[Path, Path]:
    root = root.resolve()
    actual_version = version_from_source(root)
    version = (version or actual_version).removeprefix("v")
    if not VERSION_RE.fullmatch(version) or version != actual_version:
        raise ValueError("Release version must exactly match backend/app/version.py")
    files = collect_files(root, require_dist=require_dist)
    contents = {name: path.read_bytes() for name, path in files.items()}
    manifest = {
        "schema": 1, "product": PRODUCT, "version": version,
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()},
    }
    contents["release-manifest.json"] = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"price-monitor-{version}.tar.gz"
    # Fixed gzip/tar timestamps and identities make identical inputs reproducible.
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, content in sorted(contents.items()):
                    entry = tarfile.TarInfo(name)
                    entry.size, entry.mtime = len(content), 0
                    entry.uid = entry.gid = 0
                    entry.uname = entry.gname = ""
                    entry.mode = 0o755 if name.endswith(".sh") else 0o644
                    archive.addfile(entry, io.BytesIO(content))
    checksum = destination.with_name(destination.name + ".sha256")
    checksum.write_text(f"{hashlib.sha256(destination.read_bytes()).hexdigest()}  {destination.name}\n", encoding="ascii")
    return destination, checksum


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--version")
    args = parser.parse_args()
    try:
        package, checksum = build_release(args.root, args.output_dir or args.root / "dist/releases", version=args.version)
    except (OSError, ValueError, SyntaxError) as exc:
        parser.exit(1, f"Release packaging failed: {exc}\n")
    print(json.dumps({"package": str(package), "checksum": str(checksum)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
