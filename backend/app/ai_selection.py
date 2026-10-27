"""Fair, bounded AI selection and stable content fingerprints (no credentials)."""
import hashlib
import json
from collections import defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from .models import AIProductCheck, AIAnalysisBatch, Event, Platform, Product, utcnow
from .services import decimal_text


def product_fingerprint(product):
    def canonical_decimal(value):
        if value is None:
            return None
        fixed=format(Decimal(value),'f')
        fixed=fixed.rstrip('0').rstrip('.') if '.' in fixed else fixed
        return '0' if fixed in ('-0','') else fixed
    fields = {'name': product.name, 'source_category': product.category_name,
              'price': canonical_decimal(product.price), 'unit': product.unit, 'currency': product.currency,
              'display_quantity': canonical_decimal(product.display_quantity), 'status': product.status,
              'quality': sorted(product.quality or []), 'platform_id': product.platform_id}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def fair_take(products, amount):
    queues = defaultdict(deque)
    for product in products:
        queues[product.platform_id].append(product)
    selected = []
    while queues and len(selected) < amount:
        for platform_id in list(queues):
            selected.append(queues[platform_id].popleft())
            if not queues[platform_id]:
                del queues[platform_id]
            if len(selected) == amount:
                break
    return selected


def select_products(db, limit, start, end, retry_ids=None):
    rows = list(db.scalars(select(Product).join(Platform).where(Platform.enabled.is_(True))))
    if retry_ids is not None:
        allowed = set(retry_ids)
        rows = [p for p in rows if p.id in allowed]
    else:
        from .ai_jobs import day_key
        sent = {i for batch in db.scalars(select(AIAnalysisBatch).where(
            AIAnalysisBatch.request_day == day_key(utcnow()))) for i in batch.product_ids}
        rows = [p for p in rows if p.id not in sent]
    checked = {row.product_id: row for row in db.scalars(select(AIProductCheck))}
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda p: (checked[p.id].last_checked_at if p.id in checked else minimum, p.id))
    changed = set(db.scalars(select(Event.product_id).where(Event.created_at >= start,
        Event.created_at < end, Event.kind.in_(['price_changed', 'basis_changed']))))
    priority = [p for p in rows if p.id in changed or p.created_at >= start or p.quality or
                (p.id in checked and product_fingerprint(p) != checked[p.id].fingerprint)]
    first = fair_take(priority, int(limit * .7))
    ids = {p.id for p in first}
    # Remaining at least 30% always follows least-recently-checked order, regardless of anomaly volume.
    rest = fair_take([p for p in rows if p.id not in ids], limit-len(first))
    return [p.id for p in first+rest]
