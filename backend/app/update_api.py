"""Administrator update routes and a narrowly scoped, signed host probe."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import get_db
from .models import AIAnalysisRun, CollectionRun, UpdateRun
from .security import admin_user, current_user
from . import update_service as service
from .version import VERSION

router = APIRouter()


class RunInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_id: int = Field(gt=0)


class RollbackInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    backup_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


@router.get('/api/version')
def version(user=Depends(current_user)):
    return {'version': VERSION}


@router.get('/api/update/config')
def configuration(db: Session = Depends(get_db), user=Depends(admin_user)):
    return service.public_network_config(service.update_config(), db)


@router.get('/api/update/status')
def status(db: Session = Depends(get_db), user=Depends(admin_user)):
    return service.status(db)


@router.post('/api/update/check')
async def check(user=Depends(admin_user)):
    return await service.check_update(user)


@router.post('/api/update/download', status_code=202)
def download(body: RunInput, background: BackgroundTasks, user=Depends(admin_user)):
    result, lease = service.begin_download(body.run_id, user)
    background.add_task(service.download_run, body.run_id, lease)
    return result


@router.post('/api/update/apply', status_code=202)
def apply(body: RunInput, user=Depends(admin_user)):
    return service.enqueue_apply(body.run_id, user)


@router.post('/api/update/rollback', status_code=202)
def rollback(body: RollbackInput, user=Depends(admin_user)):
    return service.enqueue_rollback(body.backup_id, user)


@router.get('/api/update/history')
def history(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
            db: Session = Depends(get_db), user=Depends(admin_user)):
    service.sync_results(db)
    service.recover_interrupted(db)
    total = db.scalar(select(func.count()).select_from(UpdateRun))
    rows = db.scalars(select(UpdateRun).order_by(UpdateRun.id.desc()).offset((page - 1) * page_size).limit(page_size))
    return {'items': [service.run_dict(row) for row in rows], 'total': total, 'page': page, 'page_size': page_size}


@router.get('/api/update/host/drain', include_in_schema=False)
def drain(proof: str = Header(default='', alias='X-Update-Host-Proof'), db: Session = Depends(get_db)):
    service.verify_host_proof(proof, 'drain')
    collection_running = db.scalar(select(func.count()).select_from(CollectionRun).where(CollectionRun.status == 'running'))
    ai_running = db.scalar(select(func.count()).select_from(AIAnalysisRun).where(AIAnalysisRun.status == 'running'))
    running = collection_running + ai_running
    queued = db.scalar(select(func.count()).select_from(CollectionRun).where(CollectionRun.status == 'queued'))
    return {'running': running, 'collection_running': collection_running, 'ai_running': ai_running,
            'queued': queued, 'ready': running == 0}


@router.get('/api/update/host/health', include_in_schema=False)
def host_health(proof: str = Header(default='', alias='X-Update-Host-Proof'), db: Session = Depends(get_db)):
    service.verify_host_proof(proof, 'health')
    checks = {'database': False, 'redis': False, 'worker': False, 'ai_worker': False, 'beat': False}
    try:
        db.execute(select(1))
        checks['database'] = True
    except Exception:
        pass
    try:
        rc = service.redis_connection()
        checks['redis'] = bool(rc.ping())
        beat_at = rc.get('pm:beat:heartbeat')
        checks['beat'] = bool(beat_at and 0 <= __import__('time').time() - float(beat_at) <= 180)
        from .ai_worker import AI_HEARTBEAT_KEY, heartbeat_fresh
        checks['ai_worker'] = heartbeat_fresh(rc.get(AI_HEARTBEAT_KEY))
    except Exception:
        pass
    try:
        from .tasks import celery_app
        replies = celery_app.control.inspect(timeout=3).ping() or {}
        # A healthy AI worker must not hide a missing collection worker.
        checks['worker'] = any(name.startswith(('collector@', 'worker@', 'celery@'))
                               and response.get('ok') == 'pong' for name, response in replies.items()
                               if isinstance(name, str) and isinstance(response, dict))
    except Exception:
        pass
    return {'status': 'ok' if all(checks.values()) else 'degraded', 'version': VERSION, **checks}
