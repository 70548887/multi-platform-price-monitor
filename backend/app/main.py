from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .collectors import AuthExpired, CollectorError, DEFAULT_RULE, Xiaohong66Collector
from .collectors.fans8 import Fans8Collector
from .db import Base, SessionLocal, engine, get_db
from .models import (AuditLog, CanonicalProduct, CollectionRun, DailyAggregate, Event, Observation,
                     Platform, Product, Rule, User, utcnow)
from .security import (admin_user, audit, current_user, decrypt_session, encrypt_session,
                       password_hash, password_matches, validate_base_url)
from .services import decimal_text, display_price, event_dict, iso, json_safe, price_quality_valid, product_dict, run_dict
from .tasks import dispatch_one, queue_run, redis_client
from .operations import platform_operation
from .rule_config import validate_rule_config
from .platform_registry import (PLATFORM_TYPES, adapter_info, adapter_default_rule,
                                adapter_supported, require_supported, validate_adapter_rule)
from .update_api import router as update_router
from .update_common import UpdateError
from .update_service import maintenance_active
from .version import VERSION

STARTED = time.monotonic()


@asynccontextmanager
async def lifespan(app):
    config.validate_secrets()
    with engine.begin() as connection:
        connection.execute(text('SELECT pg_advisory_xact_lock(71960200)'))
        Base.metadata.create_all(connection)
        connection.execute(text('ALTER TABLE platforms ADD COLUMN IF NOT EXISTS account_fingerprint VARCHAR(64)'))
        connection.execute(text("ALTER TABLE platforms ADD COLUMN IF NOT EXISTS platform_type VARCHAR(40) NOT NULL DEFAULT 'yile'"))
        connection.execute(text("ALTER TABLE update_runs ADD COLUMN IF NOT EXISTS repository VARCHAR(240) NOT NULL DEFAULT ''"))
    with SessionLocal() as db:
        if db.scalar(select(User.id).limit(1)) is None:
            if len(config.ADMIN_PASSWORD) < 12:
                raise RuntimeError('ADMIN_PASSWORD must contain at least 12 characters')
            db.add(User(username=config.ADMIN_USERNAME, password_hash=password_hash(config.ADMIN_PASSWORD), role='admin'))
            db.commit()
    yield


app = FastAPI(title='多平台价格监控', version=VERSION, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET or 'unconfigured-startup-will-fail',
                   session_cookie='price_monitor_session', https_only=config.COOKIE_SECURE,
                   same_site='strict', max_age=86400)


@app.middleware('http')
async def response_headers(request, call_next):
    if (request.url.path.startswith('/api/') and request.method not in ('GET', 'HEAD', 'OPTIONS')
            and request.url.path not in ('/api/auth/login', '/api/auth/logout') and maintenance_active()):
        return JSONResponse({'detail': '系统正在升级，请稍后再试。'}, status_code=503, headers={'Retry-After': '30'})
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(UpdateError)
async def update_error(request, exc):
    return JSONResponse({'detail': exc.message, 'error_code': exc.code}, status_code=exc.status_code)


app.include_router(update_router)


def require(db, model, identifier):
    instance = db.get(model, identifier)
    if not instance:
        raise HTTPException(404, '记录不存在')
    return instance


def page_result(items, total=None, page=1, page_size=50):
    return {'items': items, 'total': len(items) if total is None else total, 'page': page, 'page_size': page_size}


def platform_dict(db, p):
    family, version = adapter_info(p.adapter)
    count = db.scalar(select(func.count()).select_from(Product).where(Product.platform_id == p.id))
    return {'id': p.id, 'name': p.name, 'base_url': p.base_url, 'adapter': p.adapter,
            'platform_type': p.platform_type, 'platform_type_name': family['name'] if family and family['id'] == p.platform_type else '待识别类型',
            'adapter_name': version['name'] if version else '待识别版本', 'adapter_supported': adapter_supported(p),
            'adapter_note': version['note'] if version else '需要核对采集版本',
            'login_path': version['login_path'] if version else '/',
            'token_storage_key': version['token_storage_key'] if version else '',
            'type_locked': bool(count or p.encrypted_session or p.active_rule_id),
            'account_label': p.account_label, 'enabled': p.enabled, 'interval_seconds': p.interval_seconds,
            'request_interval': float(p.request_interval), 'auth_status': p.auth_status,
            'last_success_at': iso(p.last_success_at), 'last_error': p.last_error,
            'active_rule_id': p.active_rule_id,
            'product_count': count}


