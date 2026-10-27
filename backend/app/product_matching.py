"""Explainable keyword suggestions. This module never changes product links."""
from __future__ import annotations

import re
import hashlib
import json
import unicodedata

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, func, literal_column, or_, select
from sqlalchemy.orm import Session

from .db import get_db
from .models import CanonicalProduct, Platform, Product
from .security import admin_user
from .services import product_dict
from .read_snapshot import read_snapshot

router = APIRouter()

# Business names describe the source service, not the trade platform. TikTok
# deliberately remains distinct from Douyin; comments remain distinct from likes.
ALIASES = {
    '哔哩哔哩': ('哔哩哔哩', 'bilibili', 'bili', 'b站', '哔哩', 'bl'),
    '抖音': ('抖音', 'douyin', 'dy', 'd音'),
    '快手': ('快手', 'kuaishou', 'ks'),
    '小红书': ('小红书', 'xiaohongshu', 'xhs', '红薯'),
    '视频号': ('视频号', 'sph'),
    'TikTok': ('tiktok', 'tik tok', 'tt国际版'),
    '点赞': ('点赞', '点讚', '爱心', '拇指', '赞', '讚', 'likes', 'like'),
    '播放': ('播放量', '播放', '观看量', 'views', 'view'),
    '粉丝': ('粉丝', '粉絲', '关注', 'followers', 'follower'),
    '评论': ('评论', '評論', 'comments', 'comment'),
    '收藏': ('收藏', 'favorites', 'favorite'),
    '分享': ('分享', '转发', '轉發', 'shares', 'share'),
    '直播': ('直播', 'live'),
    '视频': ('视频', '視頻', 'video'),
}
WEAK_REMAINDERS = frozenset(('业务', '商品', '服务', '平台'))


def normalize(value):
    value = unicodedata.normalize('NFKC', value).lower()
    return re.sub(r'[^a-z0-9\u3400-\u9fff]+', ' ', value).strip()


def alias_pattern(value):
    value = normalize(value)
    pattern = re.escape(value).replace(r'\ ', r'\s*')
    # Avoid BL matching blue and DY matching study. A Latin abbreviation next
    # to Chinese text or punctuation is still valid (e.g. BL-点赞, DY真人赞).
    if value and value[0].isascii() and value[0].isalnum():
        pattern = r'(?<![a-z0-9])' + pattern
    if value and value[-1].isascii() and value[-1].isalnum():
        pattern += r'(?![a-z0-9])'
    return pattern


def parse_keywords(value):
    source = normalize(value)
    found, spans = [], []
    options = sorted(((alias, label) for label, values in ALIASES.items() for alias in values),
                     key=lambda item: len(item[0]), reverse=True)
    for alias, label in options:
        for match in re.finditer(alias_pattern(alias), source):
            if any(start < match.end() and match.start() < end for start, end in spans):
                continue
            spans.append(match.span())
            if label not in found:
                found.append(label)
    remaining = list(source)
    for start, end in spans:
        remaining[start:end] = ' ' * (end-start)
    result = [{'label': label, 'variants': list(ALIASES[label])} for label in found]
    for part in ''.join(remaining).split():
        if part not in WEAK_REMAINDERS and part not in [g['label'] for g in result]:
            result.append({'label': part, 'variants': [part]})
    if len(result) > 8:
        raise HTTPException(422, '关键词过多，请缩减为最多 8 个关键词')
    return result


def pattern_for(group):
    return '(?:' + '|'.join(alias_pattern(alias) for alias in group['variants']) + ')'


def sql_normalized(column):
    # PostgreSQL normalize handles full-width Latin names consistently with the
    # Python tokenizer. Only fixed SQL syntax is used; user text is bound data.
    return func.regexp_replace(func.lower(func.normalize(column, literal_column('NFKC'))),
                               '[^a-z0-9㐀-鿿]+', ' ', 'g')


@router.get('/api/product-suggestions')
def product_suggestions(q: str = Query(..., min_length=1, max_length=160),
                        exclude: str = Query('', max_length=160), mode: str = Query('all', pattern='^(all|any)$'),
                        platform_id: int | None = Query(None, gt=0), category: str = Query('', max_length=500),
                        active_only: bool = True, mapping_id: int | None = Query(None, gt=0),
                        page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100),
                        db: Session = Depends(get_db), user=Depends(admin_user)):
    with read_snapshot(db) as snapshot:
        return suggestion_result(q=q, exclude=exclude, mode=mode, platform_id=platform_id,
            category=category, active_only=active_only, mapping_id=mapping_id,
            page=page, page_size=page_size, db=snapshot)


