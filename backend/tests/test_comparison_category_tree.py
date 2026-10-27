"""Multi-level comparison categories in the isolated local test database.

All offers are fixed fixtures. No production category, account grant, source
session, or upstream collection is used by these tests.
"""
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect, select, text

from test_pipeline import isolated_database, clean_test_resources, db, admin_client
from test_comparison_catalogue import category, group, offers, viewer
from app.db import engine
from app.main import app
from app.models import CanonicalProduct, ComparisonCategory, Product


def directory(client):
    response = client.get('/api/comparison-categories')
    assert response.status_code == 200, response.text
    return {item['id']: item for item in response.json()['items']}


def path(*categories):
    return [{'id': item['id'], 'name': item['name']} for item in categories]


def test_same_child_name_can_exist_under_different_business_roots(admin_client):
    douyin = category(admin_client, '抖音')
    kuaishou = category(admin_client, '快手')
    douyin_video = category(admin_client, '视频', parent_id=douyin['id'])
    kuaishou_video = category(admin_client, '视频', parent_id=kuaishou['id'])
    livestream = category(admin_client, '直播', parent_id=douyin['id'])
    assert douyin_video['parent_id'] == douyin['id']
    assert kuaishou_video['parent_id'] == kuaishou['id']
    items = directory(admin_client)
    assert items[douyin['id']]['depth'] == 0
    assert items[douyin['id']]['children_count'] == 2
    assert items[kuaishou['id']]['children_count'] == 1
    assert items[douyin_video['id']]['depth'] == 1
    assert items[douyin_video['id']]['path'] == path(douyin, douyin_video)
    assert items[douyin_video['id']]['path_name'] == '抖音 / 视频'
    assert items[kuaishou_video['id']]['path_name'] == '快手 / 视频'
    assert items[livestream['id']]['path_name'] == '抖音 / 直播'
    assert admin_client.post('/api/comparison-categories', json={
        'name': '视频', 'parent_id': douyin['id']}).status_code == 409
    assert admin_client.post('/api/comparison-categories', json={'name': '抖音'}).status_code == 409
    # Root and child may also share a label; only siblings collide.
    root_video = category(admin_client, '视频')
    assert directory(admin_client)[root_video['id']]['parent_id'] is None


def test_business_video_product_links_multiple_platform_quotes(db, admin_client):
    root = category(admin_client, '抖音')
    video = category(admin_client, '视频', parent_id=root['id'])
    rows = offers(db, [{'platform': 'A', 'id': 'a', 'price': '0.007486'},
                       {'platform': 'B', 'id': 'b', 'unit': '', 'price': '0.009'}])
    item = group(admin_client, name='抖音视频点赞', category_id=video['id'], match_mode='similar',
                 unit='', product_ids=[p.id for p in rows.values()])
    assert item['category_path'] == path(root, video)
    assert item['category_path_name'] == '抖音 / 视频'
    assert item['category_name'] == '视频'
    assert item['platform_count'] == 2
    details = admin_client.get(f"/api/mappings/{item['id']}").json()
    comparison = admin_client.get(f"/api/mappings/{item['id']}/compare").json()
    assert details['category_path'] == path(root, video)
    assert comparison['group']['category_path_name'] == '抖音 / 视频'
    assert {offer['id'] for offer in comparison['items']} == {p.id for p in rows.values()}
    assert comparison['comparable'] is False and comparison['lowest_product_ids'] == []
    listing = admin_client.get(f"/api/mappings?category_id={root['id']}").json()
    assert listing['total'] == 1 and listing['items'][0]['id'] == item['id']
    assert admin_client.get(f"/api/mappings?category_id={root['id']}&include_descendants=false").json()['items'] == []


def test_parent_filter_and_counts_cover_subtree_without_double_counting_platforms(db, admin_client):
    root = category(admin_client, '抖音')
    video = category(admin_client, '视频', parent_id=root['id'])
    live = category(admin_client, '直播', parent_id=root['id'])
    other = category(admin_client, '小红书')
    rows = offers(db, [{'platform': 'A', 'id': 'a'}, {'platform': 'B', 'id': 'b'},
                       {'platform': 'A', 'id': 'c'}, {'platform': 'B', 'id': 'd'}])
    root_group = group(admin_client, '根直属商品', category_id=root['id'])
    video_group = group(admin_client, '视频点赞', category_id=video['id'],
                        product_ids=[rows['a'].id, rows['b'].id])
    live_group = group(admin_client, '直播点赞', category_id=live['id'], product_ids=[rows['c'].id])
    group(admin_client, '其他业务', category_id=other['id'], product_ids=[rows['d'].id])
    items = directory(admin_client)
    assert items[root['id']]['product_count'] == 3
    assert items[root['id']]['direct_product_count'] == 1
    assert items[root['id']]['offer_count'] == 3
    assert items[root['id']]['platform_count'] == 2  # A occurs in both children.
    assert items[video['id']]['product_count'] == items[video['id']]['direct_product_count'] == 1
    listing = admin_client.get(f"/api/mappings?category_id={root['id']}").json()
    assert listing['total'] == 3
    assert {g['id'] for g in listing['items']} == {g['id'] for g in (root_group, video_group, live_group)}
    direct = admin_client.get(f"/api/mappings?category_id={root['id']}&include_descendants=false").json()
    assert [g['id'] for g in direct['items']] == [root_group['id']]
    page = admin_client.get(f"/api/mappings?category_id={root['id']}&q=点赞&page=2&page_size=1").json()
    assert page['total'] == 2 and len(page['items']) == 1 and page['page'] == 2


