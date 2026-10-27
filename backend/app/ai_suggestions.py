"""Guarded AI catalogue proposals, append-only application evidence and undo.

The caller owns the transaction. This module never changes source facts and
never commits: successful decisions and their audit rows are persisted together.
"""
from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from fastapi import HTTPException
from sqlalchemy import select, text

from .category_tree import CategoryTree
from .models import (AIApplication, AISuggestion, AuditLog, CanonicalProduct,
                     ComparisonCategory, Platform, Product, User, utcnow)
from .product_matching import ALIASES, alias_pattern, normalize
from .services import decimal_text, iso, price_quality_valid


BUSINESSES = frozenset(('哔哩哔哩', '抖音', '快手', '小红书', '视频号', 'TikTok'))
ACTIONS = frozenset(('点赞', '播放', '粉丝', '评论', '收藏', '分享'))
SCENES = frozenset(('视频', '直播'))
WRITABLE_FIELDS = frozenset(('revision', 'category_id', 'group_id', 'group_name'))


def comparison_lock(db):
    db.execute(text('SELECT pg_advisory_xact_lock(71960209)'))


def semantic_signature(value):
    """Use known lexical evidence only; never trust a model's confidence score."""
    value = normalize(value)
    spans, labels = [], set()
    options = sorted(((alias, label) for label, aliases in ALIASES.items()
                      for alias in aliases), key=lambda item: len(item[0]), reverse=True)
    for alias, label in options:
        for match in re.finditer(alias_pattern(alias), value):
            if any(left < match.end() and match.start() < right for left, right in spans):
                continue
            spans.append(match.span())
            labels.add(label)
    values = [labels & group for group in (BUSINESSES, ACTIONS, SCENES)]
    if any(len(group) != 1 for group in values):
        return None
    return tuple(next(iter(group)) for group in values)


def _fingerprint(product):
    from .ai_selection import product_fingerprint
    return product_fingerprint(product)


def _path(tree, category_id):
    return ' / '.join(row.name for row in tree.path(category_id))


def _resolve_category(tree, path):
    if not isinstance(path, str):
        return None
    parts = [normalize(part) for part in re.split(r'\s*(?:/|>|＞)\s*', path) if part.strip()]
    if not parts:
        return None
    found = [identifier for identifier in tree.ordered_ids()
             if [normalize(row.name) for row in tree.path(identifier)] == parts]
    return found[0] if len(found) == 1 else None


def _dedupe(category_path, group_name, products):
    signature = semantic_signature(category_path + ' ' + group_name)
    payload = {'category': normalize(category_path), 'name': signature or normalize(group_name),
               'products': sorted((int(item['id']), item.get('fingerprint', '')) for item in products)}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _current_products(db, snapshots):
    ids = [item.get('id') for item in snapshots]
    if not ids or any(type(identifier) is not int or identifier <= 0 for identifier in ids) or len(set(ids)) != len(ids):
        raise HTTPException(409, '建议中的商品编号无效，请重新分析')
    rows = list(db.scalars(select(Product).where(Product.id.in_(ids)).order_by(Product.id)
                          .with_for_update().execution_options(populate_existing=True)))
    if len(rows) != len(ids):
        raise HTTPException(409, '建议中的商品已不存在，请重新分析')
    return rows


def _is_current(product, snapshot, platform, now):
    if not snapshot.get('fingerprint') or snapshot['fingerprint'] != _fingerprint(product):
        return '商品内容已变化，请重新分析后处理'
    if snapshot.get('canonical_id') != product.canonical_id:
        return '商品关联已变化，请刷新后重新核对'
    if snapshot.get('mapping_revision', 0) != (product.mapping_revision or 0):
        return '商品关联经过人工调整，请重新分析后处理'
    if product.canonical_id is not None:
        return '商品已有关联，现有人工关联保持不变'
    if not platform or not platform.enabled:
        return '来源平台已停用'
    if product.status != 'active' or not price_quality_valid(product.quality or []):
        return '商品不在售或价格、计价数量无效'
    try:
        price, quantity = Decimal(product.price), Decimal(product.display_quantity)
        if not price.is_finite() or price < 0 or not quantity.is_finite() or quantity <= 0:
            return '商品价格或计价数量无效'
    except (TypeError, InvalidOperation):
        return '商品价格或计价数量无效'
    observed = product.last_observed_at
    if observed is None:
        return '商品没有有效采集时间'
    observed = observed.replace(tzinfo=timezone.utc) if observed.tzinfo is None else observed
    if not -300 <= (now - observed).total_seconds() <= max(7200, platform.interval_seconds * 2):
        return '商品数据已过期，请等待正常采集后重新分析'
    return ''


