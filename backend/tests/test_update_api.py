"""Real isolated database/Redis integration; all update HTTP and host work is fake."""
from datetime import timedelta
from pathlib import Path
import base64
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from test_pipeline import isolated_database, clean_test_resources, db, admin_client
from app import update_service as service
from app.main import app
from app.models import AuditLog, UpdateRun, User, utcnow
from app.security import password_hash
from app.update_common import UpdateError, read_signed_json, sign_document, write_signed_json
from app.update_config import UpdateConfig
from app.update_github import DownloadResult, ReleaseInfo
from app.version import VERSION
from dataclasses import replace


@pytest.fixture(autouse=True)
def isolated_updater(tmp_path, monkeypatch):
    settings = UpdateConfig(root=tmp_path, github_token='fixture-github-secret', proxy='http://fixture:password@example.invalid:8080')
    monkeypatch.setattr(service, 'update_config', lambda: settings)
    return settings


class FakeGitHub:
    def __init__(self, settings):
        self.settings = settings

    async def check(self, version):
        return ReleaseInfo(version='2.0.0', tag='v2.0.0', release_id=1, asset_id=2, checksum_asset_id=3,
                           asset_name='price-monitor-2.0.0.tar.gz', notes='<script>plain text</script>',
                           html_url='https://github.com/70548887/multi-platform-price-monitor/releases/tag/v2.0.0', size=3)

    async def download(self, release, destination, progress_callback=None):
        destination.mkdir(parents=True)
        package = destination / release.asset_name
        package.write_bytes(b'test')
        return DownloadResult(package, 'a' * 64, 4)


def ready_run(client, monkeypatch):
    monkeypatch.setattr(service, 'GitHubReleaseClient', FakeGitHub)
    checked = client.post('/api/update/check', json={})
    assert checked.status_code == 200
    row = checked.json()
    assert row['status'] == 'available'
    downloaded = client.post('/api/update/download', json={'run_id': row['id']})
    assert downloaded.status_code == 202
    return row['id']


def test_check_download_apply_and_signed_result_persist_across_api_restarts(admin_client, db, monkeypatch, isolated_updater):
    identifier = ready_run(admin_client, monkeypatch)
    row = db.get(UpdateRun, identifier)
    assert row.status == 'ready'
    assert row.sha256 == 'a' * 64
    response = admin_client.post('/api/update/apply', json={'run_id': identifier})
    assert response.status_code == 202
    assert response.json()['status'] == 'awaiting_host'
    job = read_signed_json(isolated_updater.runtime_dir / 'jobs' / f'{identifier}.json', service.config.SESSION_SECRET)
    assert job['action'] == 'apply' and job['sha256'] == 'a' * 64
    assert job['repository'] == '70548887/multi-platform-price-monitor'
    assert 'fixture-github-secret' not in json.dumps(job)
    state = admin_client.get('/api/update/status').json()
    assert state['host']['connected'] is False
    assert 'package' not in state['run']
    write_signed_json(isolated_updater.runtime_dir / 'results' / f'{identifier}.json',
                      {'run_id': identifier, 'status': 'succeeded', 'backup_id': 'run-1.tar.gz',
                       'updated_at': utcnow().isoformat(), 'progress': {'percentage': 100}}, service.config.SESSION_SECRET)
    state = admin_client.get('/api/update/status').json()
    assert state['run']['status'] == 'succeeded'
    assert state['backups'][0]['id'] == 'run-1.tar.gz'
    assert admin_client.get('/api/update/history').json()['items'][0]['status'] == 'succeeded'
    actions = list(db.scalars(select(AuditLog.action)))
    assert {'update_check', 'update_download', 'update_apply', 'update_succeeded'} <= set(actions)
    rollback = admin_client.post('/api/update/rollback', json={'backup_id': 'run-1.tar.gz'})
    assert rollback.status_code == 202
    assert rollback.json()['action'] == 'rollback'