def rule_dict(rule):
    return {'id': rule.id, 'platform_id': rule.platform_id, 'version': rule.version,
            'status': rule.status, 'config': rule.config, 'notes': rule.notes,
            'created_at': iso(rule.created_at), 'tested_at': iso(rule.tested_at), 'test_ok': rule.test_ok}


def default_rule():
    import copy
    return copy.deepcopy(DEFAULT_RULE)


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


@app.get('/api/health')
def health():
    checks = {'database': 'ok', 'redis': 'ok'}
    try:
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
    except Exception:
        checks['database'] = 'error'
    try:
        redis_client().ping()
    except Exception:
        checks['redis'] = 'error'
    ready = all(v == 'ok' for v in checks.values())
    return JSONResponse({'status': 'ok' if ready else 'degraded', **checks, 'time': iso(utcnow())}, status_code=200 if ready else 503)


@app.post('/api/auth/login')
def login(body: LoginInput, request: Request, db: Session = Depends(get_db)):
    client_key = 'pm:login:' + (request.client.host if request.client else 'local')
    rc = redis_client()
    if int(rc.get(client_key) or 0) >= 10:
        raise HTTPException(429, '尝试次数过多，请稍后重试')
    user = db.scalar(select(User).where(User.username == body.username))
    if not user or not user.enabled or not password_matches(body.password, user.password_hash):
        rc.incr(client_key)
        rc.expire(client_key, 300)
        raise HTTPException(401, '账号或密码错误')
    rc.delete(client_key)
    request.session.clear()
    request.session.update({'user_id': user.id, 'csrf': secrets.token_urlsafe(32)})
    audit(db, user, 'login', 'session')
    db.commit()
    return {'id': user.id, 'username': user.username, 'role': user.role, 'csrf_token': request.session['csrf']}


@app.get('/api/auth/me')
def me(request: Request, user: User = Depends(current_user)):
    return {'id': user.id, 'username': user.username, 'role': user.role, 'csrf_token': request.session.get('csrf')}


@app.post('/api/auth/logout')
def logout(request: Request, user: User = Depends(current_user)):
    if request.headers.get('X-CSRF-Token') != request.session.get('csrf'):
        raise HTTPException(403, '请求校验失败')
    request.session.clear()
    return {'ok': True}


class PlatformInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str
    platform_type: Literal['yile', 'yike'] | None = None
    adapter: Literal['xiaohong66', 'fans8', 'yike'] | None = None
    account_label: str = Field(default='主采集账号', min_length=1, max_length=120)
    enabled: bool = True
    interval_seconds: int = Field(default=3600, ge=3600, le=604800)
    request_interval: Decimal = Field(default=Decimal('1'), ge=1, le=60)

    @field_validator('base_url')
    @classmethod
    def validate_url(cls, value):
        return validate_base_url(value)

    @model_validator(mode='after')
    def bind_adapter(self):
        if self.adapter is None:
            self.adapter = 'fans8' if self.platform_type == 'yike' else 'xiaohong66'
        family, version = adapter_info(self.adapter)
        if self.platform_type is None:
            self.platform_type = family['id']
        if self.platform_type != family['id']:
            raise ValueError('平台类型与采集版本不匹配')
        if not version['implemented']:
            self.enabled = False
        return self


@app.get('/api/platform-types')
def platform_types(user=Depends(current_user)):
    return {'items': PLATFORM_TYPES}