def _assert_revision(row, revision):
    if type(revision) is not int or revision != row.revision:
        raise HTTPException(409, '建议已被其他操作修改，请刷新后重试')


def _load_suggestion(db, identifier):
    row = db.scalar(select(AISuggestion).where(AISuggestion.id == identifier).with_for_update())
    if row is None:
        raise HTTPException(404, 'AI 建议不存在')
    return row


def _target_identity(group):
    return {'id': group.id, 'name': group.name, 'category_id': group.category_id,
            'unit': group.unit, 'currency': group.currency, 'match_mode': group.match_mode,
            'description': group.description}


def _target_group(db, row, tree, *, automatic):
    if row.category_id not in tree.by_id:
        return None, '建议类目不存在，请先选择已建立的类目'
    if not isinstance(row.group_name, str) or not row.group_name.strip() or len(row.group_name) > 200:
        return None, '统一商品名称无效'
    groups = list(db.scalars(select(CanonicalProduct).where(CanonicalProduct.category_id == row.category_id)
                             .order_by(CanonicalProduct.id).with_for_update().execution_options(populate_existing=True)))
    pinned = None
    if row.group_id is not None:
        pinned = next((item for item in groups if item.id == row.group_id), None)
        if pinned is None:
            return None, '目标统一商品或其类目已变化，请重新选择'
        if not automatic:
            return pinned, ''
    same_name = [group for group in groups if normalize(group.name) == normalize(row.group_name)]
    signature = semantic_signature(_path(tree, row.category_id) + ' ' + row.group_name)
    same_semantics = [group for group in groups if signature and
                      semantic_signature(_path(tree, group.category_id) + ' ' + group.name) == signature]
    # Exact spelling must not hide a second valid alias/semantic candidate.
    candidates = list({group.id: group for group in same_name + (same_semantics if automatic else [])}.values())
    if automatic and pinned is not None:
        identities = [item.get('target_group_snapshot') for item in row.products or []]
        expected = _target_identity(pinned)
        if not identities or any(identity != expected for identity in identities):
            return None, '目标统一商品缺少原始快照或名称、特征已变化，请人工核对'
        if not candidates or all(item.id != pinned.id for item in candidates):
            return None, '原目标业务特征已变化，请人工重新选择，未切换到其他目标'
    if len(candidates) == 1:
        return candidates[0], ''
    if len(candidates) > 1:
        return None, '已有多个可能的统一商品，请人工选择唯一目标'
    return None, ''


def _automatic_reason(db, row, products, group, tree):
    target_signature = semantic_signature(_path(tree, row.category_id) + ' ' + row.group_name)
    if target_signature is None:
        return '业务平台、服务动作或视频／直播场景不明确，需人工审核'
    if any(semantic_signature(product.name + ' ' + product.category_name) != target_signature for product in products):
        return '商品业务、服务动作或场景与目标不一致，需人工审核'
    if group:
        if group.match_mode != 'similar':
            return '同规格统一商品需要人工核对单位、币种和规格'
        if semantic_signature(_path(tree, group.category_id) + ' ' + group.name) != target_signature:
            return '已有统一商品的业务特征不一致，需人工审核'
    elif len({product.platform_id for product in products}) < 2:
        return '自动新建统一商品至少需要两个不同交易平台的商品'
    return ''


def _group_evidence(db, group):
    return {'id': group.id, 'name': group.name, 'category_id': group.category_id,
            'unit': group.unit, 'currency': group.currency, 'match_mode': group.match_mode,
            'description': group.description, 'mapping_revision': group.mapping_revision or 0,
            'product_ids': list(db.scalars(select(Product.id).where(Product.canonical_id == group.id).order_by(Product.id)))}


def _product_evidence(product):
    return {'id': product.id, 'canonical_id': product.canonical_id,
            'mapping_revision': product.mapping_revision or 0,
            'mapping_origin': product.mapping_origin or 'manual',
            'ai_application_id': product.ai_application_id}


