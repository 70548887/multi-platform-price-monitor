"""Read update settings without serializing credentials into application state."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

import yaml

from .update_common import UpdateError


@dataclass(frozen=True)
class UpdateConfig:
    root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2])
    enabled: bool = True
    github_owner: str = "70548887"
    github_repo: str = "multi-platform-price-monitor"
    github_token: str = field(default="", repr=False, compare=False)
    check_interval: int = 0
    proxy: str = field(default="", repr=False, compare=False)
    auto_download: bool = False
    auto_restart: bool = True
    backup_dir: Path = Path("data/backups")
    release_asset_pattern: str = "price-monitor-{version}.tar.gz"

    @property
    def runtime_dir(self) -> Path:
        return self.root / "data" / "update"

    def public_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "github_owner": self.github_owner,
            "github_repo": self.github_repo,
            "token_configured": bool(self.github_token),
            "proxy_configured": bool(self.proxy),
            "check_interval": self.check_interval,
            "auto_download": self.auto_download,
            "auto_restart": self.auto_restart,
            "backup_dir": "configured",
            "release_asset_pattern": self.release_asset_pattern,
        }


def load_update_config(root: Path | None = None, environ: Mapping[str, str] | None = None,
                       path: Path | None = None) -> UpdateConfig:
    root = (root or Path(__file__).resolve().parents[2]).resolve()
    environment = os.environ if environ is None else environ
    config_path = path or root / "config" / "update.yaml"
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, UnicodeError, yaml.YAMLError):
        raise UpdateError("invalid_update_config", "更新配置无法读取或格式错误") from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or not isinstance(raw.get("update", {}), dict):
        raise UpdateError("invalid_update_config", "更新配置必须包含 update 对象")
    values = raw.get("update", {})
    defaults = UpdateConfig(root=root)
    known = set(defaults.__dataclass_fields__) - {"root"}
    if set(values) - known:
        raise UpdateError("invalid_update_config", "更新配置包含未知字段")
    resolved = {name: values.get(name, getattr(defaults, name)) for name in known}
    for name in ("enabled", "auto_download", "auto_restart"):
        if type(resolved[name]) is not bool:
            raise UpdateError("invalid_update_config", "更新开关必须为布尔值")
    if type(resolved["check_interval"]) is not int or resolved["check_interval"] < 0:
        raise UpdateError("invalid_update_config", "自动检测间隔必须为非负整数")
    for name in ("github_owner", "github_repo"):
        if not isinstance(resolved[name], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", resolved[name]):
            raise UpdateError("invalid_update_config", "GitHub 仓库名称格式错误")
    for name, env_name in (("github_token", "UPDATE_GITHUB_TOKEN"), ("proxy", "UPDATE_PROXY")):
        value = resolved[name]
        if not isinstance(value, str):
            raise UpdateError("invalid_update_config", "更新凭据配置必须为字符串")
        match = re.fullmatch(r"\$\{([A-Z_]+)(?::-)?\}", value)
        if match:
            if match[1] != env_name:
                raise UpdateError("invalid_update_config", "更新配置引用了不允许的环境变量")
            value = environment.get(env_name, "")
        elif "${" in value:
            raise UpdateError("invalid_update_config", "更新环境变量格式错误")
        if env_name in environment:
            value = environment[env_name]
        if any(ord(c) < 32 for c in value):
            raise UpdateError("invalid_update_config", "更新凭据包含非法字符")
        resolved[name] = value
    if resolved["proxy"]:
        try:
            proxy = urlsplit(resolved["proxy"])
            valid_proxy = proxy.scheme in ("http", "https") and proxy.hostname and proxy.port and proxy.path in ("", "/") and not proxy.query and not proxy.fragment
        except ValueError:
            valid_proxy = False
        if not valid_proxy:
            raise UpdateError("invalid_update_config", "更新代理格式错误")
    pattern = resolved["release_asset_pattern"]
    if not isinstance(pattern, str) or pattern.count("{version}") != 1 or not re.fullmatch(r"[A-Za-z0-9._{}-]+", pattern) or "{" in pattern.replace("{version}", "") or "}" in pattern.replace("{version}", ""):
        raise UpdateError("invalid_update_config", "更新包名称必须仅包含一个 version 占位符")
    backup = resolved["backup_dir"]
    if not isinstance(backup, (str, Path)) or not str(backup).strip() or "${" in str(backup):
        raise UpdateError("invalid_update_config", "更新备份目录格式错误")
    backup_path = Path(backup)
    resolved["backup_dir"] = (backup_path if backup_path.is_absolute() else root / backup_path).resolve()
    if resolved["backup_dir"] == root:
        raise UpdateError("invalid_update_config", "更新备份目录不能为应用根目录")
    return UpdateConfig(root=root, **resolved)
