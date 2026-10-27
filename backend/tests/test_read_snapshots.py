"""Concurrent committed catalogue updates must not split one read response."""
from decimal import Decimal

from sqlalchemy import event, update

from test_pipeline import isolated_database, clean_test_resources, db, admin_client
from test_comparison_catalogue import offers, group
from app.db import engine
from app.models import Product, Observation


def test_candidate_count_revision_and_page_share_one_snapshot(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'a', 'name': 'B站点赞'},
                       {'platform': 'B', 'id': 'b', 'name': 'BL点赞'}])
    params = {'q': 'B站 点赞', 'page_size': 100}
    baseline = admin_client.get('/api/product-suggestions', params=params).json()
    changed = []

    def change_after_count(connection, cursor, statement, parameters, context, many):
        if not changed and 'SELECT count(*)' in statement and 'FROM products' in statement:
            changed.append(True)
            # A separate real transaction commits after count(), before the
            # fingerprint and page queries execute in the request transaction.
            with engine.begin() as writer:
                writer.execute(update(Product).where(Product.id == rows['b'].id)
                               .values(name='无关商品', price=Decimal('0.999')))

    event.listen(engine, 'after_cursor_execute', change_after_count)
    try:
        response = admin_client.get('/api/product-suggestions', params=params)
    finally:
        event.remove(engine, 'after_cursor_execute', change_after_count)
    assert response.status_code == 200, response.text
    data = response.json()
    assert changed and data['total'] == 2 and len(data['items']) == 2
    assert data['result_token'] == baseline['result_token']
    assert next(p for p in data['items'] if p['id'] == rows['b'].id)['name'] == 'BL点赞'
    next_read = admin_client.get('/api/product-suggestions', params=params).json()
    assert next_read['total'] == 1
    assert next_read['result_token'] != baseline['result_token']


def test_current_extremes_and_hourly_history_share_one_snapshot(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'a', 'price': '0.1'},
                       {'platform': 'B', 'id': 'b', 'price': '0.2'}])
    item = group(admin_client, product_ids=[p.id for p in rows.values()])
    changed = []

    def change_after_products(connection, cursor, statement, parameters, context, many):
        if not changed and statement.startswith('SELECT products.') and 'products.canonical_id' in statement:
            changed.append(True)
            with engine.begin() as writer:
                writer.execute(update(Product).where(Product.id == rows['b'].id).values(price=Decimal('0.9')))
                writer.execute(update(Observation).where(Observation.product_id == rows['b'].id)
                               .values(price=Decimal('0.9')))

    event.listen(engine, 'after_cursor_execute', change_after_products)
    try:
        response = admin_client.get(f"/api/mappings/{item['id']}/trend?days=1")
    finally:
        event.remove(engine, 'after_cursor_execute', change_after_products)
    assert response.status_code == 200, response.text
    data = response.json()['groups'][0]
    assert changed and Decimal(data['current']['maximum']) == Decimal('0.2')
    assert {Decimal(p['maximum']) for p in data['items'] if p['maximum']} == {Decimal('0.2')}
    next_read = admin_client.get(f"/api/mappings/{item['id']}/trend?days=1").json()['groups'][0]
    assert Decimal(next_read['current']['maximum']) == Decimal('0.9')
