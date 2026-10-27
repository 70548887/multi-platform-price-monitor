"""Platform visibility is enforced by the API, using only isolated test resources.

Importing the shared fixtures pins PostgreSQL to price_monitor_test and Redis to
DB 15 before application imports. No collector or upstream HTTP is executed.
"""
from contextlib import contextmanager
from datetime import timedelta
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from test_pipeline import (isolated_database, clean_test_resources, db, admin_client,
                           platform_and_rule, run_for, source)
from app.main import app
from app.models import AuditLog, Event, Product, utcnow
from app.services import persist_page


PASSWORD = 'isolated-platform-viewer-password'


def create_viewer(admin, platform_ids=None, *, username='scoped-viewer'):
    body = {'username': username, 'password': PASSWORD, 'role': 'viewer'}
    if platform_ids is not None:
        body['platform_ids'] = platform_ids
    response = admin.post('/api/users', json=body)
    assert response.status_code == 200, response.text
    assert PASSWORD not in response.text
    return response.json()


@contextmanager
def logged_in(username):
    with TestClient(app) as client:
        response = client.post('/api/auth/login', json={'username': username, 'password': PASSWORD})
        assert response.status_code == 200, response.text
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client, response.json()


@pytest.fixture
def catalogue(db, admin_client):
    now = utcnow()
    a, ar = platform_and_rule(db, name='allowed-platform-A')
    b, br = platform_and_rule(db, name='private-platform-B')
    a.last_success_at = now - timedelta(minutes=4)
    b.last_success_at = now - timedelta(minutes=1)
    b.auth_status, b.last_error = 'expired', 'private-platform-B-error'
    ra, rb = run_for(db, a, ar), run_for(db, b, br)
    for run, count in ((ra, 1), (rb, 2)):
        run.started_at, run.completed_at = now - timedelta(minutes=2), now - timedelta(minutes=1)
        run.products_seen = run.expected_total = count
    allowed = source(identifier='A-1', name='allowed-product-A', price='0.2')
    hidden = [source(identifier='B-1', name='private-product-B', price='0.01'),
              source(identifier='B-2', name='private-closed-product-B', price='0.02', status='closed')]
    allowed.category_id, allowed.category_name = 'A-category', 'allowed-category-A'
    for item in hidden:
        item.category_id, item.category_name = 'B-category', 'private-category-B'
    persist_page(db, ra, [allowed], observed_at=now)
    persist_page(db, rb, hidden, observed_at=now)
    products = {p.source_id: p for p in db.scalars(select(Product))}
    for platform, run, product, title in ((a, ra, products['A-1'], 'allowed-event-A'),
                                         (b, rb, products['B-1'], 'private-event-B')):
        db.add(Event(platform_id=platform.id, product_id=product.id, run_id=run.id,
                     kind='price_changed', title=title, message=title, dedupe_key=title))
    db.add(Event(kind='system', title='private-global-event', message='private-global-event',
                 dedupe_key='private-global-event'))
    db.commit()
    mixed = admin_client.post('/api/mappings', json={'name': 'shared-comparison', 'unit': '人',
        'currency': 'CNY', 'product_ids': [products['A-1'].id, products['B-1'].id]})
    assert mixed.status_code == 200, mixed.text
    private = admin_client.post('/api/mappings', json={'name': 'private-mapping-B', 'unit': '人',
        'currency': 'CNY', 'product_ids': [products['B-2'].id]})
    assert private.status_code == 200, private.text
    return {'a': a.id, 'b': b.id, 'a_product': products['A-1'].id,
            'b_product': products['B-1'].id, 'a_run': ra.id, 'b_run': rb.id,
            'mixed': mixed.json()['id'], 'private': private.json()['id'],
            'a_last_success_at': a.last_success_at.isoformat()}