@app.get('/api/platforms')
def platforms(db: Session = Depends(get_db), user=Depends(current_user)):
    rows = db.scalars(select(Platform).order_by(Platform.id)).all()
    return page_result([platform_dict(db, p) for p in rows])


@app.post('/api/platforms')
def create_platform(body: PlatformInput, db: Session = Depends(get_db), user=Depends(admin_user)):
    p = Platform(**body.model_dump())
    db.add(p)
    db.flush()
    rule = Rule(platform_id=p.id, version=1, status='draft', config=adapter_default_rule(p.adapter), notes='初始规则，连接账号后测试并发布')
    db.add(rule)
    audit(db, user, 'create_platform', str(p.id))
    db.commit()
    return platform_dict(db, p)


@app.put('/api/platforms/{platform_id}')
def edit_platform(platform_id: int, body: PlatformInput, db: Session = Depends(get_db), user=Depends(admin_user)):
    p = require(db, Platform, platform_id)
    db.refresh(p, with_for_update=True)
    changed_version = body.adapter != p.adapter or body.platform_type != p.platform_type
    if changed_version:
        if p.encrypted_session or p.active_rule_id or db.scalar(select(Product.id).where(Product.platform_id == p.id).limit(1)):
            raise HTTPException(409, '已有会话、已发布规则或商品历史，请新增平台连接以更换类型或版本')
        if db.scalar(select(CollectionRun.id).where(CollectionRun.platform_id == p.id, CollectionRun.status.in_(['running', 'queued']))):
            raise HTTPException(409, '请等待当前采集批次结束后更换版本')
        rules = db.scalars(select(Rule).where(Rule.platform_id == p.id)).all()
        for previous in rules:
            previous.status, previous.test_ok = 'archived', False
        db.add(Rule(platform_id=p.id, version=max((r.version for r in rules), default=0)+1,
                    status='draft', config=adapter_default_rule(body.adapter), notes='更换平台版本后的初始规则'))
        p.auth_status, p.account_fingerprint, p.last_error = 'missing', None, None
    if body.base_url != p.base_url or body.account_label != p.account_label:
        if body.account_label != p.account_label and db.scalar(select(Product.id).where(Product.platform_id == p.id).limit(1)):
            raise HTTPException(409, '采集账号标识用于关联历史，已有商品后不能更改；新账号请新增平台连接')
        if db.scalar(select(CollectionRun.id).where(CollectionRun.platform_id == p.id, CollectionRun.status.in_(['running', 'queued']))):
            raise HTTPException(409, '请等待当前采集批次结束后修改地址或账号')
        if body.base_url != p.base_url:
            p.encrypted_session, p.auth_status = None, 'missing'
    for key, value in body.model_dump().items():
        setattr(p, key, value)
    audit(db, user, 'update_platform', str(p.id))
    db.commit()
    return platform_dict(db, p)


class SessionInput(BaseModel):
    token: str = Field(min_length=10, max_length=16384)


@app.put('/api/platforms/{platform_id}/session')
async def connect_session(platform_id: int, body: SessionInput, db: Session = Depends(get_db), user=Depends(admin_user)):
    p = require(db, Platform, platform_id)
    db.refresh(p, with_for_update=True)
    require_supported(p)
    configured = db.get(Rule, p.active_rule_id) if p.active_rule_id else db.scalar(select(Rule).where(Rule.platform_id == p.id).order_by(Rule.version.desc()))
    async with platform_operation(p) as gate:
        collector = None
        try:
            source_class = Xiaohong66Collector if p.adapter == 'xiaohong66' else Fans8Collector
            collector = source_class(p.base_url, body.token, configured.config if configured else adapter_default_rule(p.adapter), request_gate=gate, sleep=gate.sleep)
            await collector.check_session()
        except CollectorError as exc:
            raise HTTPException(400, '会话校验失败，请确认账号已登录且平台地址正确') from exc
        finally:
            if collector is not None:
                await collector.aclose()
        account_identity = getattr(collector, 'account_identity', None)
        if not account_identity:
            raise HTTPException(400, '已登录，但未能识别平台账号编号，请核对适配器后重新连接')
        fingerprint = hashlib.sha256(str(account_identity).encode()).hexdigest() if account_identity else None
        if p.account_fingerprint and fingerprint and p.account_fingerprint != fingerprint:
            raise HTTPException(409, '连接账号与原采集账号不同，请为新账号新增平台连接')
        if fingerprint:
            p.account_fingerprint = fingerprint
    p.encrypted_session = encrypt_session(body.token)
    p.auth_status, p.last_error, p.next_due_at = 'connected', None, utcnow()
    audit(db, user, 'connect_session', str(p.id))
    db.commit()
    return platform_dict(db, p)