def ingest_suggestions(db, run, batch, report, auto_apply=False):
    comparison_lock(db)
    tree = CategoryTree.load(db)
    snapshots = {item['id']: item for item in batch.snapshot.get('products', [])}
    proposals = [('category', [item['product_id']], item['category_path'], item['group_name'], item['reason'])
                 for item in report.get('category_suggestions', [])]
    proposals += [('group', item['product_ids'], item['category_path'], item['name'], item['reason'])
                  for item in report.get('group_suggestions', [])]
    result = []
    for kind, ids, path, name, reason in proposals:
        # The transport already validates references. Recheck here so direct
        # callers cannot bypass the snapshot boundary or submit duplicate IDs.
        if not ids or len(ids) != len(set(ids)) or any(type(i) is not int or i not in snapshots for i in ids):
            continue
        selected = [dict(snapshots[identifier]) for identifier in sorted(ids)]
        category_id = _resolve_category(tree, path)
        key = _dedupe(_path(tree, category_id) if category_id else path, name, selected)
        previous = db.scalar(select(AISuggestion).where(AISuggestion.dedupe_key == key)
                             .order_by(AISuggestion.id.desc()).limit(1))
        if previous is not None:
            result.append(previous)
            continue
        row = AISuggestion(run_id=run.id, batch_id=batch.id, kind=kind, status='pending',
            category_id=category_id, group_id=None, group_name=str(name).strip()[:200],
            proposed_category_path=str(path)[:500],
            reason=str(reason)[:1500], blocked_reason='', revision=1,
            products=selected, dedupe_key=key, created_at=utcnow(), updated_at=utcnow())
        db.add(row)
        db.flush()
        group, target_reason = _target_group(db, row, tree, automatic=True)
        if group:
            row.group_id = group.id
            row.products = [{**item, 'target_group_snapshot': _target_identity(group)} for item in selected]
        row.blocked_reason = target_reason
        if auto_apply:
            try:
                with db.begin_nested():
                    apply_suggestion(db, row.id, None, row.revision, automatic=True)
            except HTTPException:
                # A stale/missing source row must not roll back unrelated
                # proposals or the successful AI batch and its usage record.
                row.blocked_reason = '商品或目标状态已变化，需管理员重新核对'
        elif not row.blocked_reason:
            row.blocked_reason = '自动应用未启用，等待管理员确认'
        result.append(row)
    db.flush()
    return result


