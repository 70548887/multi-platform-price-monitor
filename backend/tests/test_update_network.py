"""Update networking reuses encrypted collection proxy credentials only in memory."""
from dataclasses import replace
import json

import pytest
from sqlalchemy import select

from test_pipeline import isolated_database, clean_test_resources, db, admin_client
from test_update_api import FakeGitHub
from app import update_service as service
from app.models import AuditLog, SystemNetworkSettings, UpdateRun
from app.security import encrypt_session
from app.update_common import UpdateError
from app.update_config import UpdateConfig, load_update_config
from app.update_network import github_network_config, public_network_config

PROXY = 'http://fixture-user:fixture-private-password@proxy.example:8090'


def configured(db):
    settings = SystemNetworkSettings(id=1, overseas_enabled=True,
                                     overseas_encrypted_url=encrypt_session(PROXY))
    db.add(settings)
    db.commit()
    return settings


def test_overseas_config_is_resolved_in_memory_and_never_public(db, tmp_path):
    configured(db)
    settings = UpdateConfig(root=tmp_path, proxy_route='overseas', proxy='http://other.example:8080')
    resolved = github_network_config(settings, db)
    assert resolved.proxy == PROXY and settings.proxy != PROXY
    public = public_network_config(settings, db)
    assert public['proxy_route'] == 'overseas' and public['proxy_ready'] is True
    assert public['proxy_configured'] is True and public['proxy_enabled'] is True
    serialized = json.dumps(public) + repr(resolved)
    assert 'fixture-private-password' not in serialized and 'proxy.example' not in serialized


@pytest.mark.parametrize('state', ['missing', 'disabled', 'blank', 'corrupt'])
def test_unavailable_overseas_route_fails_closed(db, tmp_path, state):
    if state != 'missing':
        settings = configured(db)
        if state == 'disabled': settings.overseas_enabled = False
        if state == 'blank': settings.overseas_encrypted_url = None
        if state == 'corrupt': settings.overseas_encrypted_url = 'unreadable-credential'
        db.commit()
    cfg = UpdateConfig(root=tmp_path, proxy_route='overseas')
    with pytest.raises(UpdateError) as caught:
        github_network_config(cfg, db)
    assert caught.value.code == 'UPDATE_PROXY_UNAVAILABLE'
    assert 'credential' not in caught.value.message
    assert public_network_config(cfg, db)['proxy_ready'] is False


def test_legacy_custom_and_explicit_direct_routes_are_preserved(db, tmp_path):
    settings = UpdateConfig(root=tmp_path, proxy=PROXY)
    assert github_network_config(settings, db).proxy == PROXY
    assert github_network_config(replace(settings, proxy_route='direct'), db).proxy == ''
    assert public_network_config(replace(settings, proxy_route='direct'), db)['proxy_ready'] is True
    assert public_network_config(UpdateConfig(root=tmp_path), db)['proxy_route'] == 'direct'


def test_check_and_download_share_current_overseas_proxy_without_persisting_it(admin_client, db, monkeypatch, tmp_path):
    configured(db)
    settings = UpdateConfig(root=tmp_path, proxy_route='overseas')
    monkeypatch.setattr(service, 'update_config', lambda: settings)
    seen = []
    class RoutedGitHub(FakeGitHub):
        def __init__(self, settings):
            seen.append(settings.proxy)
            super().__init__(settings)
    monkeypatch.setattr(service, 'GitHubReleaseClient', RoutedGitHub)
    checked = admin_client.post('/api/update/check', json={})
    assert checked.status_code == 200 and checked.json()['status'] == 'available'
    identifier = checked.json()['id']
    assert admin_client.post('/api/update/download', json={'run_id': identifier}).status_code == 202
    assert seen == [PROXY, PROXY]
    db.expire_all()
    row = db.get(UpdateRun, identifier)
    assert row.status == 'ready'
    stored = json.dumps({'release': row.release, 'pending_job': row.pending_job, 'steps': row.steps,
                         'progress': row.progress, 'error': row.error, 'message': row.message})
    stored += admin_client.get('/api/update/config').text + admin_client.get('/api/update/history').text
    stored += ' '.join(log.target for log in db.scalars(select(AuditLog)))
    assert PROXY not in stored and 'fixture-private-password' not in stored


def test_disabled_proxy_prevents_github_requests_and_configuration_can_be_repaired(admin_client, db, monkeypatch, tmp_path):
    settings = configured(db)
    settings.overseas_enabled = False
    db.commit()
    monkeypatch.setattr(service, 'update_config', lambda: UpdateConfig(root=tmp_path, proxy_route='overseas'))
    def forbidden(*args):
        pytest.fail('Unavailable proxy must never instantiate a GitHub client or fall back to direct')
    monkeypatch.setattr(service, 'GitHubReleaseClient', forbidden)
    response = admin_client.post('/api/update/check', json={})
    assert response.status_code == 200
    assert response.json()['status'] == 'failed'
    assert response.json()['error_code'] == 'UPDATE_PROXY_UNAVAILABLE'
    assert admin_client.get('/api/update/config').json()['proxy_ready'] is False
    # Bad update networking must not prevent normal management or proxy repair.
    assert admin_client.get('/api/platforms').status_code == 200
    assert admin_client.put('/api/settings/network', json={'overseas': {'enabled': True}}).status_code == 200
    assert admin_client.get('/api/update/config').json()['proxy_ready'] is True


def test_route_yaml_validation_and_environment_proxy_cannot_override_overseas_choice(tmp_path):
    path = tmp_path / 'config/update.yaml'
    path.parent.mkdir()
    path.write_text('update:\n  proxy_route: overseas\n  proxy: "${UPDATE_PROXY:-}"\n')
    settings = load_update_config(tmp_path, {'UPDATE_PROXY': PROXY})
    assert settings.proxy_route == 'overseas'
    path.write_text('update:\n  proxy_route: invalid-secret-value\n')
    with pytest.raises(UpdateError) as caught:
        load_update_config(tmp_path, {})
    assert 'invalid-secret-value' not in caught.value.message