@app.post('/api/platforms/{platform_id}/collect')
def collect_now(platform_id: int, db: Session = Depends(get_db), user=Depends(admin_user)):
    p = require(db, Platform, platform_id)
    require_supported(p)
    if p.auth_status != 'connected':
        raise HTTPException(400, '请先连接采集账号')
    try:
        db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': 800000+p.id})
        run = queue_run(db, p)
        # A manual collection becomes the first sample of the next hourly
        # interval. Do not immediately enqueue an older connection-time slot.
        next_due = run.scheduled_at + timedelta(seconds=p.interval_seconds)
        if p.next_due_at < next_due:
            p.next_due_at = next_due
        audit(db, user, 'collect_now', str(p.id))
        db.commit()
        dispatch_one(run.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, '批次已记录，队列暂不可用，调度器将自动重试') from exc
    return run_dict(run, p)


@app.get('/api/platforms/{platform_id}/default-rule')
def get_default_rule(platform_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    p = require(db, Platform, platform_id)
    return adapter_default_rule(p.adapter)


class RuleInput(BaseModel):
    config: dict
    notes: str = Field(default='', max_length=2000)


def validate_rule(value):
    validate_rule_config(value)


@app.get('/api/platforms/{platform_id}/rules')
def list_rules(platform_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    require(db, Platform, platform_id)
    return page_result([rule_dict(r) for r in db.scalars(select(Rule).where(Rule.platform_id == platform_id).order_by(Rule.version.desc()))])


@app.post('/api/platforms/{platform_id}/rules')
def create_rule(platform_id: int, body: RuleInput, db: Session = Depends(get_db), user=Depends(admin_user)):
    p = require(db, Platform, platform_id)
    db.refresh(p, with_for_update=True)
    validate_adapter_rule(p.adapter, body.config)
    db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': 900000+platform_id})
    version = (db.scalar(select(func.max(Rule.version)).where(Rule.platform_id == platform_id)) or 0)+1
    rule = Rule(platform_id=platform_id, version=version, config=body.config, notes=body.notes)
    db.add(rule)
    audit(db, user, 'create_rule', f'{platform_id}:{version}')
    db.commit()
    return rule_dict(rule)


class RuleTestInput(BaseModel):
    source_product_id: str | None = None


@app.post('/api/rules/{rule_id}/test')
async def test_rule(rule_id: int, body: RuleTestInput, db: Session = Depends(get_db), user=Depends(admin_user)):
    rule = require(db, Rule, rule_id)
    p = require(db, Platform, rule.platform_id)
    db.refresh(p, with_for_update=True)
    db.refresh(rule)
    require_supported(p)
    if not p.encrypted_session:
        raise HTTPException(400, '请先连接采集账号')
    validate_adapter_rule(p.adapter, rule.config)
    async with platform_operation(p) as gate:
        collector = None
        try:
            source_class = Xiaohong66Collector if p.adapter == 'xiaohong66' else Fans8Collector
            collector = source_class(p.base_url, decrypt_session(p.encrypted_session), rule.config, request_gate=gate, sleep=gate.sleep)
            categories = await collector.list_categories()
            if body.source_product_id:
                products = [await collector.get_product(body.source_product_id)]
            else:
                iterator = collector.iter_pages()
                products = await anext(iterator, [])
                await iterator.aclose()
            rule.tested_at, rule.test_ok = utcnow(), bool(products)
            db.commit()
            return {'ok': rule.test_ok, 'products': [json_safe({
                'source_id': s.source_id, 'name': s.name, 'price': s.price,
                'unit': s.unit, 'display_quantity': s.display_quantity,
                'display_price': display_price(s.price, s.display_quantity),
                'status': s.status, 'quality': s.quality}) for s in products[:10]], 'categories': json_safe(categories), 'errors': []}
        except CollectorError as exc:
            rule.tested_at, rule.test_ok = utcnow(), False
            db.commit()
            return {'ok': False, 'products': [], 'categories': [], 'errors': [str(exc)[:500]]}
        finally:
            if collector is not None:
                await collector.aclose()


@app.post('/api/rules/{rule_id}/publish')
def publish_rule(rule_id: int, db: Session = Depends(get_db), user=Depends(admin_user)):
    rule = require(db, Rule, rule_id)
    p = require(db, Platform, rule.platform_id)
    db.refresh(p, with_for_update=True)
    db.refresh(rule)
    require_supported(p)
    validate_adapter_rule(p.adapter, rule.config)
    if not rule.test_ok:
        raise HTTPException(400, '请先测试此版本并确认解析成功')
    if db.scalar(select(CollectionRun.id).where(CollectionRun.platform_id == p.id, CollectionRun.status == 'running')):
        raise HTTPException(409, '当前批次正在运行，请结束后发布')
    for previous in db.scalars(select(Rule).where(Rule.platform_id == p.id, Rule.status == 'published')):
        previous.status = 'archived'
    rule.status, p.active_rule_id = 'published', rule.id
    audit(db, user, 'publish_rule', str(rule.id))
    db.commit()
    return rule_dict(rule)


@app.get('/api/products')
def products(platform_id: int | None = None, q: str = '', status: str | None = None,
             category: str = '', page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
             db: Session = Depends(get_db), user=Depends(current_user)):
    query = select(Product)
    if platform_id:
        query = query.where(Product.platform_id == platform_id)
    if q:
        query = query.where(or_(Product.name.ilike(f'%{q}%'), Product.source_id.ilike(f'%{q}%')))
    if status:
        query = query.where(Product.status == status)
    if category:
        query = query.where(Product.category_name == category)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(Product.id).offset((page-1)*page_size).limit(page_size)).all()
    platforms_by_id = {p.id: p for p in db.scalars(select(Platform))}
    return page_result([product_dict(p, platforms_by_id.get(p.platform_id)) for p in rows], total, page, page_size)


@app.get('/api/products/{product_id}')
def product_detail(product_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    p = require(db, Product, product_id)
    return product_dict(p, db.get(Platform, p.platform_id))


@app.get('/api/products/{product_id}/history')
def history(product_id: int, days: int = Query(7, ge=1, le=730), db: Session = Depends(get_db), user=Depends(current_user)):
    p = require(db, Product, product_id)
    platform = db.get(Platform, p.platform_id)
    interval_seconds = platform.interval_seconds
    cutoff = utcnow()-timedelta(days=days)
    observations = db.scalars(select(Observation).where(Observation.product_id == p.id, Observation.observed_at >= cutoff).order_by(Observation.scheduled_at)).all()
    runs = db.scalars(select(CollectionRun).where(CollectionRun.platform_id == p.platform_id,
        CollectionRun.account_label == p.account_label, CollectionRun.scheduled_at >= cutoff).order_by(CollectionRun.scheduled_at)).all()
    by_run = {o.run_id: o for o in observations}
    points = []
    for run in runs:
        o = by_run.get(run.id)
        points.append({'scheduled_at': iso(run.scheduled_at), 'observed_at': iso(o.observed_at) if o else None,
            'price': decimal_text(o.price) if o and o.status == 'active' and price_quality_valid(o.quality) else None,
            'raw_price': decimal_text(o.price) if o else None,
            'display_price': decimal_text(display_price(o.price, o.display_quantity)) if o and o.status == 'active' and price_quality_valid(o.quality) else None,
            'display_quantity': decimal_text(o.display_quantity) if o else None,
            'unit': o.unit if o else None, 'currency': o.currency if o else None,
            'status': o.status if o else 'missing', 'quality': o.quality if o else ['collection_gap']})
    aggregates = db.scalars(select(DailyAggregate).where(DailyAggregate.product_id == p.id,
        DailyAggregate.day >= cutoff.date().isoformat()).order_by(DailyAggregate.day)).all()
    events = db.scalars(select(Event).where(Event.product_id == p.id, Event.created_at >= cutoff).order_by(Event.created_at)).all()
    # A down scheduler creates no CollectionRun. Expose real gaps instead of
    # drawing a straight line across those unobserved hours.
    with_gaps = []
    previous = None
    for point in points:
        scheduled = datetime.fromisoformat(point['scheduled_at'])
        if previous is not None:
            gap = previous+timedelta(seconds=interval_seconds)
            while gap < scheduled-timedelta(seconds=interval_seconds*0.5):
                with_gaps.append({'scheduled_at': iso(gap), 'observed_at': None, 'price': None,
                    'raw_price': None, 'display_price': None, 'display_quantity': None,
                    'unit': None, 'currency': None, 'status': 'missing', 'quality': ['collection_gap']})
                gap += timedelta(seconds=interval_seconds)
        with_gaps.append(point)
        previous = scheduled
    return {'items': with_gaps, 'interval_seconds': interval_seconds, 'events': [event_dict(e) for e in events], 'daily': [
        {'date': a.day, 'min': decimal_text(a.minimum), 'max': decimal_text(a.maximum), 'avg': decimal_text(a.average),
         'count': a.count, 'unit': a.unit, 'currency': a.currency, 'display_quantity': decimal_text(a.display_quantity)} for a in aggregates]}


@app.get('/api/runs')
def runs(platform_id: int | None = None, status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), db: Session = Depends(get_db), user=Depends(current_user)):
    query = select(CollectionRun)
    if platform_id:
        query = query.where(CollectionRun.platform_id == platform_id)
    if status:
        query = query.where(CollectionRun.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(CollectionRun.id.desc()).offset((page-1)*page_size).limit(page_size)).all()
    ps = {p.id: p for p in db.scalars(select(Platform))}
    return page_result([run_dict(r, ps.get(r.platform_id)) for r in rows], total, page, page_size)


@app.get('/api/events')
def events(kind: str = '', acknowledged: bool | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), db: Session = Depends(get_db), user=Depends(current_user)):
    query = select(Event)
    if kind:
        query = query.where(Event.kind == kind)
    if acknowledged is not None:
        query = query.where(Event.acknowledged == acknowledged)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    return page_result([event_dict(e) for e in db.scalars(query.order_by(Event.id.desc()).offset((page-1)*page_size).limit(page_size))], total, page, page_size)


