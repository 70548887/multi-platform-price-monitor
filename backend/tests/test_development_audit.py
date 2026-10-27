"""Regressions reproduced during the development audit, in isolated test resources."""
from decimal import Decimal, localcontext

from sqlalchemy import select
from test_pipeline import isolated_database, clean_test_resources, db, admin_client
from test_comparison_catalogue import offers, group, viewer
from app.models import Product
from app.comparison_trend import basis_key


def test_statistical_basis_never_rounds_long_display_quantities():
    left = Decimal('1234567890123456789012345678.1')
    right = Decimal('1234567890123456789012345678.2')
    with localcontext() as context:
        context.prec = 12
        assert basis_key('CNY', '个', left) != basis_key('CNY', '个', right)
        assert basis_key('CNY', '个', Decimal('1.00')) == basis_key('CNY', '个', Decimal('1'))


def test_long_quantity_series_remain_separate_in_current_and_history(db, admin_client):
    rows = offers(db, [
        {'platform':'A','id':'a','quantity':'1234567890123456789012345678.1'},
        {'platform':'B','id':'b','quantity':'1234567890123456789012345678.2'},
    ])
    item = group(admin_client, match_mode='similar', product_ids=[p.id for p in rows.values()])
    result = admin_client.get(f"/api/mappings/{item['id']}/trend?days=1").json()
    assert len(result['groups']) == 2
    assert {item['current']['offer_count'] for item in result['groups']} == {1}


def test_non_ascii_csrf_is_rejected_instead_of_crashing(admin_client):
    for path in ('/api/update/check', '/api/platform-submissions'):
        body = {'name':'test','url':'https://shop.example.org'} if 'submissions' in path else {}
        response = admin_client.post(path, json=body, headers={b'X-CSRF-Token': b'\xc3\xa9'})
        assert response.status_code == 403


def test_validation_responses_do_not_echo_passwords_tokens_or_private_proxy_inputs(admin_client):
    secret = 'audit-fixture-private-value'
    response = admin_client.post('/api/platform-submissions', json={
        'name':'test', 'url':f'https://user:{secret}@shop.example.org'})
    assert response.status_code == 422
    assert secret not in response.text
    response = admin_client.post('/api/auth/login', json={'username':'a', 'password':secret*20})
    assert response.status_code == 422 and secret not in response.text


def test_suggestion_revision_changes_for_same_count_when_candidate_metadata_changes(db, admin_client):
    rows = offers(db, [{'platform':'A','id':'a','name':'B站视频点赞'},
                       {'platform':'B','id':'b','name':'bili 点赞'}])
    response = admin_client.get('/api/product-suggestions',params={'q':'B站 点赞','page_size':1})
    assert response.status_code == 200
    first = response.json()
    assert len(first['result_token']) == 64
    again = admin_client.get('/api/product-suggestions',params={'q':'B站 点赞','page_size':1,'page':2}).json()
    assert first['result_token'] == again['result_token']
    target = db.get(Product, rows['b'].id)
    target.name = 'bili 点赞（规格已变化）'
    db.commit()
    changed = admin_client.get('/api/product-suggestions',params={'q':'B站 点赞','page_size':1,'page':2}).json()
    assert changed['total'] == first['total']
    assert changed['result_token'] != first['result_token']
    # A new owner with unchanged title/total must also invalidate a batch draft.
    old = changed['result_token']
    group(admin_client, product_ids=[target.id])
    assert admin_client.get('/api/product-suggestions',params={'q':'B站 点赞'}).json()['result_token'] != old