def test_privileges_csrf_and_config_redaction(admin_client, db):
    configuration = admin_client.get('/api/update/config')
    assert configuration.status_code == 200
    assert configuration.json()['token_configured'] is True
    assert configuration.json()['proxy_configured'] is True
    assert 'fixture' not in configuration.text and 'password' not in configuration.text
    csrf = admin_client.headers.pop('X-CSRF-Token')
    assert admin_client.post('/api/update/check', json={}).status_code == 403
    admin_client.headers['X-CSRF-Token'] = csrf
    db.add(User(username='update-viewer', password_hash=password_hash('fixture-viewer-password'), role='viewer'))
    db.commit()
    with TestClient(app) as viewer:
        login = viewer.post('/api/auth/login', json={'username': 'update-viewer', 'password': 'fixture-viewer-password'})
        viewer.headers['X-CSRF-Token'] = login.json()['csrf_token']
        assert viewer.get('/api/version').json()['version'] == VERSION
        for path in ('status', 'history', 'config'):
            assert viewer.get(f'/api/update/{path}').status_code == 403
        for path in ('check', 'download', 'apply', 'rollback'):
            assert viewer.post(f'/api/update/{path}', json={}).status_code == 403


def test_deduplicated_jobs_global_lock_and_failure_does_not_expose_secret(admin_client, db, monkeypatch, isolated_updater):
    identifier = ready_run(admin_client, monkeypatch)
    assert admin_client.post('/api/update/apply', json={'run_id': identifier}).status_code == 202
    assert admin_client.post('/api/update/apply', json={'run_id': identifier}).status_code == 409
    assert admin_client.post('/api/update/check', json={}).status_code == 409
    assert len(list((isolated_updater.runtime_dir / 'jobs').glob('*.json'))) == 1
    with service.update_lock():
        with pytest.raises(UpdateError, match='已有更新操作'):
            with service.update_lock():
                pass
    run = db.get(UpdateRun, identifier)
    run.status = 'failed'
    db.commit()
    class Broken(FakeGitHub):
        async def check(self, version):
            raise RuntimeError('fixture-github-secret http://fixture:password@example.invalid:8080')
    monkeypatch.setattr(service, 'GitHubReleaseClient', Broken)
    response = admin_client.post('/api/update/check', json={})
    assert response.json()['status'] == 'failed'
    assert 'fixture' not in response.text and 'password' not in response.text


def test_signed_maintenance_blocks_writes_and_dispatch_but_leaves_read_access(admin_client, isolated_updater):
    write_signed_json(isolated_updater.runtime_dir / 'maintenance.json', {'run_id': 100}, service.config.SESSION_SECRET)
    assert admin_client.post('/api/update/check', json={}).status_code == 503
    assert admin_client.get('/api/update/status').status_code == 200
    from app.tasks import dispatch_due, execute_run
    assert dispatch_due.run() == {'maintenance': True}
    import asyncio
    assert asyncio.run(execute_run(1)) == {'maintenance': True}


def test_signed_host_proof_rejects_tampering_expiry_and_wrong_purpose(admin_client):
    def proof(purpose='drain', at=None):
        envelope = sign_document({'purpose': purpose, 'run_id': 1, 'at': (at or utcnow()).isoformat()}, service.config.SESSION_SECRET)
        return base64.urlsafe_b64encode(json.dumps(envelope).encode()).decode()
    path = '/api/update/host/drain'
    assert admin_client.get(path).status_code == 403
    assert admin_client.get(path, headers={'X-Update-Host-Proof': proof()}).json()['ready'] is True
    assert admin_client.get(path, headers={'X-Update-Host-Proof': proof('health')}).status_code == 403
    assert admin_client.get(path, headers={'X-Update-Host-Proof': proof(at=utcnow()-timedelta(minutes=2))}).status_code == 403
    assert admin_client.get(path, headers={'X-Update-Host-Proof': proof()[:-10]+'abcdefghij'}).status_code == 403


def test_unsigned_host_results_are_ignored_and_orphan_download_is_failed(admin_client, db, isolated_updater):
    row = UpdateRun(action='apply', current_version=VERSION, status='awaiting_host')
    db.add(row)
    db.commit()
    result = isolated_updater.runtime_dir / 'results' / f'{row.id}.json'
    result.parent.mkdir(parents=True)
    result.write_text(json.dumps({'payload': {'run_id': row.id, 'status': 'succeeded'}}))
    assert admin_client.get('/api/update/status').json()['run']['status'] == 'awaiting_host'
    row.status = 'downloading'
    row.updated_at = utcnow() - timedelta(minutes=10)
    db.commit()
    output = admin_client.get('/api/update/status').json()
    assert output['run']['status'] == 'failed'
    assert output['run']['error_code'] == 'UPDATE_INTERRUPTED'


def test_zero_interval_no_github_calls(monkeypatch):
    monkeypatch.setattr(service, 'GitHubReleaseClient', lambda *_: (_ for _ in ()).throw(AssertionError('network')))
    assert service.automatic_check() == {'skipped': True}


