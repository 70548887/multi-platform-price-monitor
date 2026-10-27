"""Whole-market catalogue and quotes, isolated to price_monitor_test and Redis 15.

The API is exercised using fixed source records. No collector/upstream request
or production grant is used by these tests.
"""
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect, select

from test_pipeline import (isolated_database, clean_test_resources, db, admin_client,
                           platform_and_rule, run_for, source)
from app.db import engine
from app.main import app
from app.models import AuditLog, CanonicalProduct, Product, utcnow
from app.services import persist_page


PASSWORD = 'comparison-viewer-fixture-password'


def category(admin, name='互动服务', **changes):
    response = admin.post('/api/comparison-categories', json={
        'name': name, 'description': '人工维护的比价类目', 'sort_order': 10, **changes})
    assert response.status_code == 200, response.text
    return response.json()


def group(admin, name='统一点赞', **changes):
    response = admin.post('/api/mappings', json={
        'name': name, 'unit': '人', 'currency': 'CNY', 'product_ids': [], **changes})
    assert response.status_code == 200, response.text
    return response.json()


def offers(db, specs):
    """Return stored products indexed by source ID, from any number of platforms."""
    platforms = {}
    rows = {}
    for index, spec in enumerate(specs):
        platform_key = spec.get('platform', 'A')
        if platform_key not in platforms:
            platform, rule = platform_and_rule(db, name='fixture-platform-' + platform_key)
            platforms[platform_key] = (platform, rule)
        platform, rule = platforms[platform_key]
        run = run_for(db, platform, rule, offset=index)
        identifier = spec.get('id', str(index + 1))
        item = source(identifier=identifier, name=spec.get('name', '源站点赞 ' + identifier),
                      price=spec.get('price', '0.007486'), unit=spec.get('unit', '人'),
                      quantity=spec.get('quantity', '1000'), status=spec.get('status', 'active'))
        item.raw['currency'] = spec.get('currency', 'CNY')
        item.quality = spec.get('quality', [])
        persist_page(db, run, [item], observed_at=utcnow() - timedelta(seconds=spec.get('age', 0)))
        db.flush()
        rows[identifier] = db.scalar(select(Product).where(
            Product.platform_id == platform.id, Product.source_id == identifier))
    db.commit()
    return rows


@contextmanager
def viewer(admin, platform_ids=()):
    user = admin.post('/api/users', json={'username': 'comparison-viewer', 'password': PASSWORD,
        'role': 'viewer', 'platform_ids': list(platform_ids)})
    assert user.status_code == 200, user.text
    with TestClient(app) as client:
        response = client.post('/api/auth/login', json={'username': 'comparison-viewer', 'password': PASSWORD})
        assert response.status_code == 200, response.text
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


def test_category_crud_order_and_nonempty_delete_guard(admin_client):
    later = category(admin_client, '稍后类别', sort_order=20)
    earlier = category(admin_client, '优先类别', sort_order=1)
    listing = admin_client.get('/api/comparison-categories')
    assert listing.status_code == 200
    assert [c['id'] for c in listing.json()['items']] == [earlier['id'], later['id']]
    assert all(c['product_count'] == 0 for c in listing.json()['items'])
    renamed = admin_client.put(f"/api/comparison-categories/{later['id']}", json={
        'name': '改名类别', 'description': '新的说明', 'sort_order': 0})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()['name'] == '改名类别'
    assert renamed.json()['description'] == '新的说明'
    assert admin_client.post('/api/comparison-categories', json={'name': '改名类别'}).status_code == 409
    assert admin_client.put(f"/api/comparison-categories/{earlier['id']}", json={
        'name': '改名类别'}).status_code == 409
    item = group(admin_client, category_id=later['id'])
    assert item['category_name'] == '改名类别'
    assert admin_client.delete(f"/api/comparison-categories/{later['id']}").status_code == 409
    assert admin_client.delete(f"/api/mappings/{item['id']}").status_code == 200
    assert admin_client.delete(f"/api/comparison-categories/{later['id']}").status_code == 200


