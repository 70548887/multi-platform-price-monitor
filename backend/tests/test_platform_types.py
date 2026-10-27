"""Platform identity and adapter isolation on the existing local test resources.

The pipeline fixtures keep this suite on price_monitor_test / Redis DB 15.
All source HTTP is forbidden; run together with test_pipeline without xdist.
"""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

# Import before app modules so their database/Redis configuration is isolated.
from test_pipeline import (  # noqa: F401
    admin_client,
    clean_test_resources,
    db,
    isolated_database,
    persist_page,
    run_for,
    source,
)

from app import main as api, tasks
from app.models import CollectionRun, Platform, Rule, utcnow
from app.security import encrypt_session


@pytest.fixture(autouse=True)
def forbid_source_http(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Platform type tests must never send source HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


def platform_payload(**overrides):
    body = {"name": "平台类型测试", "base_url": "https://source.example"}
    body.update(overrides)
    return body


def create_platform(client, **overrides):
    response = client.post("/api/platforms", json=platform_payload(**overrides))
    assert response.status_code == 200, response.text
    return response.json()


def edit_payload(platform, **overrides):
    fields = ("name", "base_url", "platform_type", "adapter", "account_label",
              "enabled", "interval_seconds", "request_interval")
    body = {key: platform[key] for key in fields}
    body.update(overrides)
    return body


def test_type_directory_identifies_supported_versions_and_pending_standard(admin_client):
    response = admin_client.get("/api/platform-types")
    assert response.status_code == 200
    types = {item["id"]: item for item in response.json()["items"]}
    assert set(types) == {"yile", "yike"}
    assert types["yile"]["name"] == "亿乐"
    assert types["yike"]["name"] == "易客"
    yile = {item["id"]: item for item in types["yile"]["adapters"]}
    yike = {item["id"]: item for item in types["yike"]["adapters"]}
    assert yile["xiaohong66"]["implemented"] is True
    assert yike["fans8"]["implemented"] is True
    assert yike["yike"]["implemented"] is False


@pytest.mark.parametrize("identity,expected", [
    ({}, ("yile", "xiaohong66")),
    ({"adapter": "xiaohong66"}, ("yile", "xiaohong66")),
    ({"platform_type": "yile"}, ("yile", "xiaohong66")),
    ({"platform_type": "yike"}, ("yike", "fans8")),
    ({"adapter": "fans8"}, ("yike", "fans8")),
    ({"adapter": "yike"}, ("yike", "yike")),
])
def test_legacy_and_partial_identity_infer_a_consistent_type(admin_client, identity, expected):
    platform = create_platform(admin_client, **identity)
    assert (platform["platform_type"], platform["adapter"]) == expected
    listed = admin_client.get("/api/platforms").json()["items"][0]
    assert (listed["platform_type"], listed["adapter"]) == expected


@pytest.mark.parametrize("identity", [
    {"platform_type": "unknown"},
    {"adapter": "unknown"},
    {"platform_type": "yile", "adapter": "fans8"},
    {"platform_type": "yile", "adapter": "yike"},
    {"platform_type": "yike", "adapter": "xiaohong66"},
])
def test_unknown_or_mismatched_identity_returns_422_without_rows(db, admin_client, identity):
    response = admin_client.post("/api/platforms", json=platform_payload(**identity))
    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(Platform)) == 0
    assert db.scalar(select(func.count()).select_from(Rule)) == 0


@pytest.mark.parametrize("identity", [
    {"platform_type": "unknown", "adapter": "xiaohong66"},
    {"platform_type": "yile", "adapter": "unknown"},
    {"platform_type": "yile", "adapter": "fans8"},
])
def test_invalid_edit_preserves_existing_identity(db, admin_client, identity):
    platform = create_platform(admin_client)
    response = admin_client.put(f"/api/platforms/{platform['id']}", json=edit_payload(platform, **identity))
    assert response.status_code == 422
    db.expire_all()
    saved = db.get(Platform, platform["id"])
    assert (saved.platform_type, saved.adapter) == ("yile", "xiaohong66")


def test_unused_connection_can_change_type_and_resets_its_draft(db, admin_client):
    platform = create_platform(admin_client)
    draft = db.scalar(select(Rule).where(Rule.platform_id == platform["id"]))
    draft.config = {**draft.config, "page_size": 17}
    draft.test_ok, draft.tested_at = True, utcnow()
    saved = db.get(Platform, platform["id"])
    saved.last_error = "old adapter failure"
    db.commit()

    # This test changes the parser on the same synthetic source. A tested
    # rule now binds its origin, so moving it to another site is a separate
    # rejected operation rather than part of adapter-reset coverage.
    response = admin_client.put(f"/api/platforms/{platform['id']}", json=edit_payload(
        platform, platform_type="yike", adapter="fans8"))
    assert response.status_code == 200, response.text
    assert response.json()["platform_type"] == "yike"
    assert response.json()["adapter"] == "fans8"
    db.expire_all()
    saved = db.get(Platform, platform["id"])
    assert saved.encrypted_session is None
    assert saved.account_fingerprint is None
    assert saved.auth_status == "missing"
    assert saved.active_rule_id is None
    assert saved.last_error is None
    default = admin_client.get(f"/api/platforms/{platform['id']}/default-rule")
    assert default.status_code == 200
    drafts = list(db.scalars(select(Rule).where(Rule.platform_id == platform["id"], Rule.status == "draft")))
    assert drafts
    for rule in drafts:
        assert rule.config == default.json()
        assert rule.test_ok is False
        assert rule.tested_at is None