def test_new_viewer_has_no_implicit_platform_access(admin_client, catalogue):
    user = create_viewer(admin_client)
    assert user['platform_ids'] == [] and user['all_platforms'] is False
    with logged_in(user['username']) as (client, login):
        assert login['platform_ids'] == [] and login['all_platforms'] is False
        for endpoint in ('platforms', 'products', 'runs', 'events', 'mappings'):
            response = client.get('/api/' + endpoint)
            assert response.status_code == 200, response.text
            assert response.json()['items'] == []
            assert response.json()['total'] == 0
        dashboard = client.get('/api/dashboard').json()
        for key in ('platform_count', 'product_count', 'active_count', 'closed_count', 'auth_expired_count'):
            assert dashboard[key] == 0
        for key in ('recent_runs', 'recent_events', 'daily_counts'):
            assert dashboard[key] == []
        assert dashboard['last_collection_at'] is None
        assert client.get(f"/api/products/{catalogue['a_product']}").status_code == 404
        categories = client.get('/api/product-categories')
        assert categories.status_code == 200 and categories.json()['items'] == []


def test_authorized_platform_limits_lists_search_totals_and_public_metadata(admin_client, catalogue):
    user = create_viewer(admin_client, [catalogue['a']])
    with logged_in(user['username']) as (client, login):
        assert login['platform_ids'] == [catalogue['a']]
        platforms = client.get('/api/platforms').json()
        assert platforms['total'] == 1
        assert [p['id'] for p in platforms['items']] == [catalogue['a']]
        for forbidden in ('encrypted_session', 'account_label', 'account_fingerprint', 'proxy_route',
                          'last_error', 'active_rule_id', 'token_storage_key', 'base_url'):
            assert forbidden not in platforms['items'][0]
        products = client.get('/api/products?page_size=1').json()
        assert products['total'] == 1
        assert [p['id'] for p in products['items']] == [catalogue['a_product']]
        assert products['items'][0]['price'] == '0.2'
        assert client.get('/api/products?q=private').json()['total'] == 0
        response = client.get(f"/api/products?platform_id={catalogue['b']}")
        assert response.status_code in (200, 404)
        if response.status_code == 200:
            assert response.json()['items'] == [] and response.json()['total'] == 0
        for endpoint in ('/api/platforms', '/api/products', '/api/runs', '/api/events'):
            text = client.get(endpoint).text
            assert 'private-platform-B' not in text
            assert 'private-product-B' not in text
            assert 'private-event-B' not in text


def test_product_guessing_and_history_cannot_bypass_platform_scope(admin_client, catalogue):
    user = create_viewer(admin_client, [catalogue['a']])
    with logged_in(user['username']) as (client, _):
        assert client.get(f"/api/products/{catalogue['a_product']}").status_code == 200
        history = client.get(f"/api/products/{catalogue['a_product']}/history?days=7")
        assert history.status_code == 200
        assert [point['price'] for point in history.json()['items']] == ['0.2']
        assert len(history.json()['events']) == 1
        for suffix in ('', '/history?days=7'):
            forbidden = client.get(f"/api/products/{catalogue['b_product']}{suffix}")
            assert forbidden.status_code == 404
            assert 'private' not in forbidden.text


def test_category_directory_and_counts_only_include_authorized_platforms(admin_client, catalogue):
    user = create_viewer(admin_client, [catalogue['a']])
    with logged_in(user['username']) as (client, _):
        directory = client.get('/api/product-categories')
        assert directory.status_code == 200
        assert directory.json()['items'] == [{'platform_id': catalogue['a'], 'category_id': 'A-category',
            'category_name': 'allowed-category-A', 'product_count': 1}]
        filtered = client.get(f"/api/product-categories?platform_id={catalogue['a']}")
        assert filtered.status_code == 200 and filtered.json()['items'] == directory.json()['items']
        hidden = client.get(f"/api/product-categories?platform_id={catalogue['b']}")
        assert hidden.status_code in (200, 404)
        if hidden.status_code == 200:
            assert hidden.json()['items'] == []
        assert 'private-category-B' not in directory.text


