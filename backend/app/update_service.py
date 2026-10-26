"""Durable update coordinator. Only the separate host runner replaces files."""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import re
import secrets
import threading
import time

import redis
from sqlalchemy import func, select, text

from . import config
from .db import SessionLocal
from .models import AuditLog, UpdateRun, utcnow
from .update_common import UpdateError, compare_versions, read_signed_json, verify_document, write_signed_json
from .update_config import load_update_config
from .update_github import GitHubReleaseClient, ReleaseInfo
from .version import VERSION

LOCK_KEY = 'pm:update:global'
BUSY = {'checking', 'downloading', 'awaiting_host', 'applying', 'restarting', 'rolling_back', 'applied'}
HOST_ACTIVE = {'awaiting_host', 'applying', 'restarting', 'rolling_back', 'applied'}
HOST_TERMINAL = {'succeeded', 'failed', 'rolled_back'}
STEP_NAMES = [('check', '检测版本'), ('download', '下载更新'), ('verify', '校验更新包'),
              ('backup', '备份旧版本'), ('replace', '替换应用'), ('restart', '重启服务'), ('health', '健康检查')]
PUBLIC_ERRORS = {
    'UPDATE_INTERRUPTED': '更新任务已中断，请重新检测版本。',
    'UPDATE_FAILED': '更新操作失败，请检查更新服务和发布包。',
    'HOST_FAILED': '宿主机更新未完成，详情请检查宿主机更新器日志。',
    'HOST_RESULT_INVALID': '宿主机返回的更新记录无效。',
}


def update_config():
    return load_update_config(root=config.ROOT)


def redis_connection():
    return redis.Redis.from_url(config.REDIS_URL, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)


class UpdateLease:
    """Fail closed on Redis outage; renew while GitHub operations are running."""
    def __init__(self):
        self.owner = secrets.token_hex(24)
        self.client = redis_connection()
        self.stop = threading.Event()
        self.lost = False

    def acquire(self):
        try:
            acquired = self.client.set(LOCK_KEY, self.owner, nx=True, ex=120)
        except redis.RedisError:
            raise UpdateError('UPDATE_LOCK_UNAVAILABLE', '更新锁暂不可用，请稍后重试。', 503) from None
        if not acquired:
            raise UpdateError('UPDATE_BUSY', '已有更新操作正在执行。', 409)
        self.thread = threading.Thread(target=self._renew, daemon=True)
        self.thread.start()
        return self

    def _renew(self):
        while not self.stop.wait(20):
            try:
                if not self.client.eval("if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('expire',KEYS[1],120) end return 0", 1, LOCK_KEY, self.owner):
                    self.lost = True
                    return
            except redis.RedisError:
                self.lost = True
                return

    def assert_owned(self):
        try:
            owned = not self.lost and self.client.get(LOCK_KEY) == self.owner
        except redis.RedisError:
            owned = False
        if not owned:
            raise UpdateError('UPDATE_LOCK_LOST', '更新锁已失效，请重新检测版本。', 409)

    def close(self):
        self.stop.set()
        try:
            self.client.eval("if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) end return 0", 1, LOCK_KEY, self.owner)
        except redis.RedisError:
            pass


@contextmanager
def update_lock():
    lease = UpdateLease().acquire()
    try:
        yield lease
    finally:
        lease.close()


def instant(value):
    if not isinstance(value, str):
        raise ValueError('timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone')
    return parsed.astimezone(timezone.utc)


