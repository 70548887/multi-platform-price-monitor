"""Proxy settings and platform routing on the isolated database, no source HTTP."""
from types import SimpleNamespace
import asyncio
import json

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from test_pipeline import (isolated_database, clean_test_resources, db, admin_client,
                           platform_and_rule, run_for, source)
from app import main as api, tasks
from app.collectors.base import CollectorError
from app.models import AuditLog, Platform, SystemNetworkSettings, User
from app.network_settings import get_platform_proxy, validate_proxy_url
from app.security import decrypt_session, encrypt_session, password_hash

PROXY = 'http://fixture-user:fixture-password@proxy.example:8080'


def configure(client, group='overseas'):
    return client.put('/api/settings/network', json={group: {'enabled': True, 'url': PROXY}})


def test_default_and_encrypted_settings_never_return_credentials(admin_client, db):
    default = admin_client.get('/api/settings/network')
    assert default.json()['tunnel'] == {'enabled': False, 'configured': False}
    response = configure(admin_client)
    assert response.status_code == 200, response.text
    assert response.json()['overseas'] == {'enabled': True, 'configured': True}
    for forbidden in ('fixture-user', 'fixture-password', 'proxy.example', PROXY):
        assert forbidden not in response.text
        assert forbidden not in admin_client.get('/api/settings/network').text
    saved = db.get(SystemNetworkSettings, 1)
    assert saved.overseas_encrypted_url != PROXY
    assert 'fixture' not in saved.overseas_encrypted_url
    assert decrypt_session(saved.overseas_encrypted_url) == PROXY
    assert db.scalar(select(AuditLog).where(AuditLog.action == 'update_network_settings')).target == 'network'


def test_blank_or_omitted_url_preserves_secret_clear_removes_it(admin_client, db):
    configure(admin_client)
    for update in ({'enabled': False}, {'enabled': True, 'url': ''}, {'url': None}):
        assert admin_client.put('/api/settings/network', json={'overseas': update}).status_code == 200
        db.expire_all()
        assert decrypt_session(db.get(SystemNetworkSettings, 1).overseas_encrypted_url) == PROXY
    cleared = admin_client.put('/api/settings/network', json={'overseas': {'clear': True}})
    assert cleared.json()['overseas'] == {'enabled': False, 'configured': False}
    db.expire_all()
    assert db.get(SystemNetworkSettings, 1).overseas_encrypted_url is None
    invalid = admin_client.put('/api/settings/network', json={'overseas': {'enabled': True}})
    assert invalid.status_code == 422


@pytest.mark.parametrize('payload', [
    {'overseas': {'enabled': 'true', 'url': PROXY}},
    {'overseas': {'url': {'password': 'fixture-password'}}},
    {'tunnel': {'url': 'socks5://fixture-password@proxy.example:8080'}},
    {'overseas': {'url': 'http://fixture-password@proxy.example:8080/path'}},
    {'overseas': {'url': 'http://fixture-password@proxy.example'}},
    {'overseas': {'url': 'http://fixture-password@proxy.example:99999'}},
    {'overseas': {'url': PROXY, 'clear': True}},
    {'unknown': {'url': PROXY}},
    [],
])
def test_invalid_settings_response_does_not_echo_secret(admin_client, payload):
    response = admin_client.put('/api/settings/network', json=payload)
    assert response.status_code == 422
    assert 'fixture-password' not in response.text


def test_malformed_json_and_maximum_body_hide_input(admin_client):
    for content in ('{"url":"fixture-password"', 'fixture-password' * 2000):
        response = admin_client.put('/api/settings/network', content=content, headers={'Content-Type': 'application/json'})
        assert response.status_code == 422
        assert 'fixture-password' not in response.text


def test_viewer_and_missing_csrf_rejected(admin_client, db):
    csrf = admin_client.headers.pop('X-CSRF-Token')
    assert configure(admin_client).status_code == 403
    admin_client.headers['X-CSRF-Token'] = csrf
    db.add(User(username='network-viewer', password_hash=password_hash('fixture-viewer-password'), role='viewer'))
    db.commit()
    with TestClient(api.app) as client:
        logged = client.post('/api/auth/login', json={'username': 'network-viewer', 'password': 'fixture-viewer-password'})
        client.headers['X-CSRF-Token'] = logged.json()['csrf_token']
        assert client.get('/api/settings/network').status_code == 403
        assert configure(client).status_code == 403