@app.post('/api/events/{event_id}/ack')
def acknowledge(event_id: int, db: Session = Depends(get_db), user=Depends(admin_user)):
    event = require(db, Event, event_id)
    event.acknowledged = True
    audit(db, user, 'ack_event', str(event.id))
    db.commit()
    return event_dict(event)


@app.get('/api/dashboard')
def dashboard(db: Session = Depends(get_db), user=Depends(current_user)):
    ps = {p.id: p for p in db.scalars(select(Platform))}
    return {'platform_count': len(ps), 'product_count': db.scalar(select(func.count()).select_from(Product)),
        'active_count': db.scalar(select(func.count()).select_from(Product).where(Product.status == 'active')),
        'closed_count': db.scalar(select(func.count()).select_from(Product).where(Product.status == 'closed')),
        'auth_expired_count': sum(p.auth_status != 'connected' for p in ps.values()),
        'last_collection_at': iso(db.scalar(select(func.max(Platform.last_success_at)))),
        'recent_runs': [run_dict(r, ps.get(r.platform_id)) for r in db.scalars(select(CollectionRun).order_by(CollectionRun.id.desc()).limit(8))],
        'recent_events': [event_dict(e) for e in db.scalars(select(Event).order_by(Event.id.desc()).limit(8))],
        'daily_counts': [{'date': str(day), 'count': count} for day, count in db.execute(select(func.date(Observation.observed_at), func.count()).where(Observation.observed_at >= utcnow()-timedelta(days=14)).group_by(func.date(Observation.observed_at)).order_by(func.date(Observation.observed_at)))]}