def test_viewer_directory_contains_only_authorized_branch_and_required_ancestors(db, admin_client):
    root = category(admin_client, '抖音')
    video = category(admin_client, '视频', parent_id=root['id'])
    likes = category(admin_client, '点赞服务', parent_id=video['id'])
    hidden_sibling = category(admin_client, 'private-live-branch', parent_id=root['id'])
    hidden_root = category(admin_client, 'private-business-root')
    empty_root = category(admin_client, 'empty-admin-root')
    rows = offers(db, [{'platform': 'A', 'id': 'a'}, {'platform': 'B', 'id': 'b'},
                       {'platform': 'B', 'id': 'c'}, {'platform': 'B', 'id': 'd'}])
    visible = group(admin_client, '授权视频点赞', category_id=likes['id'],
                    product_ids=[rows['a'].id, rows['b'].id])
    hidden = group(admin_client, 'private-unified-product', category_id=hidden_sibling['id'],
                   product_ids=[rows['c'].id])
    group(admin_client, 'private-root-product', category_id=hidden_root['id'], product_ids=[rows['d'].id])
    group(admin_client, 'private-empty-product', category_id=video['id'])
    with viewer(admin_client, [rows['a'].platform_id]) as client:
        items = directory(client)
        assert set(items) == {root['id'], video['id'], likes['id']}
        assert items[root['id']]['children_count'] == 1
        assert items[video['id']]['children_count'] == 1
        assert items[likes['id']]['children_count'] == 0
        for node in (root, video, likes):
            assert items[node['id']]['product_count'] == 1
            assert items[node['id']]['offer_count'] == 1
            assert items[node['id']]['platform_count'] == 1
        assert items[root['id']]['direct_product_count'] == 0
        assert items[video['id']]['direct_product_count'] == 0
        assert items[likes['id']]['direct_product_count'] == 1
        assert items[likes['id']]['path'] == path(root, video, likes)
        response = client.get(f"/api/mappings?category_id={root['id']}")
        assert response.status_code == 200
        assert response.json()['total'] == 1
        assert response.json()['items'][0]['id'] == visible['id']
        assert len(response.json()['items'][0]['products']) == 1
        assert 'private-' not in json.dumps(items)
        assert client.get(f"/api/mappings?category_id={hidden_sibling['id']}").json()['total'] == 0
        assert client.get(f"/api/mappings/{hidden['id']}").status_code == 404
        assert client.get(f"/api/mappings/{hidden['id']}/compare").status_code == 404
    assert empty_root['id'] in directory(admin_client)


def test_viewer_with_no_grants_cannot_discover_empty_business_roots(admin_client):
    root = category(admin_client, '抖音')
    category(admin_client, '视频', parent_id=root['id'])
    category(admin_client, '快手')
    with viewer(admin_client) as client:
        assert directory(client) == {}


@pytest.mark.parametrize('invalid_parent', [True, '1', 0, -1, 1.5])
def test_parent_identifier_is_a_strict_positive_integer_or_null(admin_client, invalid_parent):
    response = admin_client.post('/api/comparison-categories', json={'name': '非法父级', 'parent_id': invalid_parent})
    assert response.status_code == 422, response.text
    assert directory(admin_client) == {}


@pytest.mark.parametrize('case', ['self', 'child', 'grandchild', 'missing'])
def test_category_cannot_move_below_itself_descendants_or_unknown_parent(admin_client, case):
    root = category(admin_client, '抖音')
    child = category(admin_client, '视频', parent_id=root['id'])
    grandchild = category(admin_client, '互动', parent_id=child['id'])
    invalid = {'self': root['id'], 'child': child['id'], 'grandchild': grandchild['id'], 'missing': 999999}[case]
    response = admin_client.put(f"/api/comparison-categories/{root['id']}", json={
        'name': root['name'], 'parent_id': invalid})
    assert response.status_code == 422, response.text
    items = directory(admin_client)
    assert items[root['id']]['parent_id'] is None
    assert items[grandchild['id']]['path'] == path(root, child, grandchild)


def test_unknown_parent_is_rejected_during_creation(admin_client):
    response = admin_client.post('/api/comparison-categories', json={'name': '无父级', 'parent_id': 999999})
    assert response.status_code == 422, response.text
    assert directory(admin_client) == {}