def test_unused_connection_can_correct_source_after_unvalidated_adapter_reset(db, admin_client):
    platform = create_platform(admin_client)
    switched = admin_client.put(f"/api/platforms/{platform['id']}", json=edit_payload(
        platform, platform_type="yike", adapter="fans8", base_url="https://fans8.top"))
    assert switched.status_code == 200, switched.text
    db.expire_all()
    archived = db.scalar(select(Rule).where(Rule.platform_id == platform["id"], Rule.status == "archived"))
    assert archived is not None
    assert archived.test_ok is False
    assert archived.tested_at is None

    corrected = admin_client.put(f"/api/platforms/{platform['id']}", json=edit_payload(
        switched.json(), base_url="https://corrected.example"))
    assert corrected.status_code == 200, corrected.text
    db.expire_all()
    saved = db.get(Platform, platform["id"])
    assert (saved.platform_type, saved.adapter, saved.base_url) == (
        "yike", "fans8", "https://corrected.example")
    assert saved.auth_status == "missing"
    assert saved.encrypted_session is None
    assert saved.active_rule_id is None
    assert db.scalar(select(func.count()).select_from(CollectionRun)) == 0
    assert db.scalar(select(func.count()).select_from(Rule)) == 2


@pytest.mark.parametrize("lock_reason", ["session", "active_rule", "product_history"])
@pytest.mark.parametrize("target", [("yike", "fans8"), ("yike", "yike")])
def test_existing_identity_cannot_change_after_use(db, admin_client, lock_reason, target):
    platform = create_platform(admin_client)
    saved = db.get(Platform, platform["id"])
    rule = db.scalar(select(Rule).where(Rule.platform_id == saved.id))
    if lock_reason == "session":
        # A stored encrypted session locks identity even with stale auth_status.
        saved.encrypted_session = encrypt_session("unit-session-never-sent")
    elif lock_reason == "active_rule":
        rule.status = "published"
        saved.active_rule_id = rule.id
    else:
        persist_page(db, run_for(db, saved, rule), [source()])
    db.commit()
    response = admin_client.put(f"/api/platforms/{saved.id}", json=edit_payload(
        platform, platform_type=target[0], adapter=target[1]))
    assert response.status_code == 409
    db.expire_all()
    assert (saved.platform_type, saved.adapter) == ("yile", "xiaohong66")


def test_adapter_change_within_same_type_is_also_locked(db, admin_client):
    platform = create_platform(admin_client, platform_type="yike", adapter="fans8", base_url="https://fans8.top")
    saved = db.get(Platform, platform["id"])
    saved.encrypted_session = encrypt_session("unit-session-never-sent")
    db.commit()
    response = admin_client.put(f"/api/platforms/{saved.id}", json=edit_payload(platform, adapter="yike"))
    assert response.status_code == 409
    db.expire_all()
    assert saved.adapter == "fans8"


def test_publish_refreshes_a_rule_invalidated_while_waiting_for_platform_lock(db, admin_client):
    platform = create_platform(admin_client)
    rule = db.scalar(select(Rule).where(Rule.platform_id == platform["id"]))
    # Empty options are valid for both schemas, so schema validation alone
    # cannot detect that this successful test belonged to the previous adapter.
    rule.config, rule.test_ok, rule.tested_at = {}, True, utcnow()
    db.commit()
    assert rule.test_ok is True  # Keep the pre-edit rule in this session's cache.

    edited = admin_client.put(f"/api/platforms/{platform['id']}", json=edit_payload(
        platform, platform_type="yike", adapter="fans8"))
    assert edited.status_code == 200, edited.text
    with pytest.raises(HTTPException) as caught:
        api.publish_rule(rule.id, db, None)
    assert caught.value.status_code == 400
    db.rollback()
    db.expire_all()
    assert db.get(Platform, platform["id"]).active_rule_id is None
    assert db.get(Rule, rule.id).test_ok is False


def test_tested_draft_cannot_move_source_during_adapter_change(db, admin_client):
    platform = create_platform(admin_client)
    rule = db.scalar(select(Rule).where(Rule.platform_id == platform["id"]))
    rule.test_ok, rule.tested_at = True, utcnow()
    db.commit()

    response = admin_client.put(f"/api/platforms/{platform['id']}", json=edit_payload(
        platform, platform_type="yike", adapter="fans8", base_url="https://fans8.top"))
    assert response.status_code == 409
    db.expire_all()
    saved = db.get(Platform, platform["id"])
    assert (saved.platform_type, saved.adapter, saved.base_url) == (
        "yile", "xiaohong66", platform["base_url"])
    assert db.get(Rule, rule.id).status == "draft"
    assert db.get(Rule, rule.id).test_ok is True
    assert db.scalar(select(func.count()).select_from(Rule)) == 1