@app.get('/api/ops/stats')
def ops_stats(db: Session = Depends(get_db), user=Depends(admin_user)):
    counts = dict(db.execute(select(CollectionRun.status, func.count()).group_by(CollectionRun.status)).all())
    return {'batches': {s: counts.get(s, 0) for s in ('queued', 'running', 'success', 'partial', 'failed', 'auth_expired')},
        'latest_batches': [run_dict(r) for r in db.scalars(select(CollectionRun).order_by(CollectionRun.id.desc()).limit(100))],
        'platforms': [{'id': p.id, 'name': p.name, 'enabled': p.enabled, 'auth_status': p.auth_status,
            'interval_seconds': p.interval_seconds, 'last_success_at': iso(p.last_success_at),
            'successful_batches': db.scalar(select(func.count()).select_from(CollectionRun).where(CollectionRun.platform_id == p.id, CollectionRun.status == 'success'))} for p in db.scalars(select(Platform))],
        'observations': db.scalar(select(func.count()).select_from(Observation)),
        'total_products': db.scalar(select(func.count()).select_from(Product)), 'uptime_seconds': int(time.monotonic()-STARTED)}


def user_dict(u):
    return {'id': u.id, 'username': u.username, 'role': u.role, 'enabled': u.enabled, 'created_at': iso(u.created_at)}