def test_bad_update_configuration_does_not_pause_business_without_real_marker(admin_client, monkeypatch, isolated_updater):
    monkeypatch.setattr(service.config, 'ROOT', isolated_updater.root)
    def invalid_settings():
        raise UpdateError('invalid_update_config', '更新配置错误')
    monkeypatch.setattr(service, 'update_config', invalid_settings)
    assert service.maintenance_active() is False
    response = admin_client.post('/api/platforms', json={'name': '配置错误不影响采集管理', 'base_url': 'https://source.example'})
    assert response.status_code == 200, response.text
    write_signed_json(isolated_updater.runtime_dir / 'maintenance.json', {'run_id': 100}, service.config.SESSION_SECRET)
    assert service.maintenance_active() is True
    assert admin_client.post('/api/platforms', json={'name': '维护仍阻断', 'base_url': 'https://source.example'}).status_code == 503


def test_missing_job_recovers_exact_committed_intent_after_configuration_change(admin_client, db, monkeypatch, isolated_updater):
    identifier = ready_run(admin_client, monkeypatch)
    response = admin_client.post('/api/update/apply', json={'run_id': identifier})
    assert response.status_code == 202
    job = isolated_updater.runtime_dir / 'jobs' / f'{identifier}.json'
    original = read_signed_json(job, service.config.SESSION_SECRET)
    job.unlink()
    db.expire_all()
    run = db.get(UpdateRun, identifier)
    assert run.pending_job == original
    run.updated_at = utcnow()-timedelta(minutes=2)
    db.commit()
    monkeypatch.setattr(service, 'update_config', lambda: replace(isolated_updater, github_repo='other-repository', auto_restart=False))
    service.recover_interrupted(db)
    recovered = read_signed_json(job, service.config.SESSION_SECRET)
    assert recovered == original
    assert recovered['auto_restart'] is True
    assert recovered['repository'] == '70548887/multi-platform-price-monitor'


def test_legacy_orphan_without_committed_intent_fails_without_guessing(db, isolated_updater):
    run = UpdateRun(action='apply', current_version=VERSION, target_version='2.0.0', status='awaiting_host',
                    updated_at=utcnow()-timedelta(minutes=2))
    db.add(run)
    db.commit()
    service.recover_interrupted(db)
    assert run.status == 'failed'
    assert not (isolated_updater.runtime_dir / 'jobs' / f'{run.id}.json').exists()


def test_download_rejects_changed_repository_before_scheduling(admin_client, monkeypatch, isolated_updater):
    monkeypatch.setattr(service, 'GitHubReleaseClient', FakeGitHub)
    identifier = admin_client.post('/api/update/check', json={}).json()['id']
    monkeypatch.setattr(service, 'update_config', lambda: replace(isolated_updater, github_repo='other-repository'))
    response = admin_client.post('/api/update/download', json={'run_id': identifier})
    assert response.status_code == 409
    assert response.json()['error_code'] == 'UPDATE_REPOSITORY_CHANGED'


def test_background_download_rechecks_repository_after_response(admin_client, db, monkeypatch, isolated_updater):
    import asyncio
    monkeypatch.setattr(service, 'GitHubReleaseClient', FakeGitHub)
    identifier = admin_client.post('/api/update/check', json={}).json()['id']
    actor = db.scalar(select(User).where(User.username == 'test-admin'))
    _, lease = service.begin_download(identifier, actor)
    monkeypatch.setattr(service, 'update_config', lambda: replace(isolated_updater, github_repo='other-repository'))
    asyncio.run(service.download_run(identifier, lease))
    db.expire_all()
    assert db.get(UpdateRun, identifier).error_code == 'UPDATE_REPOSITORY_CHANGED'
    assert not (isolated_updater.runtime_dir / 'packages').exists()


def test_new_update_lock_can_recover_old_orphan_before_next_check(admin_client, db, monkeypatch):
    row = UpdateRun(action='download', current_version=VERSION, status='downloading', updated_at=utcnow()-timedelta(minutes=5))
    db.add(row)
    db.commit()
    monkeypatch.setattr(service, 'GitHubReleaseClient', FakeGitHub)
    response = admin_client.post('/api/update/check', json={})
    assert response.status_code == 200
    assert response.json()['status'] == 'available'
    db.expire_all()
    assert db.get(UpdateRun, row.id).status == 'failed'