def test_create_category_then_empty_product_then_attach_platform_quotes(db, admin_client):
    classification = category(admin_client)
    item = group(admin_client, category_id=classification['id'], description='人工确认相似互动服务',
                 match_mode='similar', unit='')
    assert item['category_id'] == classification['id']
    assert item['description'] == '人工确认相似互动服务'
    assert item['product_ids'] == [] and item['product_count'] == 0
    rows = offers(db, [{'platform': 'A', 'id': 'a'}, {'platform': 'B', 'id': 'b', 'unit': '次'}])
    saved = admin_client.put(f"/api/mappings/{item['id']}", json={
        'name': item['name'], 'unit': '', 'currency': 'CNY', 'match_mode': 'similar',
        'category_id': classification['id'], 'description': item['description'],
        'product_ids': [p.id for p in rows.values()]})
    assert saved.status_code == 200, saved.text
    assert saved.json()['product_count'] == 2 and saved.json()['platform_count'] == 2
    detail = admin_client.get(f"/api/mappings/{item['id']}")
    assert detail.status_code == 200
    assert {p['id'] for p in detail.json()['products']} == {p.id for p in rows.values()}
    assert detail.json()['category_name'] == classification['name']


def test_mapping_category_search_pagination_and_uncategorized_filter(admin_client):
    classification = category(admin_client)
    first = group(admin_client, name='可搜索点赞A', category_id=classification['id'])
    second = group(admin_client, name='可搜索点赞B', category_id=classification['id'])
    unclassified = group(admin_client, name='未分类播放')
    listing = admin_client.get(f"/api/mappings?category_id={classification['id']}&q=可搜索&page=2&page_size=1")
    assert listing.status_code == 200, listing.text
    assert listing.json()['total'] == 2 and len(listing.json()['items']) == 1
    assert listing.json()['page'] == 2 and listing.json()['page_size'] == 1
    first_page = admin_client.get(f"/api/mappings?category_id={classification['id']}&page=1&page_size=1").json()
    assert {first_page['items'][0]['id'], listing.json()['items'][0]['id']} == {first['id'], second['id']}
    only_unclassified = admin_client.get('/api/mappings?uncategorized=true').json()
    assert [g['id'] for g in only_unclassified['items']] == [unclassified['id']]


def test_legacy_mapping_update_preserves_category_description_and_similar_mode(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'a'}, {'platform': 'B', 'id': 'b', 'unit': '次'}])
    classification = category(admin_client)
    item = group(admin_client, category_id=classification['id'], description='保留人工说明',
                 match_mode='similar', unit='', product_ids=[p.id for p in rows.values()])
    response = admin_client.put(f"/api/mappings/{item['id']}", json={
        'name': '旧客户端改名', 'unit': '', 'currency': 'CNY',
        'product_ids': [p.id for p in rows.values()]})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['category_id'] == classification['id']
    assert saved['description'] == '保留人工说明' and saved['match_mode'] == 'similar'
    cleared = admin_client.put(f"/api/mappings/{item['id']}", json={
        'name': saved['name'], 'unit': '', 'currency': 'CNY', 'category_id': None,
        'product_ids': [p.id for p in rows.values()]})
    assert cleared.status_code == 200 and cleared.json()['category_id'] is None
    assert admin_client.get('/api/mappings?uncategorized=true').json()['total'] == 1


def test_similar_group_shows_all_mixed_basis_quotes_without_minimum(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'a', 'price': '0.007486', 'unit': '人'},
                       {'platform': 'B', 'id': 'b', 'price': '0.00000001', 'unit': '', 'currency': 'USD',
                        'quality': ['unit_requires_review']},
                       {'platform': 'B', 'id': 'c', 'status': 'closed'}])
    item = group(admin_client, match_mode='similar', unit='', product_ids=[p.id for p in rows.values()])
    response = admin_client.get(f"/api/mappings/{item['id']}/compare")
    assert response.status_code == 200, response.text
    result = response.json()
    assert {p['id'] for p in result['items']} == {p.id for p in rows.values()}
    assert result['comparable'] is False and result['lowest_product_ids'] == []
    assert result['eligible_product_ids'] == []
    assert all(p['comparison_eligible'] is False and p['comparison_reason'] for p in result['items'])
    quoted = {p['id']: p for p in result['items']}
    assert quoted[rows['a'].id]['price'] == '0.007486'
    assert quoted[rows['b'].id]['price'] == '0.00000001'