def test_dashboard_events_and_collection_runs_use_the_same_scope(admin_client, catalogue):
    user = create_viewer(admin_client, [catalogue['a']])
    with logged_in(user['username']) as (client, _):
        dashboard = client.get('/api/dashboard').json()
        assert dashboard['platform_count'] == dashboard['product_count'] == dashboard['active_count'] == 1
        assert dashboard['closed_count'] == dashboard['auth_expired_count'] == 0
        assert dashboard['last_collection_at'] == catalogue['a_last_success_at']
        assert [r['id'] for r in dashboard['recent_runs']] == [catalogue['a_run']]
        assert [e['title'] for e in dashboard['recent_events']] == ['allowed-event-A']
        assert sum(point['count'] for point in dashboard['daily_counts']) == 1
        runs = client.get('/api/runs').json()
        assert runs['total'] == 1 and runs['items'][0]['platform_id'] == catalogue['a']
        events = client.get('/api/events').json()
        assert events['total'] == 1 and events['items'][0]['title'] == 'allowed-event-A'
        assert client.get('/api/events?kind=system').json()['total'] == 0


def test_mixed_mapping_filters_hidden_products_and_recomputes_comparability(admin_client, catalogue):
    user = create_viewer(admin_client, [catalogue['a']])
    with logged_in(user['username']) as (client, _):
        mappings = client.get('/api/mappings').json()
        assert mappings['total'] == 1
        group = mappings['items'][0]
        assert group['id'] == catalogue['mixed']
        assert group['product_ids'] == [catalogue['a_product']]
        assert [p['id'] for p in group['products']] == [catalogue['a_product']]
        assert 'private-mapping-B' not in json.dumps(mappings)
        comparison = client.get(f"/api/mappings/{catalogue['mixed']}/compare")
        assert comparison.status_code == 200
        assert comparison.json()['comparable'] is False
        assert [p['id'] for p in comparison.json()['items']] == [catalogue['a_product']]
        assert client.get(f"/api/mappings/{catalogue['private']}/compare").status_code == 404


def test_admin_keeps_global_visibility_without_explicit_grants(admin_client, catalogue):
    identity = admin_client.get('/api/auth/me').json()
    assert identity['all_platforms'] is True and identity['platform_ids'] == []
    assert admin_client.get('/api/platforms').json()['total'] == 2
    assert admin_client.get('/api/products').json()['total'] == 3
    assert admin_client.get('/api/events').json()['total'] == 3
    assert admin_client.get('/api/runs').json()['total'] == 2
    assert admin_client.get('/api/mappings').json()['total'] == 2
    dashboard = admin_client.get('/api/dashboard').json()
    assert dashboard['platform_count'] == 2 and dashboard['product_count'] == 3
    assert dashboard['auth_expired_count'] == 1
    assert sum(point['count'] for point in dashboard['daily_counts']) == 3
    comparison = admin_client.get(f"/api/mappings/{catalogue['mixed']}/compare").json()
    assert comparison['comparable'] is True
    assert len(comparison['items']) == 2


def test_grant_revoke_and_regrant_apply_to_existing_login_immediately(admin_client, catalogue):
    user = create_viewer(admin_client, [catalogue['a']])
    with logged_in(user['username']) as (client, _):
        assert client.get('/api/products').json()['total'] == 1
        response = admin_client.patch(f"/api/users/{user['id']}", json={'platform_ids': [catalogue['b']]})
        assert response.status_code == 200
        assert client.get('/api/auth/me').json()['platform_ids'] == [catalogue['b']]
        assert client.get('/api/products').json()['total'] == 2
        assert client.get(f"/api/products/{catalogue['a_product']}").status_code == 404
        assert admin_client.patch(f"/api/users/{user['id']}", json={'platform_ids': []}).status_code == 200
        assert client.get('/api/products').json()['total'] == 0
        assert admin_client.patch(f"/api/users/{user['id']}", json={'platform_ids': [catalogue['a'], catalogue['b']]}).status_code == 200
        assert client.get('/api/products').json()['total'] == 3