class UserInput(BaseModel):
    username: str = Field(min_length=2, max_length=80, pattern=r'^[a-zA-Z0-9_.@-]+$')
    password: str = Field(min_length=12, max_length=256)
    role: Literal['admin', 'viewer'] = 'viewer'


class UserPatch(BaseModel):
    role: Literal['admin', 'viewer'] | None = None
    enabled: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=256)


@app.get('/api/users')
def users(db: Session = Depends(get_db), user=Depends(admin_user)):
    return page_result([user_dict(u) for u in db.scalars(select(User).order_by(User.id))])


@app.post('/api/users')
def create_user(body: UserInput, db: Session = Depends(get_db), user=Depends(admin_user)):
    if db.scalar(select(User.id).where(User.username == body.username)):
        raise HTTPException(409, '账号已存在')
    target = User(username=body.username, password_hash=password_hash(body.password), role=body.role)
    db.add(target)
    audit(db, user, 'create_user', body.username)
    db.commit()
    return user_dict(target)


@app.patch('/api/users/{user_id}')
def edit_user(user_id: int, body: UserPatch, db: Session = Depends(get_db), user=Depends(admin_user)):
    target = require(db, User, user_id)
    if target.id == user.id and (body.enabled is False or body.role == 'viewer'):
        raise HTTPException(400, '不能停用或降级当前管理员账号')
    for key, value in body.model_dump(exclude_none=True).items():
        if key == 'password':
            target.password_hash = password_hash(value)
        else:
            setattr(target, key, value)
    audit(db, user, 'update_user', str(target.id))
    db.commit()
    return user_dict(target)


class MappingInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    unit: str = Field(default='', max_length=80)
    currency: str = Field(default='CNY', max_length=8)
    product_ids: list[int] = Field(default_factory=list, max_length=100)


def mapping_dict(db, group):
    products = db.scalars(select(Product).where(Product.canonical_id == group.id)).all()
    ps = {p.id: p for p in db.scalars(select(Platform))}
    return {'id': group.id, 'name': group.name, 'unit': group.unit, 'currency': group.currency,
            'product_ids': [p.id for p in products], 'products': [product_dict(p, ps.get(p.platform_id)) for p in products]}


