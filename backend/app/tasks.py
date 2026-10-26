from __future__ import annotations

import asyncio
import secrets
import time
from datetime import timedelta

import redis
from celery import Celery
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import JSONB

from . import config
from .collectors import AuthExpired, CollectorError, Xiaohong66Collector
from .collectors.fans8 import Fans8Collector
from .collectors.base import safe_collection_error
from .db import SessionLocal
from .models import CollectionRun, DailyAggregate, ErrorCapture, Observation, Platform, Rule, utcnow
from .security import decrypt_session
from .services import add_event, persist_page
from .limits import PlatformLease
from .platform_registry import adapter_supported

celery_app = Celery('price_monitor', broker=config.REDIS_URL, backend=config.REDIS_URL)
celery_app.conf.update(
    task_serializer='json', result_serializer='json', accept_content=['json'],
    timezone='UTC', enable_utc=True, task_acks_late=True,
    task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
    broker_transport_options={'visibility_timeout': 7200, 'global_keyprefix': 'price_monitor:'},
    result_expires=86400,
    beat_schedule={'dispatch-due': {'task': 'app.tasks.dispatch_due', 'schedule': 60.0},
                   'retention': {'task': 'app.tasks.retain_history', 'schedule': 86400.0},
                   'update-check': {'task': 'app.tasks.check_updates', 'schedule': 60.0}},
)


def redis_client():
    return redis.Redis.from_url(config.REDIS_URL, decode_responses=True, socket_timeout=5)


def queue_run(db, platform, scheduled_at=None):
    if not adapter_supported(platform):
        raise ValueError('平台采集版本尚待验证')
    if not platform.active_rule_id:
        raise ValueError('平台尚未发布采集规则')
    existing = db.scalar(select(CollectionRun).where(CollectionRun.platform_id == platform.id,
        CollectionRun.status.in_(['queued', 'running'])).order_by(CollectionRun.id.desc()))
    if existing:
        return existing
    run = CollectionRun(platform_id=platform.id, rule_id=platform.active_rule_id,
                        account_label=platform.account_label, scheduled_at=scheduled_at or utcnow())
    db.add(run)
    db.flush()
    return run


def dispatch_one(run_id):
    collect_platform.delay(run_id)
    with SessionLocal() as db:
        run = db.get(CollectionRun, run_id)
        if run:
            run.dispatched_at = utcnow()
            db.commit()


@celery_app.task(name='app.tasks.dispatch_due')
def dispatch_due():
    from .update_service import maintenance_active
    rc = redis_client()
    rc.set('pm:beat:heartbeat', str(time.time()), ex=180)
    if maintenance_active():
        return {'maintenance': True}
    now = utcnow()
    with SessionLocal() as db:
        # Scheduler leader election lives in PostgreSQL; multiple beat processes cannot double-plan.
        if not db.scalar(text('SELECT pg_try_advisory_xact_lock(71960201)')):
            return {'leader': False}
        rc = redis_client()
        for run in db.scalars(select(CollectionRun).where(CollectionRun.status == 'running')):
            if run.heartbeat_at and run.heartbeat_at < now-timedelta(seconds=180) and not rc.exists(f'pm:lock:{run.platform_id}'):
                if run.attempts < 3:
                    run.status, run.dispatched_at, run.error = 'queued', None, 'Worker 中断，等待恢复'
                else:
                    run.status, run.completed_at, run.error = 'failed', now, 'Worker 中断，恢复次数已达上限'
                    run.error_count = max(run.error_count, 1)
                    failed_platform = db.get(Platform, run.platform_id)
                    if failed_platform:
                        failed_platform.last_error = run.error
                    add_event(db, key=f'run:{run.id}:failed', kind='collection_failed', title='采集批次不完整',
                              message=run.error, platform_id=run.platform_id, run_id=run.id, severity='error')
        for platform in db.scalars(select(Platform).where(Platform.enabled.is_(True), Platform.auth_status == 'connected', Platform.active_rule_id.is_not(None), Platform.next_due_at <= now)):
            if not adapter_supported(platform):
                continue
            queue_run(db, platform, platform.next_due_at)
            while platform.next_due_at <= now:
                platform.next_due_at += timedelta(seconds=platform.interval_seconds)
        db.commit()
        ids = [run.id for run in db.scalars(select(CollectionRun).where(CollectionRun.status == 'queued',
            (CollectionRun.dispatched_at.is_(None)) | (CollectionRun.dispatched_at < now-timedelta(seconds=120))))
            if (platform := db.get(Platform, run.platform_id)) and adapter_supported(platform)]
    for run_id in ids:
        dispatch_one(run_id)
    return {'dispatched': len(ids)}