def test_omitted_grants_preserve_scope_and_role_transitions_clear_old_grants(admin_client, catalogue):
    user = create_viewer(admin_client, [catalogue['a']])
    path = f"/api/users/{user['id']}"
    unchanged = admin_client.patch(path, json={'role': 'viewer', 'enabled': True})
    assert unchanged.status_code == 200 and unchanged.json()['platform_ids'] == [catalogue['a']]
    elevated = admin_client.patch(path, json={'role': 'admin'})
    assert elevated.status_code == 200
    assert elevated.json()['all_platforms'] is True and elevated.json()['platform_ids'] == []
    demoted = admin_client.patch(path, json={'role': 'viewer'})
    assert demoted.status_code == 200
    assert demoted.json()['all_platforms'] is False and demoted.json()['platform_ids'] == []
    assert admin_client.patch(path, json={'role': 'admin'}).status_code == 200
    assigned = admin_client.patch(path, json={'role': 'viewer', 'platform_ids': [catalogue['b']]})
    assert assigned.status_code == 200 and assigned.json()['platform_ids'] == [catalogue['b']]


@pytest.mark.parametrize('invalid', [[999999], [True], ['1'], [1.0], [-1], [0], None, '1', [1, 1]])
def test_invalid_platform_grants_are_rejected_atomically(admin_client, catalogue, invalid):
    user = create_viewer(admin_client, [catalogue['a']])
    response = admin_client.patch(f"/api/users/{user['id']}", json={'platform_ids': invalid})
    assert response.status_code == 422, response.text
    saved = next(row for row in admin_client.get('/api/users').json()['items'] if row['id'] == user['id'])
    assert saved['platform_ids'] == [catalogue['a']]
    creation = admin_client.post('/api/users', json={'username': 'invalid-grant-member',
        'password': PASSWORD, 'platform_ids': invalid})
    assert creation.status_code == 422
    assert all(row['username'] != 'invalid-grant-member' for row in admin_client.get('/api/users').json()['items'])


def test_admin_role_rejects_misleading_partial_platform_assignment(admin_client, catalogue):
    response = admin_client.post('/api/users', json={'username': 'misleading-admin', 'password': PASSWORD,
        'role': 'admin', 'platform_ids': [catalogue['a']]})
    assert response.status_code == 422


def test_viewer_cannot_expand_own_grants_or_read_administrative_settings(admin_client, catalogue):
    user = create_viewer(admin_client, [catalogue['a']])
    with logged_in(user['username']) as (client, _):
        assert client.patch(f"/api/users/{user['id']}", json={'platform_ids': [catalogue['b']]}).status_code == 403
        assert client.post('/api/users', json={'username': 'forged-admin', 'password': PASSWORD, 'role': 'admin'}).status_code == 403
        for path in ('/api/users', '/api/settings/network', '/api/update/config', '/api/update/status',
                     '/api/update/history', '/api/ops/stats',
                     f"/api/platforms/{catalogue['a']}/rules", f"/api/platforms/{catalogue['a']}/default-rule"):
            assert client.get(path).status_code == 403, path
        assert client.post('/api/update/check', json={}).status_code == 403
        assert client.get('/api/auth/me').json()['platform_ids'] == [catalogue['a']]


def test_assignment_requires_csrf_and_records_nonsecret_administrator_audit(admin_client, catalogue, db):
    user = create_viewer(admin_client, [catalogue['a']])
    before = list(db.scalars(select(AuditLog).order_by(AuditLog.id)))
    csrf = admin_client.headers.pop('X-CSRF-Token')
    blocked = admin_client.patch(f"/api/users/{user['id']}", json={'platform_ids': [catalogue['b']]})
    admin_client.headers['X-CSRF-Token'] = csrf
    assert blocked.status_code == 403
    saved = next(row for row in admin_client.get('/api/users').json()['items'] if row['id'] == user['id'])
    assert saved['platform_ids'] == [catalogue['a']]
    changed = admin_client.patch(f"/api/users/{user['id']}", json={'platform_ids': [catalogue['b']]})
    assert changed.status_code == 200
    db.expire_all()
    after = list(db.scalars(select(AuditLog).where(AuditLog.id > max(row.id for row in before)).order_by(AuditLog.id)))
    assert after
    assert any(row.target == str(user['id']) and row.user_id == admin_client.get('/api/auth/me').json()['id'] for row in after)
    assert PASSWORD not in json.dumps([{'action': row.action, 'target': row.target} for row in after])
