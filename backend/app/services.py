from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CollectionRun, Event, Observation, Platform, Product, utcnow


def iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def decimal_text(value):
    return format(value, 'f') if value is not None else None


def display_price(price: Decimal, quantity: Decimal):
    return price * quantity if quantity > 1 else price


def price_quality_valid(quality):
    # An undeclared unit blocks normalization/comparison, but the item's own
    # unchanged price basis remains a useful exact source-price time series.
    return not any(q in {'invalid_price', 'invalid_display_quantity'} for q in quality)


def product_dict(product: Product, platform: Platform | None = None):
    last = product.last_observed_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    stale_after = max((platform.interval_seconds if platform else 3600) * 2, 600)
    return {
        'id': product.id, 'platform_id': product.platform_id,
        'platform_name': platform.name if platform else '',
        'account_label': product.account_label, 'source_id': product.source_id,
        'name': product.name, 'category_id': product.category_id, 'category_name': product.category_name,
        'price': decimal_text(product.price),
        'display_price': decimal_text(display_price(product.price, product.display_quantity)),
        'display_quantity': decimal_text(product.display_quantity), 'unit': product.unit,
        'currency': product.currency, 'status': product.status,
        'stock': decimal_text(product.stock), 'stock_unlimited': product.stock_unlimited,
        'quality': product.quality, 'last_observed_at': iso(product.last_observed_at),
        'interval_seconds': platform.interval_seconds if platform else 3600,
        'freshness': 'stale' if utcnow() - last > timedelta(seconds=stale_after) else 'fresh',
        'canonical_id': product.canonical_id,
    }


def event_dict(event: Event):
    return {key: (iso(value) if isinstance(value, datetime) else value) for key, value in {
        'id': event.id, 'kind': event.kind, 'severity': event.severity,
        'title': event.title, 'message': event.message, 'product_id': event.product_id,
        'platform_id': event.platform_id, 'created_at': event.created_at,
        'acknowledged': event.acknowledged,
    }.items()}


def run_dict(run: CollectionRun, platform: Platform | None = None):
    return {
        'id': run.id, 'platform_id': run.platform_id,
        'platform_name': platform.name if platform else '',
        'scheduled_at': iso(run.scheduled_at), 'started_at': iso(run.started_at),
        'completed_at': iso(run.completed_at), 'status': run.status,
        'products_seen': run.products_seen, 'expected_total': run.expected_total,
        'error_count': run.error_count, 'error': run.error, 'attempts': run.attempts,
    }


def add_event(db: Session, *, key: str, kind: str, title: str, message: str,
              platform_id: int | None = None, product_id: int | None = None,
              run_id: int | None = None, severity: str = 'info'):
    if db.scalar(select(Event.id).where(Event.dedupe_key == key)) is None:
        db.add(Event(dedupe_key=key, kind=kind, title=title[:200], message=message,
                     platform_id=platform_id, product_id=product_id, run_id=run_id, severity=severity))


def json_safe(value):
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def persist_page(db: Session, run: CollectionRun, sources: list, *, observed_at=None) -> int:
    """Idempotent per-batch observations; older batches never replace newer quotes."""
    when = observed_at or utcnow()
    written = 0
    for source in sources:
        product = db.scalar(select(Product).where(
            Product.platform_id == run.platform_id, Product.account_label == run.account_label,
            Product.source_id == source.source_id,
        ).with_for_update())
        is_new = product is None
        if is_new:
            product = Product(platform_id=run.platform_id, account_label=run.account_label,
                              source_id=source.source_id, name=source.name, price=source.price,
                              last_run_id=run.id, last_scheduled_at=run.scheduled_at)
            db.add(product)
            db.flush()
        if db.scalar(select(Observation.id).where(Observation.run_id == run.id, Observation.product_id == product.id)):
            continue
        currency = str(source.raw.get('currency') or 'CNY')
        db.add(Observation(product_id=product.id, run_id=run.id, rule_id=run.rule_id,
                           scheduled_at=run.scheduled_at, observed_at=when, name=source.name,
                           price=source.price, display_quantity=source.display_quantity,
                           unit=source.unit, currency=currency, status=source.status,
                           stock=source.stock, stock_unlimited=source.stock_unlimited,
                           quality=source.quality, raw=json_safe(source.raw)))
        written += 1
        if product.last_scheduled_at <= run.scheduled_at:
            prefix = f'run:{run.id}:product:{product.id}'
            if not is_new:
                if product.name != source.name:
                    add_event(db, key=prefix+':rename', kind='renamed', title='商品名称变更',
                              message=f'{product.name} → {source.name}', platform_id=run.platform_id,
                              product_id=product.id, run_id=run.id)
                if product.status != source.status:
                    add_event(db, key=prefix+':status', kind='closed' if source.status == 'closed' else 'resumed',
                              title='商品停售' if source.status == 'closed' else '商品恢复销售',
                              message=source.name, platform_id=run.platform_id, product_id=product.id, run_id=run.id)
                same_basis = product.unit == source.unit and product.currency == currency and product.display_quantity == source.display_quantity
                if same_basis and product.status == source.status == 'active' and product.price != source.price and price_quality_valid(source.quality):
                    add_event(db, key=prefix+':price', kind='price_changed', title='商品价格变动',
                              message=f'{source.name}：{decimal_text(product.price)} → {decimal_text(source.price)} {currency}',
                              platform_id=run.platform_id, product_id=product.id, run_id=run.id)
                if not same_basis:
                    add_event(db, key=prefix+':basis', kind='basis_changed', title='商品计价条件变化',
                              message=source.name, platform_id=run.platform_id, product_id=product.id, run_id=run.id,
                              severity='warning')
            product.name, product.category_id, product.category_name = source.name, source.category_id, source.category_name
            product.price, product.display_quantity, product.unit = source.price, source.display_quantity, source.unit
            product.currency, product.status = currency, source.status
            product.stock, product.stock_unlimited, product.quality = source.stock, source.stock_unlimited, source.quality
            product.last_observed_at, product.last_scheduled_at, product.last_run_id = when, run.scheduled_at, run.id
    db.flush()
    return written