def test_pending_adapter_can_be_saved_but_source_actions_stop_before_operation(db, admin_client, monkeypatch):
    platform = create_platform(admin_client, platform_type="yike", adapter="yike")
    assert platform["adapter_supported"] is False
    saved = db.get(Platform, platform["id"])
    saved.encrypted_session = encrypt_session("unit-session-never-sent")
    saved.auth_status = "connected"
    rule = db.scalar(select(Rule).where(Rule.platform_id == saved.id))
    saved.active_rule_id = rule.id
    rule.status, rule.test_ok = "published", True
    db.commit()

    def forbidden_operation(*args, **kwargs):
        pytest.fail("An unimplemented adapter must stop before opening a source operation")

    monkeypatch.setattr(api, "platform_operation", forbidden_operation)
    monkeypatch.setattr(api, "dispatch_one", lambda _: pytest.fail("Unsupported adapter was dispatched"))
    responses = [
        admin_client.put(f"/api/platforms/{saved.id}/session", json={"token": "unit-session-never-sent"}),
        admin_client.post(f"/api/platforms/{saved.id}/collect"),
        admin_client.post(f"/api/rules/{rule.id}/test", json={}),
        admin_client.post(f"/api/rules/{rule.id}/publish"),
    ]
    assert all(400 <= response.status_code < 500 for response in responses)
    assert db.scalar(select(func.count()).select_from(CollectionRun)) == 0


def test_scheduler_does_not_queue_or_dispatch_pending_adapter(db, admin_client, monkeypatch):
    platform = create_platform(admin_client, platform_type="yike", adapter="yike")
    saved = db.get(Platform, platform["id"])
    rule = db.scalar(select(Rule).where(Rule.platform_id == saved.id))
    saved.auth_status = "connected"
    # Simulate a preexisting unsupported row, bypassing the API's forced disable.
    saved.enabled = True
    saved.encrypted_session = encrypt_session("unit-session-never-sent")
    saved.active_rule_id = rule.id
    saved.next_due_at = utcnow() - timedelta(hours=1)
    db.commit()
    dispatched = []
    monkeypatch.setattr(tasks, "dispatch_one", dispatched.append)
    tasks.dispatch_due.run()
    db.expire_all()
    assert dispatched == []
    assert db.scalar(select(func.count()).select_from(CollectionRun)) == 0

    # A persisted queue entry from a previous configuration is guarded as well.
    queued = run_for(db, saved, rule, status="queued")
    db.commit()
    tasks.dispatch_due.run()
    db.expire_all()
    assert dispatched == []
    assert db.get(CollectionRun, queued.id).dispatched_at is None


@pytest.mark.asyncio
async def test_worker_rejects_pending_adapter_before_constructing_source(db, admin_client, monkeypatch):
    platform = create_platform(admin_client, platform_type="yike", adapter="yike")
    saved = db.get(Platform, platform["id"])
    rule = db.scalar(select(Rule).where(Rule.platform_id == saved.id))
    saved.auth_status = "connected"
    saved.encrypted_session = encrypt_session("unit-session-never-sent")
    saved.active_rule_id = rule.id
    queued = run_for(db, saved, rule, status="queued")
    db.commit()

    def forbidden_collector(*args, **kwargs):
        pytest.fail("An unimplemented adapter must never construct a source collector")

    monkeypatch.setattr(tasks, "Xiaohong66Collector", forbidden_collector)
    if hasattr(tasks, "Fans8Collector"):
        monkeypatch.setattr(tasks, "Fans8Collector", forbidden_collector)
    result = await tasks.execute_run(queued.id)
    db.expire_all()
    assert result["status"] == "failed"
    assert db.get(CollectionRun, queued.id).products_seen == 0
    assert saved.auth_status == "connected"


def test_platform_rule_schemas_do_not_cross_between_versions(admin_client):
    yile = create_platform(admin_client)
    fans8 = create_platform(admin_client, platform_type="yike", adapter="fans8", base_url="https://fans8.top")
    defaults = {}
    for platform in (yile, fans8):
        response = admin_client.get(f"/api/platforms/{platform['id']}/default-rule")
        assert response.status_code == 200
        defaults[platform["id"]] = response.json()
        valid = admin_client.post(f"/api/platforms/{platform['id']}/rules", json={"config": response.json()})
        assert valid.status_code == 200, valid.text
    assert defaults[yile["id"]] != defaults[fans8["id"]]
    for target, other in ((yile, fans8), (fans8, yile)):
        response = admin_client.post(f"/api/platforms/{target['id']}/rules", json={"config": defaults[other["id"]]})
        assert response.status_code == 422
