"""Guarded AI proposals use fixed source fixtures, never a model or production DB."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from test_pipeline import isolated_database, clean_test_resources, db, admin_client
from test_comparison_catalogue import offers, category, group
from app import ai_suggestions as suggestions
from app.ai_selection import product_fingerprint
from app.models import (AIAnalysisRun, AIAnalysisBatch, AIApplication, AISuggestion,
                        CanonicalProduct, Product, utcnow)


def catalogue(admin, business='抖音'):
    root = category(admin, business)
    return category(admin, '视频', parent_id=root['id'])


def source_products(db, names=('抖音视频真人点赞', 'DY视频协议赞'), **changes):
    rows = offers(db, [dict({'platform': chr(65+i), 'id': str(i), 'name': name}, **changes)
                       for i, name in enumerate(names)])
    for row in rows.values():
        row.category_name = '抖音 / 视频' if '抖音' in names[0] else '哔哩哔哩 / 视频'
    db.commit()
    return list(rows.values())


def batch_for(db, products):
    run = AIAnalysisRun(dedupe_key='fixture:' + str(utcnow().timestamp()), trigger='manual',
                        settings_revision=1, model='fixture', status='running')
    db.add(run)
    db.flush()
    snapshots = [{'id': row.id, 'name': row.name, 'platform_id': row.platform_id,
                  'platform_name': 'fixture-platform-' + chr(65+i),
                  'source_category': row.category_name, 'price': str(row.price),
                  'display_quantity': str(row.display_quantity), 'unit': row.unit,
                  'currency': row.currency, 'quality': row.quality, 'status': row.status,
                  'canonical_id': row.canonical_id, 'mapping_revision': row.mapping_revision or 0,
                  'current_group_name': '', 'fingerprint': product_fingerprint(row)}
                 for i, row in enumerate(products)]
    batch = AIAnalysisBatch(run_id=run.id, ordinal=1, status='succeeded',
                            product_ids=[row.id for row in products], snapshot={'products': snapshots})
    db.add(batch)
    db.flush()
    return run, batch


def propose(db, products, *, auto=False, path='抖音 / 视频', name='抖音视频点赞'):
    run, batch = batch_for(db, products)
    report = {'group_suggestions': [{'name': name, 'category_path': path,
               'product_ids': [row.id for row in products], 'reason': '固定测试建议'}]}
    rows = suggestions.ingest_suggestions(db, run, batch, report, auto_apply=auto)
    db.commit()
    return rows[0]


def test_automatic_create_keeps_source_facts_and_undo_is_durable(db, admin_client):
    classification = catalogue(admin_client)
    products = source_products(db)
    original = [(p.name, p.category_name, p.price, p.unit) for p in products]
    row = propose(db, products, auto=True)
    assert row.status == 'applied'
    application = db.scalar(select(AIApplication))
    canonical = db.get(CanonicalProduct, row.group_id)
    assert canonical.category_id == classification['id'] and canonical.match_mode == 'similar'
    assert application.mode == 'auto' and application.before['created_group'] is True
    assert all(p.canonical_id == canonical.id and p.mapping_origin == 'ai' for p in products)
    assert original == [(p.name, p.category_name, p.price, p.unit) for p in products]
    suggestions.undo_application(db, application.id, 1)
    db.commit()
    assert application.status == 'undone' and all(p.canonical_id is None for p in products)
    assert db.get(CanonicalProduct, canonical.id) is None
    assert row.status == 'rejected'
    duplicate = propose(db, products, auto=True)
    assert duplicate.id == row.id and duplicate.status == 'rejected'
    assert db.scalar(select(func.count()).select_from(AIApplication)) == 1


def test_auto_appends_only_untouched_members_and_preserves_manual_links(db, admin_client):
    classification = catalogue(admin_client)
    products = source_products(db)
    target = group(admin_client, name='抖音视频点赞', category_id=classification['id'], match_mode='similar',
                   product_ids=[products[0].id])
    db.expire_all()
    row = propose(db, [products[1]], auto=True)
    assert row.status == 'applied'
    assert products[0].mapping_origin == 'manual'
    assert products[0].canonical_id == products[1].canonical_id == target['id']
    manual_again = propose(db, [products[0]], auto=True)
    assert manual_again.status == 'stale'
    assert db.scalar(select(func.count()).select_from(AIApplication)) == 1


@pytest.mark.parametrize('names', [
    ('抖音视频点赞', '快手视频点赞'),
    ('抖音视频点赞', '抖音直播点赞'),
    ('抖音视频点赞', '抖音视频评论'),
    ('抖音视频点赞', '抖音视频点赞评论套餐'),
    ('抖音点赞', 'DY点赞'),
])
def test_ambiguous_or_conflicting_semantics_never_auto_apply(db, admin_client, names):
    catalogue(admin_client)
    products = source_products(db, names)
    # No source category may manufacture a missing video/live distinction.
    if names[-1] == 'DY点赞':
        for product in products:
            product.category_name = '综合互动'
        db.commit()
    row = propose(db, products, auto=True)
    assert row.status == 'pending' and row.blocked_reason
    assert all(p.canonical_id is None for p in products)


def test_single_platform_new_group_is_pending_and_existing_exact_requires_review(db, admin_client):
    classification = catalogue(admin_client)
    products = source_products(db)
    row = propose(db, products[:1], auto=True)
    assert row.status == 'pending' and '两个' in row.blocked_reason
    target = group(admin_client, name='抖音视频点赞', category_id=classification['id'], match_mode='exact')
    row = propose(db, products, auto=True)
    assert row.status == 'pending' and '同规格' in row.blocked_reason
    suggestions.apply_suggestion(db, row.id, 1, row.revision)
    db.commit()
    assert row.status == 'applied' and all(p.canonical_id == target['id'] for p in products)


def test_manual_exact_rejects_mixed_units_and_quantities(db, admin_client):
    classification = catalogue(admin_client)
    products = source_products(db)
    group(admin_client, name='抖音视频点赞', category_id=classification['id'], match_mode='exact')
    products[1].display_quantity = Decimal('1')
    db.commit()
    row = propose(db, products)
    with pytest.raises(HTTPException, match='计价数量'):
        suggestions.apply_suggestion(db, row.id, 1, row.revision)
    assert all(p.canonical_id is None for p in products)


@pytest.mark.parametrize('change', ['price', 'association_revision', 'stale', 'closed'])
def test_content_freshness_and_manual_round_trip_are_rechecked(db, admin_client, change):
    catalogue(admin_client)
    products = source_products(db)
    row = propose(db, products)
    if change == 'price': products[0].price += 1
    elif change == 'association_revision': products[0].mapping_revision += 2
    elif change == 'stale': products[0].last_observed_at = utcnow() - timedelta(days=1)
    else: products[0].status = 'closed'
    db.commit()
    suggestions.apply_suggestion(db, row.id, 1, row.revision)
    db.commit()
    assert row.status == 'stale' and row.blocked_reason
    assert all(p.canonical_id is None for p in products)


def test_edit_accept_reject_and_revision_conflict(db, admin_client):
    classification = catalogue(admin_client)
    products = source_products(db)
    row = propose(db, products, path='不存在 / 分类')
    assert row.category_id is None
    edited = suggestions.edit_suggestion(db, row.id, 1, {'revision': row.revision,
        'category_id': classification['id'], 'group_id': None, 'group_name': '人工确认抖音视频点赞'})
    db.commit()
    with pytest.raises(HTTPException):
        suggestions.apply_suggestion(db, edited.id, 1, edited.revision - 1)
    suggestions.apply_suggestion(db, edited.id, 1, edited.revision)
    db.commit()
    assert edited.status == 'applied'
    products = source_products(db, ('抖音视频点赞新商品', 'DY视频赞新商品'))
    proposal = propose(db, products)
    suggestions.reject_suggestion(db, proposal.id, 1, proposal.revision)
    db.commit()
    assert proposal.status == 'rejected'


def test_rejected_alias_equivalent_proposal_is_not_reapplied(db, admin_client):
    catalogue(admin_client, '哔哩哔哩')
    products = source_products(db, ('B站视频点赞', 'BL视频赞'))
    row = propose(db, products, path='哔哩哔哩 / 视频', name='B站视频点赞')
    suggestions.reject_suggestion(db, row.id, 1, row.revision)
    db.commit()
    duplicate = propose(db, products, auto=True, path='哔哩哔哩 / 视频', name='哔哩哔哩视频点赞')
    assert duplicate.id == row.id and duplicate.status == 'rejected'
    assert all(p.canonical_id is None for p in products)


def test_undo_refuses_later_manual_changes_even_with_same_member_ids(db, admin_client):
    classification = catalogue(admin_client)
    products = source_products(db)
    row = propose(db, products, auto=True)
    application = db.scalar(select(AIApplication))
    target = db.get(CanonicalProduct, row.group_id)
    response = admin_client.put(f'/api/mappings/{target.id}', json={
        'name': target.name, 'category_id': classification['id'], 'match_mode': 'similar',
        'product_ids': [product.id for product in products]})
    assert response.status_code == 200
    db.expire_all()
    with pytest.raises(HTTPException, match='随后已被修改'):
        suggestions.undo_application(db, application.id, 1)
    assert all(p.canonical_id == target.id for p in products)


def test_automatic_failure_does_not_discard_other_batch_suggestions(db, admin_client, monkeypatch):
    catalogue(admin_client)
    products = source_products(db)
    run, batch = batch_for(db, products)
    original = suggestions.apply_suggestion
    def fail_first(session, identifier, *args, **kwargs):
        if session.get(AISuggestion, identifier).kind == 'category':
            raise HTTPException(409, '源商品已删除')
        return original(session, identifier, *args, **kwargs)
    monkeypatch.setattr(suggestions, 'apply_suggestion', fail_first)
    report = {'category_suggestions': [{'product_id': products[0].id,
                'category_path': '抖音 / 视频', 'group_name': '抖音视频点赞单项', 'reason': 'fixture'}],
              'group_suggestions': [{'product_ids': [p.id for p in products],
                'category_path': '抖音 / 视频', 'name': '抖音视频点赞', 'reason': 'fixture'}]}
    rows = suggestions.ingest_suggestions(db, run, batch, report, auto_apply=True)
    db.commit()
    assert len(rows) == 2 and rows[0].status == 'pending' and rows[1].status == 'applied'


def test_unknown_references_and_deleted_categories_never_create_catalogue(db, admin_client):
    classification = catalogue(admin_client)
    products = source_products(db)
    run, batch = batch_for(db, products)
    rows = suggestions.ingest_suggestions(db, run, batch, {'group_suggestions': [{
        'product_ids': [products[0].id, 999999], 'category_path': '抖音 / 视频',
        'name': '抖音视频点赞', 'reason': 'fixture'}]}, auto_apply=True)
    assert rows == []
    row = propose(db, products)
    assert admin_client.delete(f"/api/comparison-categories/{classification['id']}").status_code == 200
    db.expire_all()
    row = suggestions.apply_suggestion(db, row.id, 1, row.revision, automatic=True)
    db.commit()
    assert row.status == 'pending' and row.blocked_reason
    assert db.scalar(select(func.count()).select_from(CanonicalProduct)) == 0


def test_unknown_category_path_is_retained_in_review_evidence(db,admin_client):
    products=source_products(db)
    row=propose(db,products,path='抖音 / 视频 / 新互动服务')
    response=admin_client.get('/api/ai-analysis/suggestions').json()['items'][0]
    assert response['category_id'] is None
    assert response['proposed_category_path']==response['category_path']=='抖音 / 视频 / 新互动服务'
    assert row.status=='pending'


def test_automatic_target_checks_exact_and_alias_candidates_together(db, admin_client):
    classification = catalogue(admin_client, '哔哩哔哩')
    products = source_products(db, ('B站视频点赞', 'BL视频赞'))
    group(admin_client, name='B站视频点赞', category_id=classification['id'], match_mode='similar')
    group(admin_client, name='哔哩哔哩视频点赞', category_id=classification['id'], match_mode='similar')
    row = propose(db, products, auto=True, path='哔哩哔哩 / 视频', name='B站视频点赞')
    assert row.status == 'pending' and '多个' in row.blocked_reason
    assert all(product.canonical_id is None for product in products)
    assert db.scalar(select(func.count()).select_from(AIApplication)) == 0


@pytest.mark.parametrize('change', ['new_alias', 'renamed', 'features_changed', 'legacy_missing_snapshot'])
def test_automatic_pinned_target_is_rechecked_and_never_silently_replaced(db, admin_client, change):
    classification = catalogue(admin_client)
    products = source_products(db)
    target = group(admin_client, name='抖音视频点赞', category_id=classification['id'], match_mode='similar')
    row = propose(db, products)
    assert row.group_id == target['id']
    canonical = db.get(CanonicalProduct, target['id'])
    if change == 'new_alias':
        group(admin_client, name='DY视频赞', category_id=classification['id'], match_mode='similar')
    elif change == 'renamed':
        canonical.name = '抖音视频点赞已改名'
    elif change == 'features_changed':
        canonical.unit = '新规格'
    else:
        row.products = [{key: value for key, value in item.items() if key != 'target_group_snapshot'}
                        for item in row.products]
    db.commit()
    suggestions.apply_suggestion(db, row.id, None, row.revision, automatic=True)
    db.commit()
    assert row.status == 'pending' and row.blocked_reason
    assert row.group_id == target['id']
    assert all(product.canonical_id is None for product in products)
    assert db.scalar(select(func.count()).select_from(AIApplication)) == 0
