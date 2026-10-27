"""Hourly price envelopes of currently linked and authorized source products."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import json

from fastapi import HTTPException
from sqlalchemy import func, select

from .access import platform_scope
from .models import CanonicalProduct, Observation, Platform, Product, utcnow
from .services import decimal_text, iso, price_quality_valid, product_dict
from .read_snapshot import read_snapshot


def basis_key(currency, unit, quantity):
    # Decimal.normalize() uses the active precision and can round long source
    # quantities. Text normalization must never change their numeric value.
    text = decimal_text(Decimal(quantity))
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    if text == '-0':
        text = '0'
    return json.dumps([currency, unit, text], ensure_ascii=False)


def price_trend(db, user, mapping_id: int, days: int):
    with read_snapshot(db) as snapshot:
        return snapshot_price_trend(snapshot, user, mapping_id, days)


def snapshot_price_trend(db, user, mapping_id: int, days: int):
    group = db.get(CanonicalProduct, mapping_id)
    products = list(db.scalars(select(Product).where(
        Product.canonical_id == mapping_id, platform_scope(user, Product.platform_id))))
    if group is None or (user.role != 'admin' and not products):
        raise HTTPException(404, '记录不存在')
    now = utcnow()
    start = (now - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
    end = now.replace(minute=0, second=0, microsecond=0)
    platforms = {p.id: p for p in db.scalars(select(Platform).where(
        Platform.id.in_({p.platform_id for p in products})))}
    result = {}

    def series(currency, unit, quantity):
        key = basis_key(currency, unit, quantity)
        if key not in result:
            result[key] = {'key': key, 'currency': currency, 'unit': unit,
                           'display_quantity': decimal_text(quantity), 'unit_requires_review': not bool(unit.strip()),
                           'current': {'minimum': None, 'maximum': None, 'offer_count': 0,
                                       'platform_count': 0, 'lowest_product_ids': [], 'highest_product_ids': []},
                           'items': [], '_hours': {}, '_current': []}
        return result[key]

    for product in products:
        offer = product_dict(product, platforms.get(product.platform_id))
        if (offer['status'] != 'active' or offer['freshness'] != 'fresh'
                or not price_quality_valid(product.quality) or 'invalid_unit' in product.quality
                or product.price < 0):
            continue
        series(product.currency, product.unit, product.display_quantity)['_current'].append(product)

    # Pick the last observation, including closed/invalid observations, before
    # filtering. Falling back to an earlier active row would invent a quote.
    hour = func.date_trunc('hour', Observation.observed_at)
    ranked = (select(Observation.product_id, Product.platform_id, hour.label('hour'),
                     Observation.price, Observation.currency, Observation.unit,
                     Observation.display_quantity, Observation.status, Observation.quality,
                     func.row_number().over(partition_by=(Observation.product_id, hour),
                         order_by=(Observation.observed_at.desc(), Observation.id.desc())).label('rank'))
              .join(Product, Product.id == Observation.product_id)
              .where(Product.canonical_id == mapping_id, platform_scope(user, Product.platform_id),
                     Observation.observed_at >= start, Observation.observed_at <= now).subquery())
    from sqlalchemy.dialects.postgresql import JSONB
    valid_quality = ~ranked.c.quality.cast(JSONB).contains(['invalid_price'])
    for flag in ('invalid_display_quantity', 'invalid_unit'):
        valid_quality &= ~ranked.c.quality.cast(JSONB).contains([flag])
    rows = db.execute(select(ranked.c.hour, ranked.c.currency, ranked.c.unit, ranked.c.display_quantity,
                             func.min(ranked.c.price), func.max(ranked.c.price), func.avg(ranked.c.price),
                             func.count(), func.count(func.distinct(ranked.c.platform_id)))
        .where(ranked.c.rank == 1, ranked.c.status == 'active', ranked.c.price >= 0, valid_quality)
        .group_by(ranked.c.hour, ranked.c.currency, ranked.c.unit, ranked.c.display_quantity)
        .order_by(ranked.c.hour))
    for at, currency, unit, quantity, minimum, maximum, average, count, platform_count in rows:
        series(currency, unit, quantity)['_hours'][at] = {
            'at': iso(at), 'minimum': decimal_text(minimum), 'maximum': decimal_text(maximum),
            'average': decimal_text(average), 'offer_count': count, 'platform_count': platform_count}
    for item in result.values():
        current = item.pop('_current')
        if current:
            low, high = min(p.price for p in current), max(p.price for p in current)
            item['current'] = {'minimum': decimal_text(low), 'maximum': decimal_text(high),
                'offer_count': len(current), 'platform_count': len({p.platform_id for p in current}),
                'lowest_product_ids': [p.id for p in current if p.price == low],
                'highest_product_ids': [p.id for p in current if p.price == high]}
        hours = item.pop('_hours')
        at = start
        while at <= end:
            item['items'].append(hours.get(at, {'at': iso(at), 'minimum': None, 'maximum': None,
                'average': None, 'offer_count': 0, 'platform_count': 0}))
            at += timedelta(hours=1)
    return {'days': days, 'interval_seconds': 3600, 'generated_at': iso(now),
            'membership_note': '按当前关联且已获授权的商品汇总历史观测，每小时每件商品采用最后一次观测；无有效采样的时段保留断点。',
            'basis_note': '按币种、源计价单位和展示数量分组，统计在售有效基础报价；同类或单位待核验报价仅作参考。',
            'groups': sorted(result.values(), key=lambda g: (g['currency'], g['unit'], Decimal(g['display_quantity'])))}