def test_exact_compare_preserves_ineligible_quotes_and_decimal_minimum_ties(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'a', 'price': '0.007486'},
                       {'platform': 'B', 'id': 'b', 'price': '0.007486'},
                       {'platform': 'C', 'id': 'c', 'price': '0.007487'},
                       {'platform': 'D', 'id': 'closed', 'price': '0.00000001', 'status': 'closed'},
                       {'platform': 'D', 'id': 'stale', 'price': '0.00000002', 'age': 10800},
                       {'platform': 'D', 'id': 'quality', 'price': '0.00000003', 'quality': ['invalid_price']}])
    item = group(admin_client, product_ids=[p.id for p in rows.values()])
    result = admin_client.get(f"/api/mappings/{item['id']}/compare").json()
    assert result['comparable'] is True
    assert {p['id'] for p in result['items']} == {p.id for p in rows.values()}
    assert set(result['eligible_product_ids']) == {rows[k].id for k in ('a', 'b', 'c')}
    assert set(result['lowest_product_ids']) == {rows[k].id for k in ('a', 'b')}
    quotes = {p['id']: p for p in result['items']}
    for key in ('closed', 'stale', 'quality'):
        assert quotes[rows[key].id]['comparison_eligible'] is False
        assert quotes[rows[key].id]['comparison_reason']


def test_multiple_quotes_from_one_platform_do_not_form_cross_platform_minimum(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'a'}, {'platform': 'A', 'id': 'b', 'price': '0.005'}])
    item = group(admin_client, product_ids=[p.id for p in rows.values()])
    result = admin_client.get(f"/api/mappings/{item['id']}/compare").json()
    assert result['comparable'] is False
    assert result['lowest_product_ids'] == []
    assert len(result['items']) == 2


def test_out_of_window_quotes_do_not_claim_a_cross_platform_minimum(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'a'}, {'platform': 'B', 'id': 'b', 'age': 3660}])
    item = group(admin_client, product_ids=[p.id for p in rows.values()])
    result = admin_client.get(f"/api/mappings/{item['id']}/compare").json()
    assert result['comparable'] is False and result['lowest_product_ids'] == []
    assert len(result['items']) == 2


def test_source_basis_change_retains_offer_but_removes_its_minimum_eligibility(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'a'}, {'platform': 'B', 'id': 'b'}])
    item = group(admin_client, product_ids=[p.id for p in rows.values()])
    rows['b'].unit = '次'
    db.commit()
    result = admin_client.get(f"/api/mappings/{item['id']}/compare").json()
    assert {p['id'] for p in result['items']} == {p.id for p in rows.values()}
    assert result['comparable'] is False and result['lowest_product_ids'] == []
    changed = next(p for p in result['items'] if p['id'] == rows['b'].id)
    assert changed['unit'] == '次' and changed['comparison_eligible'] is False
    assert changed['comparison_reason']


def test_existing_product_is_not_silently_moved_between_unified_products(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'a'}])
    original = group(admin_client, product_ids=[rows['a'].id])
    response = admin_client.post('/api/mappings', json={'name': '竞争归属', 'match_mode': 'similar',
        'product_ids': [rows['a'].id]})
    assert response.status_code == 409, response.text
    db.expire_all()
    assert db.get(Product, rows['a'].id).canonical_id == original['id']
    assert len(admin_client.get('/api/mappings').json()['items']) == 1


@pytest.mark.parametrize('identifiers', [[True], ['1'], [1.0], [0], [-1], [1, 1], [None], list(range(1, 5002))])
def test_mapping_product_ids_are_strict_positive_unique_and_bounded(admin_client, identifiers):
    response = admin_client.post('/api/mappings', json={'name': 'invalid identifiers',
        'match_mode': 'similar', 'product_ids': identifiers})
    assert response.status_code == 422, response.text


def test_unknown_category_and_match_mode_are_rejected(admin_client):
    for changes in ({'category_id': 999999}, {'match_mode': 'unknown'}):
        response = admin_client.post('/api/mappings', json={'name': 'invalid mapping', **changes})
        assert response.status_code == 422, response.text