def signed_payload(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 256 * 1024:
        return None
    try:
        return read_signed_json(path, config.SESSION_SECRET)
    except (OSError, ValueError, TypeError, UpdateError):
        return None


def host_status(settings=None):
    settings = settings or update_config()
    payload = signed_payload(settings.runtime_dir / 'host.json')
    if not isinstance(payload, dict):
        return {'connected': False}
    try:
        age = (utcnow() - instant(payload.get('at'))).total_seconds()
        mode = payload.get('mode')
        if not -5 <= age <= 60 or mode not in ('native', 'compose'):
            return {'connected': False}
        return {'connected': True, 'last_seen': payload['at'], 'mode': mode}
    except (ValueError, TypeError):
        return {'connected': False}


def maintenance_active():
    """A signed durable marker blocks writes until the host completes recovery."""
    try:
        runtime = update_config().runtime_dir
    except UpdateError:
        # Update configuration is independent of collection availability. A typo
        # disables update operations, while a real signed maintenance marker
        # continues to pause writes even if configuration becomes unreadable.
        runtime = config.ROOT / 'data' / 'update'
    value = signed_payload(runtime / 'maintenance.json')
    return isinstance(value, dict) and type(value.get('run_id')) is int


def require_enabled(settings):
    if not settings.enabled:
        raise UpdateError('UPDATE_DISABLED', '在线升级功能已停用。', 409)


def steps_for(status):
    return [{'key': key, 'name': name, 'status': 'running' if key == status else 'pending'} for key, name in STEP_NAMES]


def run_dict(run):
    if run is None:
        return None
    release = run.release or {}
    # Download endpoints and filesystem paths remain server-side.
    public_release = {key: release.get(key) for key in ('version', 'tag', 'name', 'notes', 'published_at', 'html_url', 'size', 'asset_name')}
    return {'id': run.id, 'action': run.action, 'current_version': run.current_version,
            'target_version': run.target_version, 'tag': run.tag, 'status': run.status,
            'message': run.message, 'error_code': run.error_code, 'error': run.error,
            'steps': run.steps or [], 'progress': run.progress or {},
            'release': public_release if release else None, 'backup_id': run.backup_id,
            'backup_version': run.backup_version, 'actor': run.actor,
            'created_at': run.created_at.isoformat(), 'updated_at': run.updated_at.isoformat(),
            'completed_at': run.completed_at.isoformat() if run.completed_at else None}


def mark_failed(db, run, exc):
    # Never serialize an arbitrary exception string, URL, proxy credential or token.
    code = exc.code if isinstance(exc, UpdateError) else 'UPDATE_FAILED'
    message = exc.message if isinstance(exc, UpdateError) else PUBLIC_ERRORS['UPDATE_FAILED']
    run.status, run.error_code, run.error = 'failed', code, message
    run.message, run.updated_at, run.completed_at = message, utcnow(), utcnow()
    db.add(AuditLog(user_id=run.actor_id, action='update_failed', target=str(run.id)))
    db.commit()


def sync_results(db, settings=None):
    settings = settings or update_config()
    for run in db.scalars(select(UpdateRun).where(UpdateRun.action.in_(['apply', 'rollback']))):
        payload = signed_payload(settings.runtime_dir / 'results' / f'{run.id}.json')
        if not isinstance(payload, dict) or payload.get('run_id') != run.id:
            continue
        state = payload.get('status')
        if state not in HOST_ACTIVE | HOST_TERMINAL:
            continue
        try:
            at = instant(payload.get('updated_at') or payload.get('at'))
            if at <= run.updated_at or at > utcnow() + timedelta(seconds=10):
                continue
        except (ValueError, TypeError):
            continue
        run.status, run.updated_at = state, at
        run.message = {'applying': '正在应用更新', 'restarting': '正在重启应用', 'rolling_back': '正在恢复旧版本',
                       'succeeded': '升级和健康检查已完成', 'rolled_back': '旧版本已恢复并通过健康检查',
                       'failed': PUBLIC_ERRORS['HOST_FAILED'], 'applied': '文件已更新，等待重启生效',
                       'awaiting_host': '等待宿主机更新器'}[state]
        if isinstance(payload.get('steps'), list):
            allowed_states = {'pending', 'running', 'completed', 'failed', 'skipped'}
            run.steps = [{'key': key, 'name': name, 'status': next((s.get('status') for s in payload['steps']
                         if isinstance(s, dict) and s.get('key') == key and s.get('status') in allowed_states), 'pending')}
                         for key, name in STEP_NAMES]
        percentage = (payload.get('progress') or {}).get('percentage') if isinstance(payload.get('progress'), dict) else None
        if isinstance(percentage, (int, float)) and 0 <= percentage <= 100:
            run.progress = {'percentage': percentage}
        backup_id = payload.get('backup_id')
        if isinstance(backup_id, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', backup_id):
            run.backup_id = backup_id
            run.backup_version = run.current_version
        if state == 'failed':
            run.error_code = re.sub(r'[^A-Z0-9_]', '', str(payload.get('error_code', 'HOST_FAILED')))[:80]
            run.error = PUBLIC_ERRORS['HOST_FAILED']
        if state in HOST_TERMINAL:
            run.completed_at = at
            db.add(AuditLog(user_id=run.actor_id, action=f'update_{state}', target=str(run.id)))
    db.commit()


def recover_interrupted(db, lease=None):
    """A dead API/download leaves an explicit failure; never replay an apply job."""
    for run in db.scalars(select(UpdateRun).where(UpdateRun.status.in_(['checking', 'downloading']))):
        if (utcnow() - run.updated_at).total_seconds() < 180:
            continue
        try:
            owner = redis_connection().get(LOCK_KEY)
            locked = bool(owner and (lease is None or owner != lease.owner))
        except redis.RedisError:
            continue
        if not locked:
            mark_failed(db, run, UpdateError('UPDATE_INTERRUPTED', PUBLIC_ERRORS['UPDATE_INTERRUPTED']))
    for run in db.scalars(select(UpdateRun).where(UpdateRun.status == 'awaiting_host')):
        job = update_config().runtime_dir / 'jobs' / f'{run.id}.json'
        if not job.exists() and (utcnow() - run.updated_at).total_seconds() > 60:
            # Re-publish only the exact committed intent; current YAML must not
            # turn a previously staged manual restart into an automatic restart.
            if not run.pending_job:
                mark_failed(db, run, UpdateError('UPDATE_INTERRUPTED', PUBLIC_ERRORS['UPDATE_INTERRUPTED']))
                continue
            write_signed_json(job, run.pending_job, config.SESSION_SECRET)


def require_idle(db, lease=None):
    sync_results(db)
    recover_interrupted(db, lease)
    if db.scalar(select(UpdateRun.id).where(UpdateRun.status.in_(BUSY)).limit(1)):
        raise UpdateError('UPDATE_BUSY', '已有更新任务正在执行或等待宿主机处理。', 409)


async def check_update(actor=None):
    settings = update_config()
    require_enabled(settings)
    with update_lock() as lease, SessionLocal() as db:
        require_idle(db, lease)
        run = UpdateRun(current_version=VERSION, repository=f'{settings.github_owner}/{settings.github_repo}',
                        status='checking', action='check', message='正在检测正式发布版本',
                        actor_id=actor.id if actor else None, actor=actor.username if actor else 'system', steps=steps_for('check'))
        db.add(run)
        db.flush()
        db.add(AuditLog(user_id=run.actor_id, action='update_check', target=str(run.id)))
        db.commit()
        try:
            release = await GitHubReleaseClient(settings).check(VERSION)
            lease.assert_owned()
            if release is None:
                run.status, run.message, run.completed_at = 'up_to_date', '当前没有可升级的正式发布包', utcnow()
            else:
                run.release = release.as_dict()
                run.target_version, run.tag = release.version, release.tag
                run.status, run.message = 'available', f'发现新版本 {release.version}'
            run.steps = [{'key': key, 'name': name, 'status': 'completed' if key == 'check' else 'pending'} for key, name in STEP_NAMES]
            run.updated_at = utcnow()
            db.commit()
        except Exception as exc:
            mark_failed(db, run, exc)
        return run_dict(run)


def begin_download(run_id, actor):
    settings = update_config()
    require_enabled(settings)
    lease = UpdateLease().acquire()
    try:
        with SessionLocal() as db:
            require_idle(db, lease)
            run = db.get(UpdateRun, run_id)
            if not run or run.status != 'available' or not run.release:
                raise UpdateError('UPDATE_NOT_AVAILABLE', '请先检测并选择可用的新版本。', 409)
            if run.repository != f'{settings.github_owner}/{settings.github_repo}':
                raise UpdateError('UPDATE_REPOSITORY_CHANGED', '更新仓库已变更，请重新检测并下载版本。', 409)
            if compare_versions(run.target_version, VERSION) <= 0:
                raise UpdateError('UPDATE_DOWNGRADE', '目标版本必须高于当前版本。', 409)
            run.status, run.action, run.message = 'downloading', 'download', '正在下载并校验发布包'
            run.actor_id, run.actor, run.updated_at = actor.id if actor else None, actor.username if actor else 'system', utcnow()
            run.steps = steps_for('download')
            db.add(AuditLog(user_id=run.actor_id, action='update_download', target=str(run.id)))
            db.commit()
            return run_dict(run), lease
    except Exception:
        lease.close()
        raise


async def download_run(run_id, lease):
    try:
        with SessionLocal() as db:
            run = db.get(UpdateRun, run_id)
            if not run or run.status != 'downloading':
                return
            try:
                settings = update_config()
                if run.repository != f'{settings.github_owner}/{settings.github_repo}':
                    raise UpdateError('UPDATE_REPOSITORY_CHANGED', '更新仓库已变更，请重新检测并下载版本。', 409)
                release = ReleaseInfo.from_dict(run.release)
                destination = settings.runtime_dir / 'packages' / str(run.id)
                last_progress = 0.0
                def progress(size, total):
                    nonlocal last_progress
                    if time.monotonic() - last_progress < 1 and size != total:
                        return
                    lease.assert_owned()
                    last_progress = time.monotonic()
                    run.progress = {'percentage': min(99, round(size / total * 100, 1)) if total else 0,
                                    'downloaded_bytes': size, 'total_bytes': total or 0}
                    run.updated_at = utcnow()
                    db.commit()
                result = await GitHubReleaseClient(settings).download(release, destination, progress_callback=progress)
                lease.assert_owned()
                run.package = result.path.relative_to(settings.runtime_dir).as_posix()
                run.sha256 = result.sha256
                run.status, run.message, run.updated_at = 'ready', '发布包已下载并通过校验，可应用更新', utcnow()
                run.progress = {'percentage': 100, 'downloaded_bytes': result.size, 'total_bytes': result.size}
                run.steps = [{'key': key, 'name': name, 'status': 'completed' if key in ('check', 'download', 'verify') else 'pending'} for key, name in STEP_NAMES]
                db.commit()
            except Exception as exc:
                mark_failed(db, run, exc)
    finally:
        lease.close()


def enqueue_apply(run_id, actor):
    settings = update_config()
    require_enabled(settings)
    with update_lock() as lease, SessionLocal() as db:
        require_idle(db, lease)
        run = db.get(UpdateRun, run_id)
        if not run or run.status != 'ready' or not run.package or not run.sha256:
            raise UpdateError('UPDATE_NOT_READY', '请先下载并校验更新包。', 409)
        if compare_versions(run.target_version, VERSION) <= 0:
            raise UpdateError('UPDATE_DOWNGRADE', '目标版本必须高于当前版本。', 409)
        if run.repository != f'{settings.github_owner}/{settings.github_repo}':
            raise UpdateError('UPDATE_REPOSITORY_CHANGED', '更新仓库已变更，请重新检测并下载版本。', 409)
        run.action, run.status, run.message = 'apply', 'awaiting_host', '等待宿主机更新器应用发布包'
        run.actor_id, run.actor, run.updated_at = actor.id, actor.username, utcnow()
        run.pending_job = job_payload(run, settings)
        db.add(AuditLog(user_id=actor.id, action='update_apply', target=str(run.id)))
        db.commit()
        try:
            write_signed_json(settings.runtime_dir / 'jobs' / f'{run.id}.json', run.pending_job, config.SESSION_SECRET)
        except Exception as exc:
            mark_failed(db, run, exc)
        return run_dict(run)


def backups(db):
    # Only successful, recorded host backups are exposed as rollback choices.
    rows = db.scalars(select(UpdateRun).where(UpdateRun.backup_id.is_not(None),
                      UpdateRun.status.in_(['succeeded', 'failed', 'rolled_back', 'applied'])).order_by(UpdateRun.id.desc())).all()
    seen = set()
    result = []
    for run in rows:
        if run.backup_id in seen:
            continue
        seen.add(run.backup_id)
        result.append({'id': run.backup_id, 'version': run.backup_version, 'created_at': run.updated_at.isoformat(), 'run_id': run.id})
    return result


def enqueue_rollback(backup_id, actor):
    settings = update_config()
    require_enabled(settings)
    with update_lock() as lease, SessionLocal() as db:
        require_idle(db, lease)
        selected = next((b for b in backups(db) if b['id'] == backup_id), None)
        if not selected:
            raise UpdateError('BACKUP_NOT_FOUND', '未找到可回滚的旧版本备份。', 404)
        run = UpdateRun(current_version=VERSION, target_version=selected['version'], action='rollback',
                        status='awaiting_host', message='等待宿主机更新器恢复旧版本', actor_id=actor.id,
                        actor=actor.username, backup_id=backup_id, backup_version=selected['version'], steps=steps_for('backup'))
        db.add(run)
        db.flush()
        run.pending_job = job_payload(run, settings)
        db.add(AuditLog(user_id=actor.id, action='update_rollback', target=str(run.id)))
        db.commit()
        try:
            write_signed_json(settings.runtime_dir / 'jobs' / f'{run.id}.json', run.pending_job, config.SESSION_SECRET)
        except Exception as exc:
            mark_failed(db, run, exc)
        return run_dict(run)


def job_payload(run, settings):
    job = {'protocol': 1, 'run_id': run.id, 'action': run.action, 'current_version': run.current_version,
           'target_version': run.target_version, 'tag': 'v' + run.target_version,
           'repository': run.repository or f'{settings.github_owner}/{settings.github_repo}',
           'auto_restart': settings.auto_restart}
    if run.action == 'apply':
        job.update(package=run.package, sha256=run.sha256)
    else:
        job['backup_id'] = run.backup_id
    return job


def status(db):
    settings = update_config()
    sync_results(db, settings)
    recover_interrupted(db)
    run = db.scalar(select(UpdateRun).order_by(UpdateRun.id.desc()).limit(1))
    return {'enabled': settings.enabled, 'current_version': VERSION, 'latest_version': run.target_version if run else None,
            'status': run.status if run else 'idle', 'run': run_dict(run), 'host': host_status(settings), 'backups': backups(db)}


def verify_host_proof(proof, purpose):
    try:
        if len(proof) > 4096:
            raise ValueError('length')
        payload = verify_document(json.loads(base64.urlsafe_b64decode(proof).decode()), config.SESSION_SECRET)
        if payload.get('purpose') != purpose or type(payload.get('run_id')) is not int:
            raise ValueError('purpose')
        if abs((utcnow() - instant(payload.get('at'))).total_seconds()) > 60:
            raise ValueError('expired')
        return payload
    except Exception:
        raise UpdateError('HOST_UNAUTHORIZED', '更新器身份验证失败。', 403) from None


def automatic_check():
    """Called by Beat; a zero interval performs no GitHub traffic."""
    settings = update_config()
    if not settings.enabled or settings.check_interval <= 0 or maintenance_active():
        return {'skipped': True}
    with SessionLocal() as db:
        last = db.scalar(select(UpdateRun).order_by(UpdateRun.id.desc()).limit(1))
        if last and ((utcnow() - last.created_at).total_seconds() < settings.check_interval or last.status in BUSY | {'ready', 'available'}):
            return {'skipped': True}
    try:
        found = asyncio.run(check_update())
        if settings.auto_download and found['status'] == 'available':
            _, lease = begin_download(found['id'], None)
            asyncio.run(download_run(found['id'], lease))
        return {'status': found['status']}
    except UpdateError as exc:
        return {'error_code': exc.code}
