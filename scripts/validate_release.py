"""Validate official release metadata/assets and generate publishable release notes.

This tool never contacts GitHub, changes tags, or reads deployment credentials.
The publish job rechecks the downloaded artifact independently of its build job.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile

from package_release import VERSION_RE, managed_path, version_from_source

TAG_RE = re.compile(r"v((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))\Z")
REVISION_RE = re.compile(r"[0-9a-f]{40}\Z")


def strict_tag(value: str) -> str:
    match = TAG_RE.fullmatch(value or "")
    if not match:
        raise ValueError("Official release tags must be exactly vX.Y.Z without prerelease or leading zeros")
    return match[1]


def revision_for(root: Path, expected: str | None = None) -> str:
    revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    if not REVISION_RE.fullmatch(revision) or revision == "0" * 40 or (expected is not None and revision != expected):
        raise ValueError("Release revision must match the full checked-out Git HEAD")
    changed = subprocess.run(['git', '-C', str(root), 'diff', '--quiet', 'HEAD', '--'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    untracked = subprocess.check_output(['git', '-C', str(root), 'ls-files', '--others', '--exclude-standard'],
                                       stderr=subprocess.DEVNULL, text=True).splitlines()
    if changed.returncode != 0 or any(managed_path(name) for name in untracked):
        raise ValueError('Official releases require committed application files; the checkout is dirty')
    return revision


def changelog_entry(root: Path, version: str) -> tuple[str, str]:
    path = root / "CHANGELOG.md"
    if not path.is_file() or path.is_symlink():
        raise ValueError("CHANGELOG.md is required for official releases")
    content = path.read_text(encoding="utf-8-sig")
    headings = list(re.finditer(r"^## \[([^\]]+)\] - (\d{4}-\d{2}-\d{2})\s*$", content, re.M))
    selected = [(i, match) for i, match in enumerate(headings) if match[1] == version]
    if len(selected) != 1:
        raise ValueError("CHANGELOG.md must contain exactly one dated entry for the release version")
    index, match = selected[0]
    if index != 0:
        raise ValueError("Release changelog entry must be the first version in CHANGELOG.md")
    released = date.fromisoformat(match[2])
    if released > date.today():
        raise ValueError("Release changelog date must not be in the future")
    end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
    entry = content[match.end():end].strip()
    sections = list(re.finditer(r"^### (.+?)\s*$", entry, re.M))
    for required in ("概要", "新增", "修复", "升级说明", "回滚说明"):
        found = [(i, section) for i, section in enumerate(sections) if section[1] == required]
        if len(found) != 1:
            raise ValueError(f"Release changelog requires exactly one section: {required}")
        pos, section = found[0]
        stop = sections[pos + 1].start() if pos + 1 < len(sections) else len(entry)
        if not entry[section.end():stop].strip():
            raise ValueError(f"Release changelog section cannot be empty: {required}")
    return match[2], entry


def validate_project(root: Path, *, tag: str | None = None, revision: str | None = None) -> dict:
    root = root.resolve()
    version = version_from_source(root)
    if tag is not None:
        # Keep the packaging CLI's existing --version X.Y.Z compatibility, but
        # enforce strict vX.Y.Z in the workflow gate and validate CLI below.
        requested = strict_tag(tag) if tag.startswith("v") else tag
        if not VERSION_RE.fullmatch(requested) or requested != version:
            raise ValueError("Tag, backend version, and release version must match")
    package = json.loads((root / "frontend/package.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "frontend/package-lock.json").read_text(encoding="utf-8"))
    if package.get("version") != version or lock.get("version") != version or lock.get("packages", {}).get("", {}).get("version") != version:
        raise ValueError("Backend VERSION, frontend package.json and lockfile root versions must match")
    published, entry = changelog_entry(root, version)
    return {"product": "price-monitor", "version": version, "tag": "v" + version,
            "revision": revision_for(root, revision), "date": published, "changelog": entry}


def verify_assets(directory: Path, metadata: dict) -> dict:
    version, revision = strict_tag(metadata["tag"]), metadata["revision"]
    if not REVISION_RE.fullmatch(revision) or revision == "0" * 40:
        raise ValueError("Expected release revision is invalid")
    filename = f"price-monitor-{version}.tar.gz"
    package, checksum = directory / filename, directory / (filename + ".sha256")
    allowed = {filename, filename + ".sha256", "release-notes.md"}
    if {path.name for path in directory.iterdir()} - allowed:
        raise ValueError("Release asset directory contains unexpected files")
    for path in (package, checksum):
        if path.is_symlink() or not path.is_file():
            raise ValueError("Release package and matching SHA256 file are required")
    expected_line = f"{hashlib.sha256(package.read_bytes()).hexdigest()}  {filename}"
    if checksum.read_text(encoding="ascii").strip() != expected_line:
        raise ValueError("Release SHA256 file must match the exact package filename and bytes")
    with tarfile.open(package, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(name.casefold() for name in names)) or len(names) > 25000:
            raise ValueError("Release archive contains duplicate paths or too many files")
        if sum(member.size for member in members) > 2 * 1024 ** 3:
            raise ValueError("Release archive exceeds the expanded size budget")
        for member in members:
            if not member.isfile() or not managed_path(member.name) or member.size < 0 or member.size > 512 * 1024 ** 2:
                raise ValueError("Release archive contains an unmanaged path or unexpected file type")
        if names.count("release-manifest.json") != 1:
            raise ValueError("Release manifest is required")
        manifest_member = archive.getmember("release-manifest.json")
        if manifest_member.size > 8 * 1024 ** 2:
            raise ValueError("Release manifest exceeds its size budget")
        manifest = json.load(archive.extractfile(manifest_member))
        if (type(manifest.get("schema")) is not int or manifest.get("schema") != 1 or manifest.get("product") != "price-monitor" or
                manifest.get("version") != version or manifest.get("revision") != revision):
            raise ValueError("Release manifest product, schema, version or revision does not match the tested checkout")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != set(names) - {"release-manifest.json"}:
            raise ValueError("Release file inventory does not match its manifest")
        required = {"backend/app/main.py", "backend/app/version.py", "backend/requirements.txt", "frontend/dist/index.html",
                    "frontend/package.json", "frontend/package-lock.json", "scripts/update_host.py", "docker-compose.yml", "CHANGELOG.md"}
        if not required <= set(files):
            raise ValueError("Release archive is missing required application files")
        for name, digest in files.items():
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Release file hash is invalid")
            payload = archive.extractfile(name).read()
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError("Release file content differs from its manifest")
    return {"package": filename, "sha256": expected_line.split()[0], "version": version, "revision": revision}


def render_notes(metadata: dict, assets: dict) -> str:
    return (f"# 多平台商品价格监控 {metadata['tag']}\n\n"
            f"发布日期：{metadata['date']}\n\n"
            f"对应提交：`{metadata['revision']}`\n\n"
            f"{metadata['changelog']}\n\n"
            "### 资源校验\n\n"
            f"升级包：`{assets['package']}`\n\n"
            f"SHA256：`{assets['sha256']}`\n\n"
            "同时提供同名 `.sha256` 校验文件。后台只接受正式版本升级包，下载后先校验，再由宿主机更新器应用。\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tag", help="Exact official tag vX.Y.Z")
    parser.add_argument("--revision", help="Expected full Git HEAD, normally GITHUB_SHA")
    parser.add_argument("--assets", type=Path, help="Independently verify an already built release asset directory")
    parser.add_argument("--notes-output", type=Path)
    args = parser.parse_args()
    try:
        if args.tag is not None:
            strict_tag(args.tag)
        metadata = validate_project(args.root, tag=args.tag, revision=args.revision)
        output = {name: value for name, value in metadata.items() if name != "changelog"}
        if args.assets:
            assets = verify_assets(args.assets, metadata)
            output.update(assets)
            if args.notes_output:
                args.notes_output.parent.mkdir(parents=True, exist_ok=True)
                args.notes_output.write_text(render_notes(metadata, assets), encoding="utf-8", newline="\n")
        elif args.notes_output:
            raise ValueError("Release notes require verified assets")
    except (OSError, ValueError, SyntaxError, KeyError, TypeError, tarfile.TarError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Release validation failed: {error}\n")
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