def suggestion_result(*, q, exclude, mode, platform_id, category, active_only, mapping_id,
                      page, page_size, db):
    groups = parse_keywords(q)
    excluded = parse_keywords(exclude)
    if not groups:
        raise HTTPException(422, '请输入业务和商品关键词，例如“B站 点赞”')
    if mapping_id is not None and db.get(CanonicalProduct, mapping_id) is None:
        raise HTTPException(404, '统一商品不存在')
    name, classification = sql_normalized(Product.name), sql_normalized(Product.category_name)
    conditions, terms = [], []
    for group in groups:
        pattern = pattern_for(group)
        in_name, in_category = name.op('~')(pattern), classification.op('~')(pattern)
        conditions.append(or_(in_name, in_category))
        terms.append(case((in_name, 20), (in_category, 10), else_=0))
    # Structured keywords are combined; widening the mode remains an explicit
    # user choice instead of silently mixing unrelated business platforms.
    predicate = and_(*conditions) if mode == 'all' else or_(*conditions)
    query = select(Product).where(predicate)
    if platform_id is not None:
        query = query.where(Product.platform_id == platform_id)
    if category:
        query = query.where(Product.category_name == category)
    if active_only:
        query = query.where(Product.status == 'active')
    for group in excluded:
        pattern = pattern_for(group)
        query = query.where(~or_(name.op('~')(pattern), classification.op('~')(pattern)))
    score = sum(terms) + case((Product.status == 'active', 2), else_=0)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    counts = list(db.execute(query.with_only_columns(Product.platform_id, func.count())
                            .group_by(Product.platform_id)))
    revision = hashlib.sha256()
    # A constant total does not mean a constant candidate set: names, prices,
    # ownership or category can change between pages. Bind all pages to the
    # same metadata revision without exposing or persisting any raw source data.
    metadata = query.with_only_columns(Product.id, Product.name, Product.category_name,
        Product.price, Product.currency, Product.unit, Product.display_quantity,
        Product.status, Product.quality, Product.canonical_id, Product.last_observed_at)
    for row in db.execute(metadata.order_by(Product.id)).yield_per(500):
        revision.update(json.dumps(list(row), ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8'))
        revision.update(b'\n')
    rows = list(db.execute(query.add_columns(score.label('match_score'))
                          .order_by(score.desc(), Product.platform_id, Product.id)
                          .offset((page-1)*page_size).limit(page_size)))
    platforms = {p.id: p for p in db.scalars(select(Platform).where(
        Platform.id.in_({pid for pid, _ in counts})))}
    items = []
    for product, rank in rows:
        normalized_name, normalized_category = normalize(product.name), normalize(product.category_name)
        reasons, warnings, labels = [], [], set()
        for group in groups:
            regex = pattern_for(group)
            if re.search(regex, normalized_name):
                reasons.append(f"名称匹配：{group['label']}")
                labels.add(group['label'])
            elif re.search(regex, normalized_category):
                reasons.append(f"源类目匹配：{group['label']}")
                labels.add(group['label'])
                warnings.append(f"“{group['label']}”仅匹配源类目，请核对商品用途")
        if mode == 'any' and len(labels) < len(groups):
            warnings.append('仅匹配部分关键词')
        if any(g['label'] == '点赞' for g in groups):
            if re.search(pattern_for({'variants': ALIASES['评论']}), normalized_name):
                warnings.append('名称含评论，可能是评论点赞')
            if any(term in normalized_name for term in ('三连', '三联', '套餐', '点赞加', '赞粉')):
                warnings.append('可能是组合套餐，请核对包含内容')
        if not product.unit.strip():
            warnings.append('计价单位待核验')
        item = product_dict(product, platforms.get(product.platform_id))
        item.update(match_score=int(rank), match_reasons=reasons, match_warnings=warnings,
                    association_conflict=bool(product.canonical_id and product.canonical_id != mapping_id))
        items.append(item)
    return {'items': items, 'total': total, 'page': page, 'page_size': page_size,
            'result_token': revision.hexdigest(),
            'keywords': groups, 'mode': mode,
            'platform_counts': [{'platform_id': pid, 'name': platforms[pid].name, 'count': count}
                                for pid, count in sorted(counts)],
            'note': '按业务别名、商品名称和源类目进行关键词推荐；不等于规格一致，勾选并保存后才建立关联。'}
