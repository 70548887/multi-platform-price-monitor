from __future__ import annotations

import json

import pytest

from app.update_common import UpdateError
from app.update_config import load_update_config


def config_file(tmp_path, text):
    path = tmp_path / "config" / "update.yaml"
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    return path


def test_update_defaults_without_yaml(tmp_path):
    config = load_update_config(tmp_path, {})
    assert config.github_owner == "70548887"
    assert config.github_repo == "multi-platform-price-monitor"
    assert config.runtime_dir == tmp_path / "data" / "update"
    assert config.backup_dir == tmp_path / "data" / "backups"
    assert config.check_interval == 0 and config.auto_download is False and config.auto_restart is True


def test_update_yaml_and_environment_credentials_are_not_serialized(tmp_path):
    config_file(tmp_path, 'update:\n  github_token: "${UPDATE_GITHUB_TOKEN:-}"\n  proxy: "${UPDATE_PROXY:-}"\n  check_interval: 3600\n')
    config = load_update_config(tmp_path, {"UPDATE_GITHUB_TOKEN": "fixture-secret", "UPDATE_PROXY": "http://user:fixture-pass@proxy.example:1234"})
    assert config.github_token == "fixture-secret" and config.check_interval == 3600
    assert config.public_dict()["token_configured"] is True
    serialized = repr(config) + json.dumps(config.public_dict())
    assert "fixture-secret" not in serialized and "fixture-pass" not in serialized and "proxy.example" not in serialized


def test_environment_overrides_literal_yaml_and_empty_value_disables_secret(tmp_path):
    config_file(tmp_path, 'update:\n  github_token: "fixture-old"\n  proxy: "http://old.example:8080"\n')
    config = load_update_config(tmp_path, {"UPDATE_GITHUB_TOKEN": "", "UPDATE_PROXY": ""})
    assert config.github_token == "" and config.proxy == ""


@pytest.mark.parametrize("value", [
    'update: []', 'update:\n  enabled: "true"', 'update:\n  check_interval: -1',
    'update:\n  check_interval: true', 'update:\n  arbitrary: value',
    'update:\n  github_owner: "../private"', 'update:\n  github_token: "${HOME}"',
    'update:\n  github_token: "${UPDATE_PROXY:-}"', 'update:\n  proxy: "socks5://proxy.example:1080"',
    'update:\n  proxy: "http://proxy.example:xx"', 'update:\n  release_asset_pattern: "../{version}.tar.gz"',
    'update:\n  release_asset_pattern: "{other}-{version}.tar.gz"', 'update:\n  backup_dir: "."',
    'update:\n  backup_dir: "${HOME}"', 'update: [broken',
])
def test_bad_update_config_has_no_untrusted_values_in_errors(tmp_path, value):
    config_file(tmp_path, value)
    with pytest.raises(UpdateError) as error:
        load_update_config(tmp_path, {})
    assert error.value.code == "invalid_update_config"
    assert "private" not in str(error.value) and "HOME" not in str(error.value)