async def execute_run(run_id: int):
    from .update_service import maintenance_active
    if maintenance_active():
        return {'maintenance': True}
    rc = redis_client()
    owner = secrets.token_hex(24)
    with SessionLocal() as db:
        run = db.get(CollectionRun, run_id)
        if not run or run.status not in ('queued', 'running'):
            return {'skipped': True}
        lock_key = f'pm:lock:{run.platform_id}'
        if not rc.set(lock_key, owner, nx=True, ex=180):
            return {'overlap': True}
        platform_id = run.platform_id
        lease, collector, failure = None, None, None
        expected_total, missing_session = None, False
        try:
            try:
                platform = db.get(Platform, platform_id)
                rule = db.get(Rule, run.rule_id)
                if not platform or not rule:
                    raise RuntimeError('Collection configuration is missing')
                if not adapter_supported(platform):
                    raise CollectorError('平台采集版本尚待验证')
                if not platform.encrypted_session:
                    missing_session = True
                    raise AuthExpired('采集账号未连接')
                run.status, run.started_at, run.heartbeat_at = 'running', run.started_at or utcnow(), utcnow()
                run.attempts += 1
                db.commit()

                def heartbeat():
                    with SessionLocal() as heartbeat_db:
                        from sqlalchemy import update
                        heartbeat_db.execute(update(CollectionRun).where(CollectionRun.id == run_id).values(heartbeat_at=utcnow()))
                        heartbeat_db.commit()

                lease = PlatformLease(rc, platform.id, platform.request_interval, owner=owner, heartbeat=heartbeat)
                await lease.start()
                source_class = Xiaohong66Collector if platform.adapter == 'xiaohong66' else Fans8Collector
                collector = source_class(platform.base_url, decrypt_session(platform.encrypted_session), rule.config,
                                               request_gate=lease.gate, sleep=lease.sleep)
                async for page in collector.iter_pages():
                    lease.renew()
                    persist_page(db, run, page)
                    run.products_seen = db.scalar(select(func.count()).select_from(Observation).where(Observation.run_id == run.id))
                    run.expected_total = collector.expected_total
                    run.heartbeat_at = utcnow()
                    db.commit()
                expected_total = collector.expected_total
            except Exception as exc:
                failure = exc
            finally:
                if collector is not None:
                    if expected_total is None:
                        try:
                            expected_total = collector.expected_total
                        except Exception:
                            pass
                    try:
                        await collector.aclose()
                    except Exception as exc:
                        if failure is None:
                            failure = exc
                # Finalize independently of this possibly broken transaction.
                try:
                    db.rollback()
                except Exception:
                    try:
                        db.close()
                    except Exception:
                        pass

            with SessionLocal() as final_db:
                final_run = final_db.get(CollectionRun, run_id)
                final_platform = final_db.get(Platform, platform_id)
                final_run.products_seen = final_db.scalar(select(func.count()).select_from(Observation).where(Observation.run_id == run_id))
                final_run.expected_total = expected_total
                final_run.completed_at = utcnow()
                if failure is None:
                    final_run.status, final_run.error, final_run.error_count = 'success', None, 0
                    final_platform.auth_status, final_platform.last_success_at, final_platform.last_error = 'connected', final_run.completed_at, None
                elif isinstance(failure, AuthExpired):
                    final_run.status, final_run.error_count = 'auth_expired', 1
                    final_run.error = '请连接采集账号' if missing_session else '采集会话已失效，请重新连接账号'
                    if final_platform:
                        final_platform.auth_status = 'missing' if missing_session else 'expired'
                        final_platform.last_error = final_run.error
                    add_event(final_db, key=f'run:{run_id}:auth', kind='auth_expired', title='平台登录已过期',
                              message=final_platform.name if final_platform else '采集账号不可用',
                              platform_id=platform_id, run_id=run_id, severity='error')
                else:
                    final_run.status = 'partial' if final_run.products_seen else 'failed'
                    final_run.error_count = 1
                    # Constructor/decryption/cleanup exceptions may embed credentials.
                    # Explain exact local completeness guards; unknown text stays hidden.
                    final_run.error = safe_collection_error(failure)
                    if final_platform:
                        final_platform.last_error = final_run.error
                    final_db.add(ErrorCapture(run_id=run_id, message=final_run.error))
                    add_event(final_db, key=f'run:{run_id}:failed', kind='collection_failed', title='采集批次不完整',
                              message=final_run.error, platform_id=platform_id, run_id=run_id, severity='error')
                final_db.commit()
                result = {'run_id': run_id, 'status': final_run.status, 'products_seen': final_run.products_seen}
        finally:
            try:
                if lease is not None:
                    await lease.close()
            finally:
                # Also runs if lease.start/close or database finalization fails.
                rc.eval("if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) end return 0", 1, lock_key, owner)
        return {'auth_expired': True} if missing_session else result