def test_category_scope_counts_and_detail_exclude_unauthorized_platforms(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'a'}, {'platform': 'B', 'id': 'b'},
                       {'platform': 'B', 'id': 'private'}])
    shared_category = category(admin_client, 'visible-category')
    private_category = category(admin_client, 'hidden-category')
    category(admin_client, 'empty-admin-category')
    shared = group(admin_client, name='visible-group', category_id=shared_category['id'],
                   product_ids=[rows['a'].id, rows['b'].id])
    hidden_in_shared = group(admin_client, name='hidden-group-in-shared-category',
                            category_id=shared_category['id'])
    private = group(admin_client, name='hidden-group', category_id=private_category['id'],
                    product_ids=[rows['private'].id])
    with viewer(admin_client, [rows['a'].platform_id]) as client:
        directory = client.get('/api/comparison-categories').json()['items']
        assert len(directory) == 1 and directory[0]['id'] == shared_category['id']
        assert directory[0]['product_count'] == 1
        listing = client.get('/api/mappings').json()
        assert listing['total'] == 1 and listing['items'][0]['id'] == shared['id']
        detail = client.get(f"/api/mappings/{shared['id']}").json()
        assert detail['platform_count'] == 1 and detail['product_count'] == 1
        assert [p['id'] for p in detail['products']] == [rows['a'].id]
        assert client.get(f"/api/mappings?category_id={private_category['id']}").json()['total'] == 0
        for item in (private, hidden_in_shared):
            assert client.get(f"/api/mappings/{item['id']}").status_code == 404
            assert client.get(f"/api/mappings/{item['id']}/compare").status_code == 404
        result = client.get(f"/api/mappings/{shared['id']}/compare").json()
        assert result['comparable'] is False and result['lowest_product_ids'] == []
        assert [p['id'] for p in result['items']] == [rows['a'].id]
        assert 'fixture-platform-B' not in json.dumps(result)


def test_unassigned_viewer_sees_no_categories_or_unified_products(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'a'}])
    classification = category(admin_client)
    item = group(admin_client, category_id=classification['id'], product_ids=[rows['a'].id])
    with viewer(admin_client) as client:
        assert client.get('/api/comparison-categories').json()['items'] == []
        assert client.get('/api/mappings').json()['total'] == 0
        assert client.get(f"/api/mappings/{item['id']}").status_code == 404


def test_category_writes_require_admin_csrf_and_are_audited(db, admin_client):
    classification = category(admin_client)
    with viewer(admin_client) as client:
        for method, path, body in (
            ('POST', '/api/comparison-categories', {'name': 'forbidden'}),
            ('PUT', f"/api/comparison-categories/{classification['id']}", {'name': 'forbidden'}),
            ('DELETE', f"/api/comparison-categories/{classification['id']}", None),
        ):
            assert client.request(method, path, json=body).status_code == 403
    csrf = admin_client.headers.pop('X-CSRF-Token')
    try:
        assert admin_client.post('/api/comparison-categories', json={'name': 'missing-csrf'}).status_code == 403
    finally:
        admin_client.headers['X-CSRF-Token'] = csrf
    assert admin_client.put(f"/api/comparison-categories/{classification['id']}", json={
        'name': 'audited-update'}).status_code == 200
    assert admin_client.delete(f"/api/comparison-categories/{classification['id']}").status_code == 200
    db.expire_all()
    actions = list(db.scalars(select(AuditLog.action)))
    assert any('category' in action and 'create' in action for action in actions)
    assert any('category' in action and ('update' in action or 'edit' in action) for action in actions)
    assert any('category' in action and 'delete' in action for action in actions)


def test_catalogue_search_matches_description_without_exposing_hidden_groups(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'allowed'}, {'platform': 'B', 'id': 'hidden'}])
    visible = group(admin_client, name='名称不含搜索词', description='支持快速渠道与长售后',
                    product_ids=[rows['allowed'].id])
    group(admin_client, name='另一商品', description='支持快速渠道与短售后',
          product_ids=[rows['hidden'].id])
    assert admin_client.get('/api/mappings', params={'q': '快速渠道'}).json()['total'] == 2
    with viewer(admin_client, [rows['allowed'].platform_id]) as client:
        response = client.get('/api/mappings', params={'q': '快速渠道'})
        assert response.status_code == 200
        assert response.json()['total'] == 1
        assert [item['id'] for item in response.json()['items']] == [visible['id']]


def test_repeated_application_startup_preserves_existing_mapping_and_additive_schema(db, admin_client):
    item = group(admin_client, description='persistent-category-schema', match_mode='similar')
    for _ in range(2):
        with TestClient(app):
            pass
    db.expire_all()
    assert db.get(CanonicalProduct, item['id']).description == 'persistent-category-schema'
    columns = {column['name'] for column in inspect(engine).get_columns('canonical_products')}
    assert {'category_id', 'description', 'match_mode'}.issubset(columns)
    assert inspect(engine).has_table('comparison_categories')
