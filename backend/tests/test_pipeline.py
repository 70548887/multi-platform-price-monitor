"""Integration tests against an isolated real PostgreSQL database, never production.

This module uses price_monitor_test and Redis DB 15 on the configured project instance.
Source HTTP is replaced with deterministic collector fixtures; no upstream traffic occurs.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values
import psycopg
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
env_values = dotenv_values(ROOT / ".env")
source_url = os.getenv("TEST_DATABASE_URL") or env_values.get("DATABASE_URL")
if not source_url:
    raise RuntimeError("Integration tests require TEST_DATABASE_URL or the project .env")
test_url = make_url(source_url).set(database="price_monitor_test")
if test_url.database != "price_monitor_test" or test_url.host not in ("127.0.0.1", "localhost"):
    raise RuntimeError("Integration tests are restricted to local price_monitor_test")
os.environ["DATABASE_URL"] = test_url.render_as_string(hide_password=False)
redis_parts = urlsplit(os.getenv("TEST_REDIS_URL") or env_values.get("REDIS_URL", "redis://127.0.0.1:56379/0"))
if redis_parts.hostname not in ("127.0.0.1", "localhost"):
    raise RuntimeError("Integration tests require a local Redis instance")
os.environ["REDIS_URL"] = urlunsplit(redis_parts._replace(path="/15"))

# Import only after setting explicit test resources; dotenv does not override environment values.
from fastapi.testclient import TestClient  # noqa: E402
from app import config, tasks  # noqa: E402
from app.collectors import IncompleteCollection  # noqa: E402
from app.collectors.base import SourceProduct  # noqa: E402
from app.db import Base, SessionLocal, engine, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.operations import platform_operation  # noqa: E402
from app.models import CollectionRun, DailyAggregate, ErrorCapture, Event, Observation, Platform, Product, Rule, User, utcnow  # noqa: E402
from app.security import encrypt_session, password_hash  # noqa: E402
from app.services import persist_page, product_dict  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def isolated_database():
    admin_url = test_url.set(drivername="postgresql", database="postgres")
    with psycopg.connect(admin_url.render_as_string(hide_password=False), autocommit=True) as connection:
        if not connection.execute("SELECT 1 FROM pg_database WHERE datname='price_monitor_test'").fetchone():
            connection.execute("CREATE DATABASE price_monitor_test")
    if engine.url.database != "price_monitor_test":
        raise RuntimeError("Refusing to run destructive test cleanup on any other database")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE platforms ADD COLUMN IF NOT EXISTS account_fingerprint VARCHAR(64)'))
        connection.execute(text("ALTER TABLE platforms ADD COLUMN IF NOT EXISTS platform_type VARCHAR(40) NOT NULL DEFAULT 'yile'"))
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_test_resources(isolated_database):
    assert engine.url.database == "price_monitor_test"
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    client = tasks.redis_client()
    for key in client.scan_iter("pm:*"):
        client.delete(key)
    yield


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session
        session.rollback()


def source(*, identifier="9585", name="高精度商品", price="0.007486", quantity="1000", unit="人", status="active"):
    return SourceProduct(
        source_id=identifier, name=name, category_id="12", category_name="测试分类",
        price=Decimal(price), display_quantity=Decimal(quantity), unit=unit,
        status=status, stock=None, stock_unlimited=True,
        raw={"price": price, "price_show_num": quantity, "currency": "CNY"},
    )


def platform_and_rule(db, *, name="本地测试平台"):
    platform = Platform(name=name, base_url="https://source.example", account_label="test-account",
                        auth_status="connected", encrypted_session=encrypt_session("test-session-no-network"))
    db.add(platform)
    db.flush()
    rule = Rule(platform_id=platform.id, version=1, status="published", config={})
    db.add(rule)
    db.flush()
    platform.active_rule_id = rule.id
    return platform, rule


def run_for(db, platform, rule, *, offset=0, status="success"):
    run = CollectionRun(platform_id=platform.id, rule_id=rule.id, account_label=platform.account_label,
                        scheduled_at=utcnow() + timedelta(hours=offset), status=status)
    db.add(run)
    db.flush()
    return run


@pytest.fixture
def admin_client(db):
    db.add(User(username="test-admin", password_hash=password_hash("test-admin-password"), role="admin"))
    db.commit()
    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={"username": "test-admin", "password": "test-admin-password"})
        assert response.status_code == 200
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        yield client


def test_exact_decimal_and_display_multiplier_roundtrip(db):
    platform, rule = platform_and_rule(db)
    run = run_for(db, platform, rule)
    persist_page(db, run, [source()])
    db.commit()
    db.expire_all()
    product = db.scalar(select(Product))
    observation = db.scalar(select(Observation))
    assert product.price == Decimal("0.007486")
    assert observation.price == Decimal("0.007486")
    assert product.display_quantity == Decimal("1000")
    rendered = product_dict(product, platform)
    assert Decimal(rendered["price"]) == Decimal("0.007486")
    assert Decimal(rendered["display_price"]) == Decimal("7.486")
    assert observation.raw["price"] == "0.007486"


def test_zero_display_quantity_without_unit_keeps_source_history_and_daily_summary(db, admin_client, monkeypatch):
    fixed_now = utcnow()
    yesterday = fixed_now.replace(hour=0, minute=0, second=0, microsecond=0)-timedelta(days=1)
    platform, rule = platform_and_rule(db)
    run = run_for(db, platform, rule)
    first = source(price="0.007447", quantity="0", unit="")
    first.quality = ["unit_requires_review"]
    second = source(identifier="second", price="0.008", quantity="0", unit="")
    second.quality = ["unit_requires_review"]
    persist_page(db, run, [first, second], observed_at=yesterday+timedelta(hours=10))
    db.commit()
    product = db.scalar(select(Product).where(Product.source_id == "9585"))
    history = admin_client.get(f"/api/products/{product.id}/history?days=7")
    assert history.status_code == 200
    assert history.json()["items"][0]["price"] == "0.007447"
    assert history.json()["items"][0]["display_price"] == "0.007447"
    monkeypatch.setattr(tasks, "utcnow", lambda: fixed_now)
    tasks.retain_history.run()
    db.expire_all()
    summary = db.scalar(select(DailyAggregate).where(DailyAggregate.product_id == product.id))
    assert summary is not None
    assert summary.minimum == summary.maximum == summary.average == Decimal("0.007447")
    assert summary.display_quantity == 0
    mapping = admin_client.post("/api/mappings", json={"name": "未声明单位的人工映射", "unit": "", "currency": "CNY",
        "product_ids": list(db.scalars(select(Product.id)))})
    assert mapping.status_code == 200
    comparison = admin_client.get(f"/api/mappings/{mapping.json()['id']}/compare").json()
    assert comparison["comparable"] is False


def test_manual_collection_advances_hourly_schedule_without_immediate_duplicate(db, admin_client, monkeypatch):
    from app import main as api
    platform, rule = platform_and_rule(db)
    platform.next_due_at = utcnow()-timedelta(hours=1)
    db.commit()
    monkeypatch.setattr(api, "dispatch_one", lambda run_id: None)
    monkeypatch.setattr(tasks, "dispatch_one", lambda run_id: None)
    response = admin_client.post(f"/api/platforms/{platform.id}/collect")
    assert response.status_code == 200
    db.expire_all()
    run = db.get(CollectionRun, response.json()["id"])
    assert db.get(Platform, platform.id).next_due_at == run.scheduled_at+timedelta(seconds=platform.interval_seconds)
    run.status, run.completed_at = "success", utcnow()
    db.commit()
    tasks.dispatch_due.run()
    assert db.scalar(select(func.count()).select_from(CollectionRun)) == 1


def test_zero_price_closed_product_creates_status_event_without_price_alert(db):
    platform, rule = platform_and_rule(db)
    baseline = run_for(db, platform, rule, offset=-1)
    persist_page(db, baseline, [source(price="10")])
    closed_run = run_for(db, platform, rule)
    persist_page(db, closed_run, [source(price="0", status="closed")])
    db.commit()
    product = db.scalar(select(Product))
    assert product.status == "closed"
    assert product.price == 0
    assert list(db.scalars(select(Event.kind))) == ["closed"]


def test_product_rename_keeps_identity_and_history(db):
    platform, rule = platform_and_rule(db)
    before = run_for(db, platform, rule, offset=-1)
    persist_page(db, before, [source(name="原名称")])
    original_id = db.scalar(select(Product.id))
    after = run_for(db, platform, rule)
    persist_page(db, after, [source(name="新名称")])
    db.commit()
    assert db.scalar(select(func.count()).select_from(Product)) == 1
    assert db.get(Product, original_id).name == "新名称"
    assert list(db.scalars(select(Observation.name).order_by(Observation.id))) == ["原名称", "新名称"]
    assert list(db.scalars(select(Event.kind))) == ["renamed"]


def test_replayed_batch_has_one_observation_per_product(db):
    platform, rule = platform_and_rule(db)
    run = run_for(db, platform, rule)
    assert persist_page(db, run, [source(), source()]) == 1
    db.commit()
    assert persist_page(db, run, [source()]) == 0
    db.commit()
    assert db.scalar(select(func.count()).select_from(Observation)) == 1
    assert db.scalar(select(func.count()).select_from(Product)) == 1


def test_delayed_older_batch_preserves_newer_current_price(db):
    platform, rule = platform_and_rule(db)
    newer = run_for(db, platform, rule)
    persist_page(db, newer, [source(name="较新名称", price="12")])
    older = run_for(db, platform, rule, offset=-1)
    persist_page(db, older, [source(name="较旧名称", price="5")])
    db.commit()
    product = db.scalar(select(Product))
    assert product.price == Decimal("12")
    assert product.name == "较新名称"
    assert product.last_run_id == newer.id
    assert db.scalar(select(func.count()).select_from(Observation)) == 2
    assert db.scalar(select(func.count()).select_from(Event)) == 0


@pytest.mark.asyncio
async def test_partial_collection_keeps_unseen_products_active(db, monkeypatch):
    platform, rule = platform_and_rule(db)
    baseline = run_for(db, platform, rule, offset=-1)
    persist_page(db, baseline, [source(identifier="one"), source(identifier="two")])
    run = run_for(db, platform, rule, status="queued")
    db.commit()

    class PartialCollector:
        expected_total = 2

        def __init__(self, *args, **kwargs):
            pass

        async def iter_pages(self):
            yield [source(identifier="one", price="0.008")]
            raise IncompleteCollection("第二页响应失败")

        async def aclose(self):
            pass

    monkeypatch.setattr(tasks, "Xiaohong66Collector", PartialCollector)
    result = await tasks.execute_run(run.id)
    db.expire_all()
    assert result["status"] == "partial"
    assert db.get(CollectionRun, run.id).products_seen == 1
    assert db.scalar(select(Product).where(Product.source_id == "two")).status == "active"
    assert "closed" not in list(db.scalars(select(Event.kind)))


def test_incompatible_unit_mapping_is_rejected_and_existing_mapping_stops_comparing(db, admin_client):
    platform, rule = platform_and_rule(db)
    run = run_for(db, platform, rule)
    persist_page(db, run, [source(identifier="one", unit="人"), source(identifier="two", unit="月")])
    db.commit()
    products = list(db.scalars(select(Product).order_by(Product.id)))
    incompatible = admin_client.post("/api/mappings", json={
        "name": "不兼容映射", "unit": "人", "currency": "CNY", "product_ids": [product.id for product in products],
    })
    assert incompatible.status_code == 422
    valid = admin_client.post("/api/mappings", json={
        "name": "可比商品", "unit": "人", "currency": "CNY", "product_ids": [products[0].id],
    })
    assert valid.status_code == 200
    response = admin_client.get(f"/api/mappings/{valid.json()['id']}/compare")
    assert response.status_code == 200
    assert response.json()["comparable"] is False


def test_viewer_reads_prices_but_cannot_change_platforms(db):
    db.add(User(username="test-viewer", password_hash=password_hash("test-viewer-password"), role="viewer"))
    db.commit()
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "test-viewer", "password": "test-viewer-password"})
        assert login.status_code == 200
        assert client.get("/api/products").status_code == 200
        assert client.get("/api/ops/stats").status_code == 403
        response = client.post("/api/platforms", json={"name": "拒绝创建", "base_url": "https://source.example"},
                               headers={"X-CSRF-Token": login.json()["csrf_token"]})
        assert response.status_code == 403


def test_admin_mutations_require_matching_csrf(admin_client):
    token = admin_client.headers.pop("X-CSRF-Token")
    body = {"name": "CSRF验证平台", "base_url": "https://source.example"}
    assert admin_client.post("/api/platforms", json=body).status_code == 403
    assert admin_client.post("/api/platforms", json=body, headers={"X-CSRF-Token": "invalid"}).status_code == 403
    assert admin_client.post("/api/platforms", json=body, headers={"X-CSRF-Token": token}).status_code == 200
    admin_client.headers["X-CSRF-Token"] = token


def test_session_credentials_are_not_returned_by_platform_api(db, admin_client):
    platform, _ = platform_and_rule(db)
    db.commit()
    response = admin_client.get("/api/platforms")
    assert response.status_code == 200
    serialized = response.text
    assert "test-session-no-network" not in serialized
    assert platform.encrypted_session not in serialized
    assert "encrypted_session" not in serialized


def test_daily_aggregation_uses_whole_utc_dates_and_excludes_invalid_quotes(db, monkeypatch):
    fixed_now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    today = fixed_now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    platform, rule = platform_and_rule(db)
    for index, (observed, price, invalid) in enumerate([
        (yesterday + timedelta(hours=1), "1", False),
        (yesterday + timedelta(hours=18), "3", False),
        (yesterday + timedelta(hours=8), "0", True),
        (today + timedelta(hours=1), "999", False),
    ]):
        run = run_for(db, platform, rule, offset=index)
        item = source(price=price)
        if invalid:
            item.quality = ["invalid_price"]
        persist_page(db, run, [item], observed_at=observed)
    db.commit()
    monkeypatch.setattr(tasks, "utcnow", lambda: fixed_now)
    tasks.retain_history.run()
    tasks.retain_history.run()
    db.expire_all()
    rows = list(db.scalars(select(DailyAggregate)))
    assert len(rows) == 1
    assert rows[0].day == yesterday.date().isoformat()
    assert rows[0].count == 2
    assert rows[0].minimum == Decimal("1")
    assert rows[0].maximum == Decimal("3")
    assert rows[0].average == Decimal("2")


@pytest.mark.parametrize("warning", ["unit_requires_review", "invalid_stock"])
def test_noncritical_quality_warning_preserves_source_price_history_and_changes(db, admin_client, warning):
    platform, rule = platform_and_rule(db)
    first = source(price="1", unit="" if warning == "unit_requires_review" else "人")
    first.quality = [warning]
    second = replace(first, price=Decimal("2"))
    persist_page(db, run_for(db, platform, rule, offset=-1), [first])
    persist_page(db, run_for(db, platform, rule), [second])
    db.commit()
    product = db.scalar(select(Product))
    history = admin_client.get(f"/api/products/{product.id}/history")
    assert history.status_code == 200
    assert [Decimal(item["price"]) for item in history.json()["items"]] == [Decimal("1"), Decimal("2")]
    assert list(db.scalars(select(Event.kind))) == ["price_changed"]


@pytest.mark.parametrize("critical", ["invalid_price", "invalid_display_quantity"])
def test_critical_quality_creates_price_gap_without_false_change(db, admin_client, critical):
    platform, rule = platform_and_rule(db)
    persist_page(db, run_for(db, platform, rule, offset=-1), [source(price="1")])
    invalid = source(price="0")
    invalid.quality = [critical]
    persist_page(db, run_for(db, platform, rule), [invalid])
    db.commit()
    product = db.scalar(select(Product))
    response = admin_client.get(f"/api/products/{product.id}/history")
    assert response.status_code == 200
    assert response.json()["items"][-1]["price"] is None
    assert "price_changed" not in list(db.scalars(select(Event.kind)))


def test_lost_worker_without_lock_is_requeued_and_dispatched(db, monkeypatch):
    platform, rule = platform_and_rule(db)
    platform.next_due_at = utcnow() + timedelta(days=1)
    run = run_for(db, platform, rule, status="running")
    run.started_at = utcnow() - timedelta(minutes=10)
    run.heartbeat_at = utcnow() - timedelta(minutes=5)
    run.attempts = 1
    db.commit()
    dispatched = []
    monkeypatch.setattr(tasks, "dispatch_one", dispatched.append)
    result = tasks.dispatch_due.run()
    db.expire_all()
    assert db.get(CollectionRun, run.id).status == "queued"
    assert dispatched == [run.id]
    assert result == {"dispatched": 1}


def test_actual_celery_worker_persists_a_task_without_upstream_http(db):
    platform, rule = platform_and_rule(db)
    platform.next_due_at = utcnow() + timedelta(days=1)
    run = run_for(db, platform, rule, status="queued")
    db.commit()
    worker_name = f"pipeline-test-{os.getpid()}@local"
    queue_name = f"pipeline-test-{os.getpid()}"
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = str(ROOT / "backend" / "tests") + os.pathsep + str(ROOT / "backend")
    log_directory = ROOT / ".runtime" / "test-logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / "celery-integration.log"
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with log_path.open("w", encoding="utf-8") as log:
        worker = subprocess.Popen([
            sys.executable, "-m", "celery", "-A", "worker_harness:celery_app", "worker",
            "--pool=solo", "--concurrency=1", "--loglevel=warning", "--without-gossip",
            "--without-mingle", "--hostname", worker_name, "--queues", queue_name,
        ], cwd=ROOT / "backend", env=child_env, stdout=log, stderr=subprocess.STDOUT,
           creationflags=creationflags)
        try:
            ready = False
            for _ in range(20):
                if worker.poll() is not None:
                    pytest.fail(f"Isolated Celery worker exited; inspect {log_path}")
                response = tasks.celery_app.control.ping(destination=[worker_name], timeout=1)
                if response:
                    ready = True
                    break
                time.sleep(0.2)
            assert ready, f"Isolated Celery worker did not become ready; inspect {log_path}"
            result = tasks.collect_platform.apply_async(args=[run.id], queue=queue_name).get(timeout=20)
            assert result["status"] == "success"
            replay = tasks.collect_platform.apply_async(args=[run.id], queue=queue_name).get(timeout=20)
            assert replay == {"skipped": True}
            db.expire_all()
            product = db.scalar(select(Product))
            assert product.source_id == "9585"
            assert product.price == Decimal("0.007486")
            assert db.scalar(select(func.count()).select_from(Observation)) == 1
            assert db.get(CollectionRun, run.id).status == "success"
        finally:
            if worker.poll() is None:
                tasks.celery_app.control.broadcast("shutdown", destination=[worker_name])
                try:
                    worker.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    worker.terminate()
                    worker.wait(timeout=5)


@pytest.mark.parametrize("interval_seconds", [3600, 48 * 3600])
def test_history_fills_one_missing_slot_between_three_scheduled_points(db, admin_client, interval_seconds):
    platform, rule = platform_and_rule(db)
    platform.interval_seconds = interval_seconds
    before = run_for(db, platform, rule, offset=-(2 * interval_seconds / 3600))
    after = run_for(db, platform, rule)
    persist_page(db, before, [source(price="1")], observed_at=before.scheduled_at)
    persist_page(db, after, [source(price="2")], observed_at=after.scheduled_at)
    db.commit()
    product = db.scalar(select(Product))
    response = admin_client.get(f"/api/products/{product.id}/history?days=7")
    assert response.status_code == 200
    data = response.json()
    assert data["interval_seconds"] == interval_seconds
    assert len(data["items"]) == 3
    assert [point["status"] for point in data["items"]] == ["active", "missing", "active"]
    assert data["items"][1]["price"] is None
    assert data["items"][1]["quality"] == ["collection_gap"]
    assert datetime.fromisoformat(data["items"][1]["scheduled_at"]) == before.scheduled_at + timedelta(seconds=interval_seconds)
    assert admin_client.get(f"/api/products/{product.id}").json()["interval_seconds"] == interval_seconds


@pytest.mark.asyncio
async def test_preview_operation_shares_worker_lock_and_releases_only_own_lock(db):
    from fastapi import HTTPException

    platform, rule = platform_and_rule(db)
    run = run_for(db, platform, rule, status="queued")
    db.commit()
    client = tasks.redis_client()
    async with platform_operation(platform):
        result = await tasks.execute_run(run.id)
        assert result == {"overlap": True}
        with pytest.raises(HTTPException) as busy:
            async with platform_operation(platform):
                pytest.fail("Nested platform operation should have failed")
        assert busy.value.status_code == 409
        assert client.exists(f"pm:lock:{platform.id}") == 1
    assert client.exists(f"pm:lock:{platform.id}") == 0


@pytest.mark.asyncio
async def test_preview_and_account_checks_share_global_request_interval(db):
    platform, _ = platform_and_rule(db)
    platform.request_interval = Decimal("1")
    db.commit()
    async with platform_operation(platform) as gate:
        await gate()
        first = time.monotonic()
        await gate()
        assert time.monotonic() - first >= 0.90
    # A second preview/account-check operation must respect the first one's last request.
    async with platform_operation(platform) as next_gate:
        started = time.monotonic()
        await next_gate()
        assert time.monotonic() - started >= 0.90


def test_session_and_preview_api_return_busy_while_worker_holds_lock(db, admin_client):
    platform, rule = platform_and_rule(db)
    db.commit()
    client = tasks.redis_client()
    lock_key = f"pm:lock:{platform.id}"
    client.set(lock_key, "test-worker-owner", ex=30)
    try:
        connect = admin_client.put(f"/api/platforms/{platform.id}/session", json={"token": "no-upstream-test-token"})
        preview = admin_client.post(f"/api/rules/{rule.id}/test", json={})
        assert connect.status_code == 409
        assert preview.status_code == 409
        assert client.get(lock_key) == "test-worker-owner"
    finally:
        client.delete(lock_key)


def test_retention_keeps_boundary_day_complete_across_two_consecutive_days(db, monkeypatch):
    fixed_now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    midnight = fixed_now.replace(hour=0, minute=0, second=0, microsecond=0)
    boundary = midnight-timedelta(days=config.DETAIL_RETENTION_DAYS)
    platform, rule = platform_and_rule(db)
    for index, (hour, price) in enumerate([(1, "1"), (18, "9")]):
        persist_page(db, run_for(db, platform, rule, offset=index), [source(price=price)],
                     observed_at=boundary+timedelta(hours=hour))
    db.commit()
    monkeypatch.setattr(tasks, "utcnow", lambda: fixed_now)
    tasks.retain_history.run()
    db.expire_all()
    day = boundary.date().isoformat()
    first = db.scalar(select(DailyAggregate).where(DailyAggregate.day == day))
    assert (first.minimum, first.maximum, first.average, first.count) == (Decimal("1"), Decimal("9"), Decimal("5"), 2)
    # Midday execution must retain the whole boundary date, including its 01:00 quote.
    assert db.scalar(select(func.count()).select_from(Observation)) == 2
    monkeypatch.setattr(tasks, "utcnow", lambda: fixed_now+timedelta(days=1))
    tasks.retain_history.run()
    db.expire_all()
    frozen = db.scalar(select(DailyAggregate).where(DailyAggregate.day == day))
    assert (frozen.minimum, frozen.maximum, frozen.average, frozen.count) == (Decimal("1"), Decimal("9"), Decimal("5"), 2)
    assert db.scalar(select(func.count()).select_from(Observation)) == 0


@pytest.mark.parametrize("day_offset", [0, -1])
def test_retention_never_overwrites_frozen_summary_from_partial_boundary_remnants(db, monkeypatch, day_offset):
    fixed_now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    midnight = fixed_now.replace(hour=0, minute=0, second=0, microsecond=0)
    boundary = midnight-timedelta(days=config.DETAIL_RETENTION_DAYS)+timedelta(days=day_offset)
    platform, rule = platform_and_rule(db)
    run = run_for(db, platform, rule)
    # Reproduce a prior rolling-hour purge: only the later quote is still present.
    persist_page(db, run, [source(price="9")], observed_at=boundary+timedelta(hours=18))
    product = db.scalar(select(Product))
    db.add(DailyAggregate(product_id=product.id, day=boundary.date().isoformat(), unit="人",
                          currency="CNY", display_quantity=Decimal("1000"),
                          minimum=Decimal("1"), maximum=Decimal("9"), average=Decimal("5"), count=2))
    db.commit()
    monkeypatch.setattr(tasks, "utcnow", lambda: fixed_now)
    tasks.retain_history.run()
    db.expire_all()
    summary = db.scalar(select(DailyAggregate))
    assert (summary.minimum, summary.maximum, summary.average, summary.count) == (Decimal("1"), Decimal("9"), Decimal("5"), 2)


def test_account_label_cannot_change_after_price_history_exists(db, admin_client):
    platform, rule = platform_and_rule(db)
    persist_page(db, run_for(db, platform, rule), [source()])
    db.commit()
    response = admin_client.put(f"/api/platforms/{platform.id}", json={
        "name": platform.name, "base_url": platform.base_url, "account_label": "different-account",
    })
    assert response.status_code == 409
    db.expire_all()
    assert db.get(Platform, platform.id).account_label == "test-account"
    assert db.scalar(select(Product)).account_label == "test-account"
    assert db.scalar(select(func.count()).select_from(Observation)) == 1


def test_connect_session_without_account_identity_returns_400_and_preserves_credentials(db, admin_client, monkeypatch):
    from app import main as api

    class IdentityMissingCollector:
        account_identity = None

        def __init__(self, base_url, token, rule, request_gate=None, sleep=None):
            pass

        async def check_session(self):
            return True

        async def aclose(self):
            pass

    platform, _ = platform_and_rule(db)
    platform.auth_status = "missing"
    db.commit()
    original_ciphertext = platform.encrypted_session
    monkeypatch.setattr(api, "Xiaohong66Collector", IdentityMissingCollector)
    response = admin_client.put(f"/api/platforms/{platform.id}/session", json={
        "token": json.dumps({"cookies": {"PHPSESSID": "local-unidentified-session"}}),
    })
    assert response.status_code == 400
    db.expire_all()
    current = db.get(Platform, platform.id)
    assert current.auth_status == "missing"
    assert current.account_fingerprint is None
    assert current.encrypted_session == original_ciphertext


def test_connecting_different_account_returns_409_and_preserves_original_encrypted_session(db, admin_client, monkeypatch):
    from app import main as api
    from app.security import decrypt_session

    identity = {"value": "source-account-1001"}

    class IdentifiedCollector:
        def __init__(self, base_url, token, rule, request_gate=None, sleep=None):
            self.account_identity = identity["value"]

        async def check_session(self):
            return True

        async def aclose(self):
            pass

    platform, _ = platform_and_rule(db)
    db.commit()
    monkeypatch.setattr(api, "Xiaohong66Collector", IdentifiedCollector)
    original_token = json.dumps({"cookies": {"PHPSESSID": "local-account-one-session"}})
    first = admin_client.put(f"/api/platforms/{platform.id}/session", json={"token": original_token})
    assert first.status_code == 200
    db.expire_all()
    accepted = db.get(Platform, platform.id)
    original_ciphertext, original_fingerprint = accepted.encrypted_session, accepted.account_fingerprint
    assert original_fingerprint
    assert decrypt_session(original_ciphertext) == original_token
    identity["value"] = "source-account-2002"
    second = admin_client.put(f"/api/platforms/{platform.id}/session", json={
        "token": json.dumps({"cookies": {"PHPSESSID": "local-account-two-session"}}),
    })
    assert second.status_code == 409
    db.expire_all()
    preserved = db.get(Platform, platform.id)
    assert preserved.encrypted_session == original_ciphertext
    assert preserved.account_fingerprint == original_fingerprint
    assert preserved.auth_status == "connected"
    assert decrypt_session(preserved.encrypted_session) == original_token


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["constructor", "decrypt", "lease_start"])
async def test_initialization_failure_terminates_batch_and_releases_owned_lease(db, monkeypatch, failure_phase):
    platform, rule = platform_and_rule(db)
    run = run_for(db, platform, rule, status="queued")
    if failure_phase == "decrypt":
        platform.encrypted_session = "invalid-ciphertext-sensitive-value"
    db.commit()

    class BrokenCollector:
        def __init__(self, *args, **kwargs):
            raise ValueError("secret-password-and-cookie-must-not-appear")

    if failure_phase == "lease_start":
        class BrokenLease(tasks.PlatformLease):
            async def start(self):
                raise RuntimeError("secret-password-and-cookie-must-not-appear")
        monkeypatch.setattr(tasks, "PlatformLease", BrokenLease)
    else:
        monkeypatch.setattr(tasks, "Xiaohong66Collector", BrokenCollector)
    result = await tasks.execute_run(run.id)
    db.expire_all()
    ended = db.get(CollectionRun, run.id)
    assert result["status"] == ended.status == "failed"
    assert ended.completed_at is not None
    assert ended.error_count == 1
    assert "secret-password" not in ended.error
    assert "invalid-ciphertext" not in ended.error
    assert tasks.redis_client().get(f"pm:lock:{platform.id}") is None
    assert db.scalar(select(func.count()).select_from(ErrorCapture)) == 1
    assert list(db.scalars(select(Event.kind))) == ["collection_failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("page_written", [False, True])
async def test_collector_close_failure_is_recorded_and_cannot_leak_lock(db, monkeypatch, page_written):
    platform, rule = platform_and_rule(db)
    run = run_for(db, platform, rule, status="queued")
    db.commit()

    class BrokenCloseCollector:
        expected_total = 1 if page_written else 0

        def __init__(self, *args, **kwargs):
            pass

        async def iter_pages(self):
            if page_written:
                yield [source()]

        async def aclose(self):
            raise RuntimeError("cleanup-cookie-must-not-appear")

    monkeypatch.setattr(tasks, "Xiaohong66Collector", BrokenCloseCollector)
    result = await tasks.execute_run(run.id)
    db.expire_all()
    ended = db.get(CollectionRun, run.id)
    assert result["status"] == ("partial" if page_written else "failed")
    assert ended.products_seen == int(page_written)
    assert ended.error_count == 1
    assert "cleanup-cookie" not in ended.error
    assert db.get(Platform, platform.id).last_success_at is None
    assert tasks.redis_client().get(f"pm:lock:{platform.id}") is None


@pytest.mark.asyncio
async def test_failed_primary_rollback_still_finalizes_with_fresh_session_and_releases_lock(db, monkeypatch):
    platform, rule = platform_and_rule(db)
    run = run_for(db, platform, rule, status="queued")
    db.commit()
    original_factory = tasks.SessionLocal
    allocated = []

    def session_factory(*args, **kwargs):
        session = original_factory(*args, **kwargs)
        if not allocated:
            def broken_rollback():
                raise RuntimeError("rollback unavailable")
            session.rollback = broken_rollback
        allocated.append(session)
        return session

    class BrokenCollector:
        def __init__(self, *args, **kwargs):
            raise ValueError("constructor failed")

    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(tasks, "Xiaohong66Collector", BrokenCollector)
    result = await tasks.execute_run(run.id)
    db.expire_all()
    assert result["status"] == "failed"
    assert db.get(CollectionRun, run.id).completed_at is not None
    assert tasks.redis_client().get(f"pm:lock:{platform.id}") is None


def test_exhausted_worker_recovery_records_failure_count_and_event(db, monkeypatch):
    platform, rule = platform_and_rule(db)
    platform.next_due_at = utcnow()+timedelta(days=1)
    run = run_for(db, platform, rule, status="running")
    run.started_at = utcnow()-timedelta(minutes=10)
    run.heartbeat_at = utcnow()-timedelta(minutes=5)
    run.attempts = 3
    db.commit()
    dispatched = []
    monkeypatch.setattr(tasks, "dispatch_one", dispatched.append)
    tasks.dispatch_due.run()
    tasks.dispatch_due.run()
    db.expire_all()
    failed = db.get(CollectionRun, run.id)
    assert failed.status == "failed"
    assert failed.error_count == 1
    assert failed.completed_at is not None
    assert db.get(Platform, platform.id).last_error == failed.error
    assert list(db.scalars(select(Event.kind))) == ["collection_failed"]
    assert dispatched == []


def test_zero_display_quantity_without_unit_keeps_hourly_and_daily_prices_but_is_not_comparable(db, admin_client, monkeypatch):
    fixed_now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    yesterday = fixed_now.replace(hour=0, minute=0, second=0, microsecond=0)-timedelta(days=1)
    platform, rule = platform_and_rule(db)
    for index, (hour, price) in enumerate([(1, "0.007447"), (18, "0.007448")]):
        run = run_for(db, platform, rule, offset=-48+index)
        item = source(price=price, quantity="0", unit="")
        item.quality = ["unit_requires_review"]
        persist_page(db, run, [item], observed_at=yesterday+timedelta(hours=hour))
    db.commit()
    product = db.scalar(select(Product))
    response = admin_client.get(f"/api/products/{product.id}/history?days=7")
    assert response.status_code == 200
    actual = [point for point in response.json()["items"] if point["status"] == "active"]
    assert [Decimal(point["price"]) for point in actual] == [Decimal("0.007447"), Decimal("0.007448")]
    assert [Decimal(point["display_price"]) for point in actual] == [Decimal("0.007447"), Decimal("0.007448")]
    monkeypatch.setattr(tasks, "utcnow", lambda: fixed_now)
    tasks.retain_history.run()
    db.expire_all()
    aggregate = db.scalar(select(DailyAggregate))
    assert aggregate.display_quantity == 0
    assert aggregate.unit == ""
    assert aggregate.minimum == Decimal("0.007447")
    assert aggregate.maximum == Decimal("0.007448")
    assert aggregate.average == Decimal("0.0074475")
    assert aggregate.count == 2
    mapping = admin_client.post("/api/mappings", json={
        "name": "无声明单位不可比", "unit": "", "currency": "CNY", "product_ids": [product.id],
    })
    assert mapping.status_code == 200
    comparison = admin_client.get(f"/api/mappings/{mapping.json()['id']}/compare")
    assert comparison.status_code == 200
    assert comparison.json()["comparable"] is False


def test_manual_collection_advances_due_time_and_scheduler_does_not_create_second_immediate_run(db, admin_client, monkeypatch):
    from app import main as api

    platform, _ = platform_and_rule(db)
    platform.next_due_at = utcnow()-timedelta(minutes=10)
    db.commit()
    dispatched = []

    def fake_dispatch(run_id):
        dispatched.append(run_id)
        with SessionLocal() as dispatch_db:
            dispatched_run = dispatch_db.get(CollectionRun, run_id)
            dispatched_run.dispatched_at = utcnow()
            dispatch_db.commit()

    monkeypatch.setattr(api, "dispatch_one", fake_dispatch)
    monkeypatch.setattr(tasks, "dispatch_one", fake_dispatch)
    response = admin_client.post(f"/api/platforms/{platform.id}/collect")
    assert response.status_code == 200
    run_id = response.json()["id"]
    db.expire_all()
    run = db.get(CollectionRun, run_id)
    assert db.get(Platform, platform.id).next_due_at >= run.scheduled_at+timedelta(seconds=platform.interval_seconds)
    result = tasks.dispatch_due.run()
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(CollectionRun)) == 1
    assert dispatched == [run_id]
    assert result["dispatched"] == 0