@celery_app.task(name='app.tasks.collect_platform', acks_late=True)
def collect_platform(run_id):
    return asyncio.run(execute_run(run_id))


@celery_app.task(name='app.tasks.check_updates')
def check_updates():
    from .update_service import automatic_check
    return automatic_check()


@celery_app.task(name='app.tasks.retain_history')
def retain_history():
    now = utcnow()
    complete_before = now.replace(hour=0, minute=0, second=0, microsecond=0)
    detail_cutoff = complete_before-timedelta(days=config.DETAIL_RETENTION_DAYS)
    with SessionLocal() as db:
        if not db.scalar(text('SELECT pg_try_advisory_xact_lock(71960202)')):
            return
        # Both aggregation and pruning use the same UTC midnight boundary.
        # Older daily summaries are frozen: their detail may already be gone.
        utc_day = func.date(func.timezone('UTC', Observation.observed_at))
        rows = db.execute(select(Observation.product_id, utc_day, Observation.unit,
            Observation.currency, Observation.display_quantity, func.min(Observation.price), func.max(Observation.price),
            func.avg(Observation.price), func.count()).where(Observation.observed_at < complete_before,
                Observation.observed_at >= detail_cutoff,
                Observation.status == 'active',
                ~Observation.quality.cast(JSONB).contains(['invalid_price']),
                ~Observation.quality.cast(JSONB).contains(['invalid_display_quantity'])).group_by(Observation.product_id, utc_day,
                    Observation.unit, Observation.currency, Observation.display_quantity)).all()
        aggregated = 0
        for pid, day, unit, currency, qty, minimum, maximum, average, count in rows:
            existing = db.scalar(select(DailyAggregate).where(DailyAggregate.product_id == pid,
                DailyAggregate.day == str(day), DailyAggregate.unit == unit,
                DailyAggregate.currency == currency, DailyAggregate.display_quantity == qty))
            # A legacy rolling-hour purge may have left only part of the
            # boundary day. Preserve its existing complete summary too.
            if existing and day <= detail_cutoff.date():
                continue
            if not existing:
                db.add(DailyAggregate(product_id=pid, day=str(day), unit=unit, currency=currency,
                    display_quantity=qty, minimum=minimum, maximum=maximum, average=average, count=count))
            else:
                existing.minimum, existing.maximum, existing.average, existing.count = minimum, maximum, average, count
            aggregated += 1
        db.flush()
        db.execute(delete(Observation).where(Observation.observed_at < detail_cutoff))
        db.execute(delete(DailyAggregate).where(DailyAggregate.day < (now-timedelta(days=config.AGGREGATE_RETENTION_DAYS)).date().isoformat()))
        db.execute(delete(ErrorCapture).where(ErrorCapture.created_at < now-timedelta(days=config.ERROR_RETENTION_DAYS)))
        db.commit()
        return {'aggregated': aggregated}
