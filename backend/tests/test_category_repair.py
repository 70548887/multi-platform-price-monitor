"""Offline category recovery against price_monitor_test; no source requests."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
from threading import Event as ThreadEvent
import time

import pytest
from sqlalchemy import select, text

from test_pipeline import (isolated_database, clean_test_resources, db,
                           platform_and_rule, run_for, source)
from app import tasks
from app.db import SessionLocal, engine
from app.models import AuditLog, CollectionRun, Observation, Product
from app.services import persist_page


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'repair_product_categories.py'
spec = importlib.util.spec_from_file_location('tested_category_repair', SCRIPT)
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


@pytest.fixture
def recovery_root(monkeypatch, tmp_path):
    monkeypatch.setattr(repair, 'ROOT', tmp_path)
    return tmp_path


def stored_product(db, *, adapter='fans8', label='', raw_label='Recovered / Category'):
    platform, rule = platform_and_rule(db, name='offline-category-fixture')
    platform.adapter = adapter
    platform.platform_type = 'yike' if adapter == 'fans8' else 'yile'
    run = run_for(db, platform, rule)
    item = source()
    item.category_name = label
    item.raw = {**item.raw, 'ClassName1': raw_label}
    persist_page(db, run, [item])
    db.commit()
    product = db.scalar(select(Product).where(Product.platform_id == platform.id))
    return platform, rule, run, product


def test_dry_run_reports_without_writing_metadata_backup_or_audit(db, recovery_root):
    _, _, _, product = stored_product(db)
    result = repair.repair_categories()
    assert result == {'mode': 'dry_run', 'products': 1, 'backup_created': False}
    db.expire_all()
    assert db.get(Product, product.id).category_name == ''
    assert not (recovery_root / '.runtime').exists()
    assert list(db.scalars(select(AuditLog))) == []


def test_apply_uses_last_observation_and_preserves_prices_runs_and_history(db, recovery_root):
    platform, rule, first_run, product = stored_product(db, raw_label='Old category')
    latest = run_for(db, platform, rule, offset=1)
    item = source(price='0.009123')
    item.category_name = ''
    item.raw = {**item.raw, 'ClassName1': ' Fresh parent ', 'ClassName2': '', 'ClassName3': ' Leaf '}
    persist_page(db, latest, [item])
    db.commit()
    observations = [(o.id, o.price, o.raw, o.observed_at) for o in db.scalars(select(Observation).order_by(Observation.id))]
    runs = [(r.id, r.status, r.products_seen, r.completed_at) for r in db.scalars(select(CollectionRun).order_by(CollectionRun.id))]
    before_price = db.get(Product, product.id).price
    result = repair.repair_categories(apply=True)
    assert result == {'mode': 'apply', 'products': 1, 'backup_created': True}
    db.expire_all()
    product = db.get(Product, product.id)
    assert product.category_name == 'Fresh parent / Leaf'
    assert product.price == before_price
    assert product.category_id == '12' and product.last_run_id == latest.id
    assert observations == [(o.id, o.price, o.raw, o.observed_at) for o in db.scalars(select(Observation).order_by(Observation.id))]
    assert runs == [(r.id, r.status, r.products_seen, r.completed_at) for r in db.scalars(select(CollectionRun).order_by(CollectionRun.id))]
    backups = list((recovery_root / '.runtime/category-repair').glob('*.json'))
    assert len(backups) == 1
    payload = json.loads(backups[0].read_text(encoding='utf-8'))
    assert payload['items'] == [{'product_id': product.id, 'last_run_id': latest.id,
        'category_id': '12', 'category_name': '', 'recovered_category_name': 'Fresh parent / Leaf'}]
    assert db.scalar(select(AuditLog).where(AuditLog.action == 'repair_product_categories')).target == 'fans8:1'
    assert repair.repair_categories(apply=True) == {'mode': 'apply', 'products': 0, 'backup_created': False}
    assert len(list((recovery_root / '.runtime/category-repair').glob('*.json'))) == 1


@pytest.mark.parametrize('adapter,label,raw_label', [
    ('fans8', 'Existing category', 'Replacement must not win'),
    ('xiaohong66', '', 'Other adapter must not change'),
    ('fans8', '', ''),
])
def test_existing_category_other_adapters_and_missing_source_labels_are_untouched(db, recovery_root, adapter, label, raw_label):
    _, _, _, product = stored_product(db, adapter=adapter, label=label, raw_label=raw_label)
    assert repair.repair_categories(apply=True)['products'] == 0
    db.expire_all()
    assert db.get(Product, product.id).category_name == label
    assert not (recovery_root / '.runtime').exists()


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_active_fans8_batch_blocks_apply_but_keeps_dry_run_read_only(db, recovery_root, status):
    _, _, run, product = stored_product(db)
    run.status = status
    db.commit()
    assert repair.repair_categories()['products'] == 1
    with pytest.raises(RuntimeError, match='Fans8 collection is active'):
        repair.repair_categories(apply=True)
    db.expire_all()
    assert db.get(Product, product.id).category_name == ''
    assert db.get(CollectionRun, run.id).status == status
    assert not (recovery_root / '.runtime').exists()


def test_apply_holds_the_same_platform_planning_lock_until_metadata_commit(db, recovery_root, monkeypatch):
    platform, _, _, product = stored_product(db)
    repairing, release = ThreadEvent(), ThreadEvent()
    planner_started, planner = ThreadEvent(), {}
    original = repair.source_category_label

    def pause_with_product_locked(raw):
        repairing.set()
        assert release.wait(10), 'Repair lock test timed out'
        return original(raw)

    def plan_without_dispatch():
        with SessionLocal() as session:
            candidate = session.get(type(platform), platform.id)
            planner['pid'] = session.scalar(text('SELECT pg_backend_pid()'))
            planner_started.set()
            run = tasks.queue_run(session, candidate)
            session.commit()
            return run.id

    monkeypatch.setattr(repair, 'source_category_label', pause_with_product_locked)
    with ThreadPoolExecutor(max_workers=2) as executor:
        recovering = executor.submit(repair.repair_categories, apply=True)
        assert repairing.wait(5), 'Repair did not lock its candidate'
        planning = executor.submit(plan_without_dispatch)
        try:
            assert planner_started.wait(5), 'Contending planner did not start'
            waiting = False
            deadline = time.monotonic() + 5
            with engine.connect() as observer:
                while time.monotonic() < deadline and not planning.done():
                    waiting = bool(observer.scalar(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid "
                        "AND locktype='advisory' AND NOT granted)"), {'pid': planner['pid']}))
                    if waiting:
                        break
                    time.sleep(0.01)
            assert waiting, 'A new collection must wait until category metadata is committed'
        finally:
            release.set()
        assert recovering.result(timeout=10)['products'] == 1
        run_id = planning.result(timeout=10)
    db.expire_all()
    assert db.get(Product, product.id).category_name == 'Recovered / Category'
    assert db.get(CollectionRun, run_id).status == 'queued'