@app.get('/api/mappings')
def mappings(db: Session = Depends(get_db), user=Depends(current_user)):
    return page_result([mapping_dict(db, g) for g in db.scalars(select(CanonicalProduct).order_by(CanonicalProduct.id))])


def save_mapping(db, group, body):
    selected = db.scalars(select(Product).where(Product.id.in_(body.product_ids))).all() if body.product_ids else []
    if len(selected) != len(set(body.product_ids)):
        raise HTTPException(422, '包含不存在的商品')
    if any(p.unit != body.unit or p.currency != body.currency for p in selected):
        raise HTTPException(422, '商品单位、币种须与统一商品一致')
    if any(p.canonical_id and p.canonical_id != group.id for p in selected):
        raise HTTPException(409, '商品已关联其他统一商品，请先解除原关联')
    group.name, group.unit, group.currency = body.name, body.unit, body.currency
    for p in db.scalars(select(Product).where(Product.canonical_id == group.id)):
        p.canonical_id = None
    for p in selected:
        p.canonical_id = group.id


@app.post('/api/mappings')
def create_mapping(body: MappingInput, db: Session = Depends(get_db), user=Depends(admin_user)):
    group = CanonicalProduct(name=body.name, unit=body.unit, currency=body.currency)
    db.add(group)
    db.flush()
    save_mapping(db, group, body)
    audit(db, user, 'create_mapping', str(group.id))
    db.commit()
    return mapping_dict(db, group)


@app.put('/api/mappings/{mapping_id}')
def edit_mapping(mapping_id: int, body: MappingInput, db: Session = Depends(get_db), user=Depends(admin_user)):
    group = require(db, CanonicalProduct, mapping_id)
    save_mapping(db, group, body)
    audit(db, user, 'update_mapping', str(group.id))
    db.commit()
    return mapping_dict(db, group)


@app.delete('/api/mappings/{mapping_id}')
def delete_mapping(mapping_id: int, db: Session = Depends(get_db), user=Depends(admin_user)):
    group = require(db, CanonicalProduct, mapping_id)
    for p in db.scalars(select(Product).where(Product.canonical_id == group.id)):
        p.canonical_id = None
    db.flush()
    db.delete(group)
    audit(db, user, 'delete_mapping', str(mapping_id))
    db.commit()
    return {'ok': True}


@app.get('/api/mappings/{mapping_id}/compare')
def compare(mapping_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    group = require(db, CanonicalProduct, mapping_id)
    data = mapping_dict(db, group)
    all_products = data['products']
    active = [p for p in all_products if p['status'] == 'active' and not p['quality'] and p['freshness'] == 'fresh'
              and p['unit'] == group.unit and p['currency'] == group.currency]
    # Undeclared units never yield a cross-platform minimum.
    comparable = bool(group.unit) and len(active) >= 2
    if comparable:
        observed = [datetime.fromisoformat(p['last_observed_at']) for p in active]
        comparable = (max(observed)-min(observed)).total_seconds() <= 3600
    return {'items': sorted(active, key=lambda p: Decimal(p['price'])) if comparable else all_products,
            'comparable': comparable, 'reason': '' if comparable else '至少两个有效在售报价、明确相同单位币种且采样相差不超过一小时后方可比较'}


FRONTEND = config.ROOT / 'frontend' / 'dist'


@app.get('/{path:path}', include_in_schema=False)
def frontend(path: str):
    if path.startswith('api/'):
        raise HTTPException(404, '接口不存在')
    target = (FRONTEND/path).resolve()
    if FRONTEND.resolve() not in target.parents and target != FRONTEND.resolve():
        raise HTTPException(404)
    if target.is_file():
        return FileResponse(target)
    if (FRONTEND/'index.html').is_file():
        return FileResponse(FRONTEND/'index.html')
    return JSONResponse({'message': '前端尚未构建，请在 frontend 执行 npm run build'}, status_code=503)
