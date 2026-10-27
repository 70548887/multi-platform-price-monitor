"""Recover missing Fans8 category labels from already saved source observations.

Defaults to a dry run. --apply creates a metadata backup and changes only empty
current Product.category_name fields. Prices, observation rows and batch status
are never altered, and this command sends no upstream requests.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from sqlalchemy import select, text
from app.collectors.fans8 import source_category_label
from app.db import SessionLocal
from app.models import AuditLog, CollectionRun, Observation, Platform, Product


def repair_categories(*, apply: bool = False) -> dict:
    with SessionLocal() as db:
        if apply:
            # Match dispatch_due/queue_run ordering. The no-active-run check
            # must remain true until metadata is committed: otherwise a new
            # collector could start between this check and the product locks.
            db.execute(text('SELECT pg_advisory_xact_lock(71960201)'))
            platform_ids = list(db.scalars(select(Platform.id).where(
                Platform.adapter == 'fans8').order_by(Platform.id)))
            locked_platform_ids = []
            for platform_id in platform_ids:
                db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': 800000 + platform_id})
                platform = db.scalar(select(Platform).where(Platform.id == platform_id).with_for_update())
                if platform is not None and platform.adapter == 'fans8':
                    locked_platform_ids.append(platform_id)
            if db.scalar(select(CollectionRun.id).where(
                    CollectionRun.platform_id.in_(locked_platform_ids),
                    CollectionRun.status.in_(['queued', 'running'])).limit(1)):
                raise RuntimeError('Fans8 collection is active; retry after it completes.')
        query = (select(Product, Observation.raw).join(Platform, Platform.id == Product.platform_id)
                 .join(Observation, (Observation.product_id == Product.id) &
                       (Observation.run_id == Product.last_run_id))
                 .where(Platform.adapter == 'fans8', Product.category_name == '').order_by(Product.id))
        if apply:
            query = query.with_for_update(of=Product)
        changes = [(product, source_category_label(raw)) for product, raw in db.execute(query)
                   if isinstance(raw, dict) and source_category_label(raw)]
        backup = None
        if apply and changes:
            directory = ROOT / '.runtime' / 'category-repair'
            directory.mkdir(parents=True, exist_ok=True)
            backup = directory / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
            payload = {'created_at': datetime.now(timezone.utc).isoformat(), 'items': [
                {'product_id': product.id, 'last_run_id': product.last_run_id,
                 'category_id': product.category_id, 'category_name': product.category_name,
                 'recovered_category_name': label} for product, label in changes]}
            # Create the evidence file exclusively before changing any rows.
            with backup.open('x', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            for product, label in changes:
                product.category_name = label
            db.add(AuditLog(user_id=None, action='repair_product_categories', target=f'fans8:{len(changes)}'))
            db.commit()
        return {'mode': 'apply' if apply else 'dry_run', 'products': len(changes),
                'backup_created': backup is not None}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(repair_categories(apply=args.apply), ensure_ascii=False))
