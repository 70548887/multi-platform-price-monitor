"""Price envelopes and member intake, using only the isolated test database."""
from datetime import timedelta
from decimal import Decimal
import json

from sqlalchemy import select

from test_pipeline import isolated_database, clean_test_resources, db, admin_client, run_for
from test_comparison_catalogue import offers, group, viewer
from app.models import Observation, Platform, PlatformSubmission, Rule, AuditLog, utcnow
from app.services import persist_page
from test_pipeline import source


def test_price_envelope_preserves_decimals_hour_gaps_and_permissions(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'a', 'price': '0.007486'},
                       {'platform': 'B', 'id': 'b', 'price': '0.009999'}])
    item = group(admin_client, match_mode='similar', product_ids=[p.id for p in rows.values()])
    response = admin_client.get(f"/api/mappings/{item['id']}/trend?days=1")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload['groups']) == 1
    series = payload['groups'][0]
    assert series['current']['minimum'] == '0.007486'
    assert series['current']['maximum'] == '0.009999'
    assert series['current']['platform_count'] == 2
    valid = [p for p in series['items'] if p['offer_count']]
    assert len(valid) == 1
    assert valid[0]['minimum'] == '0.007486' and valid[0]['maximum'] == '0.009999'
    assert any(p['minimum'] is None and p['offer_count'] == 0 for p in series['items'])
    with viewer(admin_client, [rows['a'].platform_id]) as client:
        visible = client.get(f"/api/mappings/{item['id']}/trend").json()
        assert visible['groups'][0]['current']['maximum'] == '0.007486'
        assert '0.009999' not in json.dumps(visible)
        assert visible['groups'][0]['current']['platform_count'] == 1
    assert admin_client.get(f"/api/mappings/{item['id']}/trend?days=31").status_code == 422


def test_mixed_currency_unit_quantity_are_separate_and_inactive_prices_excluded(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'a', 'unit': '', 'quantity': '1', 'price': '0.1'},
                       {'platform': 'B', 'id': 'b', 'unit': '人', 'quantity': '1', 'price': '0.2'},
                       {'platform': 'B', 'id': 'c', 'unit': '人', 'currency': 'USD', 'quantity': '1'},
                       {'platform': 'B', 'id': 'd', 'unit': '人', 'quantity': '1000'},
                       {'platform': 'B', 'id': 'closed', 'status': 'closed', 'price': '0'},
                       {'platform': 'B', 'id': 'invalid', 'quality': ['invalid_price'], 'price': '0'}])
    item = group(admin_client, match_mode='similar', unit='', product_ids=[p.id for p in rows.values()])
    response = admin_client.get(f"/api/mappings/{item['id']}/trend")
    assert response.status_code == 200, response.text
    data = response.json()['groups']
    assert len(data) == 4
    assert sum(x['current']['offer_count'] for x in data) == 4
    assert next(x for x in data if not x['unit'])['unit_requires_review'] is True
    assert len({x['key'] for x in data}) == 4


def test_last_closed_observation_in_hour_does_not_fall_back_to_earlier_quote(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'a', 'price': '0.2'}])
    product = rows['a']
    now = utcnow()
    previous_hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    old = db.scalar(select(Observation).where(Observation.product_id == product.id))
    old.observed_at = previous_hour + timedelta(minutes=5)
    platform = db.get(Platform, product.platform_id)
    rule = db.get(Rule, platform.active_rule_id)
    run = run_for(db, platform, rule, offset=10)
    persist_page(db, run, [source(identifier='a', status='closed', price='0')], observed_at=previous_hour+timedelta(minutes=40))
    db.commit()
    item = group(admin_client, product_ids=[product.id])
    data = admin_client.get(f"/api/mappings/{item['id']}/trend?days=1").json()
    assert data['groups'] == []


def test_unauthorized_trend_is_not_discoverable(db, admin_client):
    rows = offers(db, [{'platform': 'A', 'id': 'a'}])
    item = group(admin_client, product_ids=[rows['a'].id])
    with viewer(admin_client) as client:
        assert client.get(f"/api/mappings/{item['id']}/trend").status_code == 404


def test_member_submission_keeps_public_origin_and_own_records_only(db, admin_client):
    with viewer(admin_client) as client:
        response = client.post('/api/platform-submissions', json={
            'name': ' 新平台 ', 'url': 'https://shop.example.org/login?token=do-not-store#private', 'notes': '有商品目录'})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['name'] == '新平台' and data['url'] == 'https://shop.example.org'
        assert 'do-not-store' not in response.text
        assert data['status'] == 'pending'
        assert client.post('/api/platform-submissions', json={'name': '重复', 'url': data['url']}).status_code == 409
        assert client.patch(f"/api/platform-submissions/{data['id']}", json={'status': 'accepted'}).status_code == 403
        row = db.get(PlatformSubmission, data['id'])
        other = PlatformSubmission(user_id=1, name='管理员记录', url='https://other.example.org')
        db.add(other)
        db.commit()
        assert client.get('/api/platform-submissions').json()['total'] == 1
        review = admin_client.patch(f"/api/platform-submissions/{row.id}", json={'status': 'accepted', 'admin_note': '计划接入'})
        assert review.status_code == 200, review.text
        assert client.get('/api/platform-submissions').json()['items'][0]['admin_note'] == '计划接入'
        assert admin_client.patch(f"/api/platform-submissions/{row.id}", json={'status': 'rejected'}).status_code == 409
        assert admin_client.get('/api/platform-submissions').json()['total'] == 2
    db.expire_all()
    assert 'do-not-store' not in db.get(PlatformSubmission, data['id']).url
    assert {'submit_platform', 'review_platform_submission'} <= set(db.scalars(select(AuditLog.action)))


def test_submission_url_validation_csrf_and_pending_limit(admin_client):
    with viewer(admin_client) as client:
        for url in ('https://u:password@example.org', 'http://127.0.0.1', 'http://[::1]',
                    'http://localhost', 'ftp://public.example.org', 'https://public.example.org:9999',
                    'https://api.zxcbug.com', 'https://local.internal'):
            assert client.post('/api/platform-submissions', json={'name': 'invalid', 'url': url}).status_code == 422
        csrf = client.headers.pop('X-CSRF-Token')
        assert client.post('/api/platform-submissions', json={'name': 'missing-csrf', 'url':'https://new.example.org'}).status_code == 403
        client.headers['X-CSRF-Token'] = csrf
        for i in range(5):
            assert client.post('/api/platform-submissions', json={'name': '平台', 'url':f'https://site{i}.example.org'}).status_code == 200
        assert client.post('/api/platform-submissions', json={'name':'超限', 'url':'https://sixth.example.org'}).status_code == 429
