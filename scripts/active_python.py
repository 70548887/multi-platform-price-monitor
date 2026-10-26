"""Resolve a signed Windows runtime activation without importing the application."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.update_common import UpdateError, read_signed_json, validate_relative_path


def active_python(root: Path, secret: str) -> Path:
    root = root.resolve()
    source = root / 'data' / 'update' / 'activation.json'
    record = read_signed_json(source, secret)
    relative = validate_relative_path(record.get('python'))
    target = root.joinpath(*relative.parts)
    if not (relative.parts[0] == '.venv' or target.is_relative_to(root / 'data' / 'update' / 'envs')):
        raise UpdateError('INVALID_ACTIVATION', '应用运行环境指针无效')
    for parent in (target, *target.parents):
        if parent == root:
            break
        if parent.is_symlink() or (parent.exists() and getattr(parent.lstat(), 'st_file_attributes', 0) & 0x400):
            raise UpdateError('INVALID_ACTIVATION', '应用运行环境包含不允许的链接')
    if not target.is_file():
        raise UpdateError('INVALID_ACTIVATION', '应用运行环境不存在')
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(active_python(args.root, os.environ.get('SESSION_SECRET', '')))
        return 0
    except Exception:
        print('应用运行环境校验失败，请检查宿主机更新记录。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
