"""Real PostgreSQL keyword matching with fixed catalogues, never upstream traffic."""
import json
import pytest
from sqlalchemy import select

from test_pipeline import isolated_database, clean_test_resources, db, admin_client
from test_comparison_catalogue import offers, group, viewer
from app.models import Product
from app.product_matching import parse_keywords


def test_keyword_segmentation_fullwidth_aliases_and_business_boundaries():
    for value in ('bili-点赞', 'Ｂ站 点赞', '哔哩哔哩点赞', 'BL 赞'):
        assert {g['label'] for g in parse_keywords(value)} == {'哔哩哔哩', '点赞'}
    assert {g['label'] for g in parse_keywords('study blue')} == {'study', 'blue'}
    assert {g['label'] for g in parse_keywords('TikTok 点赞')} == {'TikTok', '点赞'}
    assert '抖音' not in {g['label'] for g in parse_keywords('TikTok 点赞')}


@pytest.fixture
def catalogue(db):
    specs = [
        {'platform':'A','id':'a','name':'B站视频点赞'},
        {'platform':'B','id':'b','name':'BL-极速赞'},
        {'platform':'B','id':'c','name':'bili评论点赞'},
        {'platform':'B','id':'d','name':'B站三连套餐'},
        {'platform':'A','id':'category-only','name':'通用渠道'},
        {'platform':'A','id':'wrong','name':'抖音真人点赞'},
        {'platform':'A','id':'noise','name':'blue study likes'},
        {'platform':'B','id':'closed','name':'Ｂ站 点赞','status':'closed'},
    ]
    rows = offers(db, specs)
    rows['category-only'].category_name = '哔哩哔哩 / 点赞'
    rows['d'].category_name = 'B站点赞'
    db.commit()
    return rows


def test_recommendations_match_name_category_synonyms_and_explain_warnings(db, admin_client, catalogue):
    response = admin_client.get('/api/product-suggestions', params={'q':'bili 点赞'})
    assert response.status_code == 200, response.text
    result = response.json()
    ids = {p['id'] for p in result['items']}
    assert ids == {catalogue[k].id for k in ('a','b','c','d','category-only')}
    assert result['total'] == 5
    assert sum(p['count'] for p in result['platform_counts']) == 5
    assert len(result['platform_counts']) == 2
    assert {k['label'] for k in result['keywords']} == {'哔哩哔哩','点赞'}
    rows = {p['id']:p for p in result['items']}
    assert rows[catalogue['a'].id]['match_score'] > rows[catalogue['category-only'].id]['match_score']
    assert any('仅匹配源类目' in warning for warning in rows[catalogue['category-only'].id]['match_warnings'])
    assert any('评论' in warning for warning in rows[catalogue['c'].id]['match_warnings'])
    assert any('套餐' in warning for warning in rows[catalogue['d'].id]['match_warnings'])
    # Product identity and exact decimal values are unchanged by suggestion reads.
    assert rows[catalogue['a'].id]['price'] == '0.007486'
    db.expire_all()
    assert all(product.canonical_id is None for product in db.scalars(select(Product)))


def test_exclusions_platform_filter_and_pagination_preserve_real_total(admin_client, catalogue):
    response = admin_client.get('/api/product-suggestions', params={
        'q':'B站 点赞','exclude':'评论 三连','platform_id':catalogue['b'].platform_id,'page_size':1})
    assert response.status_code == 200, response.text
    assert response.json()['total'] == 1
    assert response.json()['items'][0]['id'] == catalogue['b'].id
    first = admin_client.get('/api/product-suggestions', params={'q':'B站 赞','page_size':2}).json()
    second = admin_client.get('/api/product-suggestions', params={'q':'B站 赞','page_size':2,'page':2}).json()
    assert first['total'] == second['total'] == 5
    assert not {p['id'] for p in first['items']} & {p['id'] for p in second['items']}
    assert first['items'] == admin_client.get('/api/product-suggestions',params={'q':'B站 赞','page_size':2}).json()['items']


def test_wide_search_is_explicit_closed_option_and_other_ownership_is_explained(admin_client, catalogue):
    owner = group(admin_client, product_ids=[catalogue['b'].id])
    result = admin_client.get('/api/product-suggestions', params={'q':'B站 点赞','active_only':False}).json()
    assert result['total'] == 6
    assert next(p for p in result['items'] if p['id']==catalogue['b'].id)['association_conflict'] is True
    own = admin_client.get('/api/product-suggestions', params={'q':'B站 点赞','mapping_id':owner['id']}).json()
    assert next(p for p in own['items'] if p['id']==catalogue['b'].id)['association_conflict'] is False
    wide = admin_client.get('/api/product-suggestions', params={'q':'B站 点赞','mode':'any'}).json()
    assert catalogue['wrong'].id in {p['id'] for p in wide['items']}
    assert any('部分关键词' in warning for p in wide['items'] if p['id']==catalogue['wrong'].id for warning in p['match_warnings'])
    assert admin_client.get('/api/product-suggestions',params={'q':'B站','mapping_id':999999}).status_code==404


def test_suggestions_remain_admin_only_and_bound_input_is_not_a_wildcard(admin_client, catalogue):
    with viewer(admin_client,[catalogue['a'].platform_id]) as client:
        assert client.get('/api/product-suggestions',params={'q':'B站 点赞'}).status_code==403
    for query in ('%%__', ' '.join('term'+str(i) for i in range(9))):
        assert admin_client.get('/api/product-suggestions',params={'q':query}).status_code==422
    response = admin_client.get('/api/product-suggestions',params={'q':"nothere' OR 1=1 --"})
    assert response.status_code==200 and response.json()['items']==[]
    assert admin_client.get('/api/product-suggestions',params={'q':'x'*161}).status_code==422