def test_eight_level_limit_applies_to_creation_and_entire_moved_subtree(admin_client):
    chain = [category(admin_client, '层级0')]
    for level in range(1, 8):
        chain.append(category(admin_client, f'层级{level}', parent_id=chain[-1]['id']))
    assert directory(admin_client)[chain[-1]['id']]['depth'] == 7
    response = admin_client.post('/api/comparison-categories', json={'name': '层级8', 'parent_id': chain[-1]['id']})
    assert response.status_code == 422, response.text
    source_root = category(admin_client, '可移动业务')
    branch = category(admin_client, '视频', parent_id=source_root['id'])
    leaf = category(admin_client, '互动', parent_id=branch['id'])
    response = admin_client.put(f"/api/comparison-categories/{branch['id']}", json={
        'name': branch['name'], 'parent_id': chain[6]['id']})
    assert response.status_code == 422, response.text  # Branch fits, but its child would be depth 8.
    assert directory(admin_client)[branch['id']]['parent_id'] == source_root['id']
    response = admin_client.put(f"/api/comparison-categories/{branch['id']}", json={
        'name': branch['name'], 'parent_id': chain[5]['id']})
    assert response.status_code == 200, response.text
    assert directory(admin_client)[leaf['id']]['depth'] == 7


def test_renaming_without_parent_preserves_branch_and_explicit_null_moves_to_root(db, admin_client):
    root = category(admin_client, '抖音')
    video = category(admin_client, '视频', parent_id=root['id'])
    child = category(admin_client, '互动', parent_id=video['id'])
    item = group(admin_client, '抖音视频点赞', category_id=child['id'])
    response = admin_client.put(f"/api/comparison-categories/{video['id']}", json={'name': '短视频'})
    assert response.status_code == 200, response.text
    assert response.json()['parent_id'] == root['id']
    detail = admin_client.get(f"/api/mappings/{item['id']}").json()
    assert detail['category_path_name'] == '抖音 / 短视频 / 互动'
    moved = admin_client.put(f"/api/comparison-categories/{video['id']}", json={'name': '短视频', 'parent_id': None})
    assert moved.status_code == 200, moved.text
    assert moved.json()['parent_id'] is None
    assert directory(admin_client)[child['id']]['path_name'] == '短视频 / 互动'
    assert admin_client.get(f"/api/mappings?category_id={root['id']}").json()['total'] == 0
    db.expire_all()
    assert db.get(CanonicalProduct, item['id']).category_id == child['id']


def test_move_rejects_sibling_name_conflict_and_preserves_existing_tree(admin_client):
    first = category(admin_client, '抖音')
    second = category(admin_client, '快手')
    source_video = category(admin_client, '视频', parent_id=first['id'])
    category(admin_client, '视频', parent_id=second['id'])
    response = admin_client.put(f"/api/comparison-categories/{source_video['id']}", json={
        'name': '视频', 'parent_id': second['id']})
    assert response.status_code == 409, response.text
    assert directory(admin_client)[source_video['id']]['parent_id'] == first['id']


def test_category_delete_requires_no_child_and_no_direct_product(admin_client):
    root = category(admin_client, '抖音')
    video = category(admin_client, '视频', parent_id=root['id'])
    assert admin_client.delete(f"/api/comparison-categories/{root['id']}").status_code == 409
    item = group(admin_client, category_id=video['id'])
    assert admin_client.delete(f"/api/comparison-categories/{video['id']}").status_code == 409
    assert admin_client.delete(f"/api/mappings/{item['id']}").status_code == 200
    assert admin_client.delete(f"/api/comparison-categories/{video['id']}").status_code == 200
    assert admin_client.delete(f"/api/comparison-categories/{root['id']}").status_code == 200


def test_startup_upgrades_legacy_global_name_constraint_without_losing_links(db, admin_client):
    root = category(admin_client, '旧单级分类')
    item = group(admin_client, '已有关联商品', category_id=root['id'], description='保留说明')
    # Recreate the legacy constraint on the isolated table. The production
    # migration must remove it without replacing either table or its records.
    # The fixture Session must not retain an ACCESS SHARE lock while a separate
    # connection performs DDL, including when this test fails during assertions.
    db.rollback()
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text('ALTER TABLE comparison_categories ADD CONSTRAINT comparison_categories_name_key UNIQUE (name)'))
    try:
        for _ in range(2):
            db.rollback()
            with TestClient(app):
                pass
        db.expire_all()
        assert db.get(CanonicalProduct, item['id']).category_id == root['id']
        assert db.get(CanonicalProduct, item['id']).description == '保留说明'
        assert db.get(ComparisonCategory, root['id']).name == '旧单级分类'
        assert db.get(ComparisonCategory, root['id']).parent_id is None
        constraints = inspect(engine).get_unique_constraints('comparison_categories')
        assert not any(c.get('column_names') == ['name'] for c in constraints)
        other_root = category(admin_client, '新业务')
        category(admin_client, '视频', parent_id=root['id'])
        category(admin_client, '视频', parent_id=other_root['id'])
        assert admin_client.post('/api/comparison-categories', json={'name': root['name']}).status_code == 409
    finally:
        db.rollback()
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            connection.execute(text('ALTER TABLE comparison_categories DROP CONSTRAINT IF EXISTS comparison_categories_name_key'))