def apply_suggestion(db, suggestion_id, actor_id, revision, automatic=False):
    comparison_lock(db)
    row = _load_suggestion(db, suggestion_id)
    _assert_revision(row, revision)
    if row.status == 'applied':
        return row
    if row.status != 'pending':
        raise HTTPException(409, '此建议已处理，请刷新后重试')
    tree = CategoryTree.load(db)
    products = _current_products(db, row.products)
    snapshots = {item['id']: item for item in row.products}
    platforms = {item.id: item for item in db.scalars(select(Platform).where(
        Platform.id.in_({product.platform_id for product in products})))}
    reason = next((message for product in products
                   if (message := _is_current(product, snapshots[product.id], platforms.get(product.platform_id), utcnow()))), '')
    if reason:
        row.status, row.blocked_reason = 'stale', reason
        row.revision += 1
        row.updated_at = utcnow()
        db.flush()
        return row
    group, reason = _target_group(db, row, tree, automatic=automatic)
    if not reason and automatic:
        reason = _automatic_reason(db, row, products, group, tree)
    if not reason and group and group.match_mode == 'exact':
        if any(not product.unit or product.unit != group.unit or product.currency != group.currency for product in products):
            reason = '同规格关联的单位和币种必须明确且一致'
        quantities = {product.display_quantity for product in products}
        existing_quantities = set(db.scalars(select(Product.display_quantity).where(Product.canonical_id == group.id)))
        if len(quantities | existing_quantities) != 1:
            reason = '同规格关联的计价数量必须一致'
    if not reason and group:
        member_ids = list(db.scalars(select(Product.id).where(Product.canonical_id == group.id)))
        if len(member_ids) + len(products) > 5000:
            reason = '统一商品关联数量超过 5000 件，请拆分后处理'
    if reason:
        row.blocked_reason = reason
        row.updated_at = utcnow()
        if not automatic:
            raise HTTPException(409, reason)
        db.flush()
        return row
    # Manually choosing an existing same-name item is safe; creating another
    # identically named group in this category is never necessary.
    created = group is None
    before = {'created_group': created, 'group': _group_evidence(db, group) if group else None,
              'products': [_product_evidence(product) for product in products]}
    if group is None:
        currencies = {product.currency for product in products}
        group = CanonicalProduct(name=row.group_name.strip(), category_id=row.category_id,
            unit='', currency=next(iter(currencies)) if len(currencies) == 1 else '',
            description='AI 建议经规则校验或管理员确认建立的同类／相似商品。', match_mode='similar', mapping_revision=0)
        db.add(group)
        db.flush()
    application = AIApplication(suggestion_id=row.id, mode='auto' if automatic else 'manual', status='applied',
        actor_id=actor_id, group_id=group.id, group_name=group.name, category_path=_path(tree, group.category_id),
        products=[], before=before, after={}, created_at=utcnow())
    db.add(application)
    db.flush()
    group.mapping_revision = (group.mapping_revision or 0) + 1
    for product in products:
        product.canonical_id = group.id
        product.mapping_revision = (product.mapping_revision or 0) + 1
        product.mapping_origin = 'ai'
        product.ai_application_id = application.id
    db.flush()
    evidence = []
    for product in products:
        item = dict(snapshots[product.id])
        item.update(name=product.name, platform_name=platforms[product.platform_id].name,
                    source_category=product.category_name, before_group_name='', after_group_name=group.name)
        evidence.append(item)
    application.products = evidence
    application.after = {'group': _group_evidence(db, group), 'products': [_product_evidence(product) for product in products]}
    row.group_id, row.status, row.blocked_reason = group.id, 'applied', ''
    row.revision += 1
    row.updated_at = utcnow()
    db.add(AuditLog(user_id=actor_id, action='ai_auto_apply' if automatic else 'ai_manual_apply', target=str(application.id)))
    db.flush()
    return row


def edit_suggestion(db, suggestion_id, actor_id=None, payload=None, **changes):
    payload = dict(payload or changes)
    if set(payload) - WRITABLE_FIELDS:
        raise HTTPException(422, '建议编辑字段无效')
    comparison_lock(db)
    row = _load_suggestion(db, suggestion_id)
    _assert_revision(row, payload.get('revision'))
    if row.status != 'pending':
        raise HTTPException(409, '只有待审核建议可以编辑')
    tree = CategoryTree.load(db)
    category_id = payload.get('category_id', row.category_id)
    group_id = payload.get('group_id', row.group_id)
    if type(category_id) is not int or category_id not in tree.by_id:
        raise HTTPException(422, '请选择已存在的比价类目')
    if group_id is not None and (type(group_id) is not int or group_id <= 0):
        raise HTTPException(422, '统一商品编号无效')
    name = payload.get('group_name', row.group_name)
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise HTTPException(422, '请填写有效的统一商品名称')
    if group_id is not None:
        group = db.get(CanonicalProduct, group_id)
        if group is None or group.category_id != category_id:
            raise HTTPException(422, '统一商品不属于所选类目')
        name = group.name
    row.category_id, row.group_id, row.group_name = category_id, group_id, name.strip()
    row.blocked_reason = '管理员已调整建议，等待确认应用'
    row.revision += 1
    row.updated_at = utcnow()
    # Keep the initial dedupe key: editing a pending item must not erase a
    # rejection/undo safeguard for the original model proposal.
    db.add(AuditLog(user_id=actor_id, action='edit_ai_suggestion', target=str(row.id)))
    db.flush()
    return row


def reject_suggestion(db, suggestion_id, actor_id, revision):
    comparison_lock(db)
    row = _load_suggestion(db, suggestion_id)
    _assert_revision(row, revision)
    if row.status not in ('pending', 'stale'):
        raise HTTPException(409, '此建议已处理，请刷新后重试')
    row.status, row.blocked_reason = 'rejected', '管理员已拒绝；商品内容未变化时不会再次自动应用'
    row.revision += 1
    row.updated_at = utcnow()
    db.add(AuditLog(user_id=actor_id, action='reject_ai_suggestion', target=str(row.id)))
    db.flush()
    return row


