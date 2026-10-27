"""Real worker fixture: all model/source I/O is local, only test DB/Redis allowed."""
from __future__ import annotations

import asyncio
import json
import os
import time
from urllib.parse import urlsplit

import httpx
from sqlalchemy.engine import make_url

from app import ai_analysis, tasks
from worker_harness import LocalFixtureCollector


if (make_url(tasks.config.DATABASE_URL).database != 'price_monitor_test'
        or urlsplit(tasks.config.REDIS_URL).path != '/15'
        or urlsplit(tasks.config.REDIS_URL).hostname not in ('127.0.0.1', 'localhost')):
    raise RuntimeError('AI process fixture requires local price_monitor_test and Redis 15')
prefix = os.environ.get('AI_RUNTIME_TEST_PREFIX', '')
if not prefix.startswith('ai-runtime-test-'):
    raise RuntimeError('An isolated runtime test prefix is required')

celery_app = tasks.celery_app
celery_app.conf.broker_transport_options = {
    **celery_app.conf.broker_transport_options, 'global_keyprefix': prefix+':'
}
celery_app.conf.result_backend_transport_options = {'global_keyprefix': prefix+':'}
tasks.Xiaohong66Collector = LocalFixtureCollector
real_fetch_report = ai_analysis.fetch_report


async def fixture_fetch(connection, data):
    async def respond(request):
        assert request.url.path.endswith('/chat/completions')
        body = json.loads(request.content)
        assert body['model'] == 'fixture-model'
        rc = tasks.redis_client()
        rc.set(prefix+':ai-started', str(time.time()), ex=90)
        deadline = time.monotonic()+45
        while not rc.exists(prefix+':release'):
            if time.monotonic() >= deadline:
                raise httpx.ReadTimeout('Local fixture gate expired', request=request)
            await asyncio.sleep(.05)
        report = {'summary':'真实进程本地模型测试', 'findings':[],
                  'category_suggestions':[], 'group_suggestions':[]}
        return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(report)},
                                                   'finish_reason':'stop'}],
                                         'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}})
    return await real_fetch_report(connection, data, transport=httpx.MockTransport(respond))


ai_analysis.fetch_report = fixture_fetch