def test_routes_fail_closed_and_direct_does_not_use_proxy(admin_client, db):
    platform = SimpleNamespace(proxy_route='direct')
    assert get_platform_proxy(db, platform) is None
    for route in ('tunnel', 'overseas', 'unknown'):
        platform.proxy_route = route
        with pytest.raises(CollectorError):
            get_platform_proxy(db, platform)
    configure(admin_client)
    db.expire_all()
    platform.proxy_route = 'overseas'
    assert get_platform_proxy(db, platform) == PROXY
    saved = db.get(SystemNetworkSettings, 1)
    saved.overseas_encrypted_url = 'broken-secret-value'
    db.commit()
    with pytest.raises(CollectorError) as error:
        get_platform_proxy(db, platform)
    assert 'broken-secret-value' not in str(error.value)


@pytest.mark.parametrize('host', ['api.zxcbug.com', 'api.zxcbug.com.', 'sub.api.zxcbug.com', 'sub.api.zxcbug.com.'])
def test_protected_service_not_used_as_proxy(host):
    with pytest.raises(HTTPException):
        validate_proxy_url('http://' + host + ':8080')


def test_platform_route_crud_legacy_put_preserves_existing_route(admin_client, db):
    create = {'name': '测试代理路由', 'base_url': 'https://www.xiaohong66.com', 'proxy_route': 'overseas'}
    created = admin_client.post('/api/platforms', json=create)
    assert created.status_code == 200
    row = created.json()
    assert row['proxy_route'] == 'overseas'
    assert admin_client.get('/api/platforms').json()['items'][0]['proxy_route'] == 'overseas'
    legacy = admin_client.put('/api/platforms/' + str(row['id']), json={key: value for key, value in create.items() if key != 'proxy_route'})
    assert legacy.status_code == 200
    assert legacy.json()['proxy_route'] == 'overseas'
    invalid = admin_client.post('/api/platforms', json={**create, 'proxy_route': 'bad'})
    assert invalid.status_code == 422
    platform = db.get(Platform, row['id'])
    _, rule = platform_and_rule(db)
    run_for(db, platform, rule, status='running')
    db.commit()
    rejected = admin_client.put('/api/platforms/' + str(row['id']), json={**create, 'proxy_route': 'tunnel'})
    assert rejected.status_code == 409


def test_worker_passes_selected_proxy_snapshot(admin_client, db, monkeypatch):
    configure(admin_client)
    platform, rule = platform_and_rule(db)
    platform.proxy_route = 'overseas'
    run = run_for(db, platform, rule, status='queued')
    db.commit()
    observed = []
    class FakeCollector:
        expected_total = 1
        def __init__(self, *args, **kwargs):
            observed.append(kwargs.get('proxy_url'))
        async def iter_pages(self):
            yield [source()]
        async def aclose(self):
            pass
    monkeypatch.setattr(tasks, 'Xiaohong66Collector', FakeCollector)
    result = asyncio.run(tasks.execute_run(run.id))
    assert result['status'] == 'success'
    assert observed == [PROXY]


def test_session_and_preview_pass_selected_proxy(admin_client, db, monkeypatch):
    configure(admin_client)
    platform, rule = platform_and_rule(db)
    platform.proxy_route = 'overseas'
    db.commit()
    observed = []
    class FakeCollector:
        account_identity = 'fixture-account'
        expected_total = 1
        def __init__(self, *args, **kwargs):
            observed.append(kwargs.get('proxy_url'))
        async def check_session(self):
            return True
        async def list_categories(self):
            return []
        async def iter_pages(self):
            yield [source()]
        async def aclose(self):
            pass
    monkeypatch.setattr(api, 'Xiaohong66Collector', FakeCollector)
    connected = admin_client.put(f'/api/platforms/{platform.id}/session', json={'token': 'fixture-session-long'})
    assert connected.status_code == 200, connected.text
    preview = admin_client.post(f'/api/rules/{rule.id}/test', json={})
    assert preview.status_code == 200 and preview.json()['ok'] is True
    assert observed == [PROXY, PROXY]