def undo_application(db, application_id, actor_id):
    comparison_lock(db)
    application = db.scalar(select(AIApplication).where(AIApplication.id == application_id).with_for_update())
    if application is None:
        raise HTTPException(404, 'AI 处理记录不存在')
    if application.status == 'undone':
        return application
    group = db.scalar(select(CanonicalProduct).where(CanonicalProduct.id == application.group_id).with_for_update())
    after = application.after or {}
    expected_group = after.get('group')
    if group is None or not expected_group or _group_evidence(db, group) != expected_group:
        raise HTTPException(409, '统一商品随后已被修改，撤销会覆盖新操作，请人工核对')
    product_ids = [item['id'] for item in after.get('products', [])]
    products = list(db.scalars(select(Product).where(Product.id.in_(product_ids)).order_by(Product.id).with_for_update()))
    expected_products = {item['id']: item for item in after.get('products', [])}
    if len(products) != len(product_ids) or any(_product_evidence(product) != expected_products[product.id] for product in products):
        raise HTTPException(409, '商品关联随后已被修改，撤销会覆盖新操作，请人工核对')
    previous = {item['id']: item for item in application.before['products']}
    for product in products:
        before = previous[product.id]
        product.canonical_id = before['canonical_id']
        product.mapping_origin = before.get('mapping_origin', 'manual')
        product.ai_application_id = before.get('ai_application_id')
        product.mapping_revision = (product.mapping_revision or 0) + 1
    db.flush()
    if application.before.get('created_group'):
        if db.scalar(select(Product.id).where(Product.canonical_id == group.id).limit(1)) is not None:
            raise HTTPException(409, '新建统一商品已有后续成员，不能自动撤销')
        db.delete(group)
    else:
        group.mapping_revision = (group.mapping_revision or 0) + 1
    application.status, application.undone_at = 'undone', utcnow()
    row = db.get(AISuggestion, application.suggestion_id)
    if row:
        row.status, row.blocked_reason = 'rejected', '此建议已撤销；商品内容未变化时不会再次自动应用'
        row.revision += 1
        row.updated_at = utcnow()
    db.add(AuditLog(user_id=actor_id, action='undo_ai_application', target=str(application.id)))
    db.flush()
    return application


def suggestion_dict(db, row):
    tree = CategoryTree.load(db)
    products = []
    for snapshot in row.products or []:
        item = {key: snapshot.get(key) for key in ('id', 'name', 'platform_id', 'platform_name',
                'source_category', 'price', 'unit', 'currency', 'display_quantity', 'current_group_name')}
        current = db.get(Product, snapshot.get('id'))
        if current:
            platform = db.get(Platform, current.platform_id)
            group = db.get(CanonicalProduct, current.canonical_id) if current.canonical_id else None
            item.update(name=current.name, platform_name=platform.name if platform else item['platform_name'],
                        current_group_name=group.name if group else '', before_group_name=snapshot.get('current_group_name', ''))
        products.append(item)
    return {'id': row.id, 'run_id': row.run_id, 'batch_id': row.batch_id, 'kind': row.kind,
            'status': row.status, 'category_id': row.category_id,
            'category_path': _path(tree, row.category_id) if row.category_id in tree.by_id else (row.proposed_category_path or ''),
            'proposed_category_path': row.proposed_category_path or '',
            'group_id': row.group_id, 'group_name': row.group_name, 'reason': row.reason,
            'blocked_reason': row.blocked_reason, 'revision': row.revision, 'products': products,
            'created_at': iso(row.created_at), 'updated_at': iso(row.updated_at)}


def application_dict(db, row):
    actor = db.get(User, row.actor_id) if row.actor_id else None
    return {'id': row.id, 'suggestion_id': row.suggestion_id, 'mode': row.mode,
            'status': row.status, 'actor_name': actor.username if actor else '系统规则',
            'group_id': row.group_id, 'group_name': row.group_name, 'category_path': row.category_path,
            'products': [{key: item.get(key) for key in ('id', 'name', 'platform_name', 'source_category',
                         'before_group_name', 'after_group_name','price','unit','currency','display_quantity')} for item in row.products or []],
            'before': row.before, 'after': row.after, 'created_at': iso(row.created_at),
            'undone_at': iso(row.undone_at)}
