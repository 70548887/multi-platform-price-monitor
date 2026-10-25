"""Isolated integration-test worker. Refuse production resources before patching HTTP."""
from __future__ import annotations

from decimal import Decimal
from sqlalchemy.engine import make_url

from app import config, tasks
from app.collectors.base import SourceProduct

if make_url(config.DATABASE_URL).database != "price_monitor_test" or not config.REDIS_URL.endswith("/15"):
    raise RuntimeError("The worker fixture can only run against isolated test resources")


class LocalFixtureCollector:
    expected_total = 1

    def __init__(self, base_url, token, rule, request_gate=None, sleep=None):
        self.request_gate = request_gate
        self.sleep = sleep

    async def iter_pages(self):
        if self.request_gate:
            await self.request_gate()
        yield [SourceProduct(
            source_id="9585", name="本地Worker集成样本", category_id="test", category_name="测试",
            price=Decimal("0.007486"), unit="人", display_quantity=Decimal("1000"),
            status="active", stock=None, stock_unlimited=True,
            raw={"price": "0.007486", "price_show_num": "1000", "currency": "CNY"},
        )]

    async def aclose(self):
        pass


tasks.Xiaohong66Collector = LocalFixtureCollector
celery_app = tasks.celery_app
