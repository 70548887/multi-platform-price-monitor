"""Disposable, populated UI fixture. Never uses production DB, Redis or model I/O.

Run from the repository with PYTHONPATH=backend;backend/tests:
  python backend/tests/ai_ui_preview.py seed
  python backend/tests/ai_ui_preview.py serve --port 28679
The named price_monitor_test database is intentionally reset by `seed`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path


def prepare():
    # The shared test module sets *explicit* local test URLs before importing app.
    from test_pipeline import isolated_database, engine, tasks
    from sqlalchemy.engine import make_url
    from urllib.parse import urlsplit
    if (make_url(str(engine.url)).database != 'price_monitor_test'
            or engine.url.host not in ('127.0.0.1', 'localhost')
            or urlsplit(tasks.config.REDIS_URL).path != '/15'):
        raise RuntimeError('UI preview requires local price_monitor_test and Redis 15')
    setup = isolated_database.__wrapped__()
    next(setup)
    return setup


async def seed():
    from sqlalchemy import text, select
    from app.db import Base, SessionLocal, engine
    from app.models import User, ComparisonCategory, CanonicalProduct, AIAnalysisRun, Product
    from app.security import password_hash
    from app.ai_config import save_ai_settings
    from app import ai_analysis
    from test_comparison_catalogue import offers

    with engine.begin() as conn:
        conn.execute(text('TRUNCATE TABLE ' + ','.join('"'+t.name+'"' for t in Base.metadata.sorted_tables)
                          + ' RESTART IDENTITY CASCADE'))
    with SessionLocal() as db:
        owner=User(username='ui-admin',password_hash=password_hash('ui-fixture-password'),role='admin',enabled=True)
        db.add(owner);db.flush()
        roots={}
        for name in ('抖音','哔哩哔哩'):
            root=ComparisonCategory(name=name);db.add(root);db.flush()
            leaf=ComparisonCategory(name='视频',parent_id=root.id);db.add(leaf);db.flush();roots[name]=leaf
        target=CanonicalProduct(name='哔哩哔哩视频点赞',category_id=roots['哔哩哔哩'].id,
                                unit='人',currency='CNY',match_mode='exact')
        db.add(target);db.commit()
        specs=[
            {'platform':'A','id':'a1','name':'抖音视频真人点赞','price':'0.012345'},
            {'platform':'B','id':'b1','name':'DY视频协议赞','price':'0.023456'},
            {'platform':'A','id':'a2','name':'B站视频点赞','price':'0.034567'},
            {'platform':'B','id':'b2','name':'BL视频赞','price':'0.045678'},
            {'platform':'A','id':'a3','name':'快手视频收藏','price':'0.056789'},
            {'platform':'B','id':'b3','name':'KS视频收藏','price':'0.067890'},
        ]
        products=offers(db,specs)
        for identifier,product in products.items():
            product.category_name=('抖音 / 视频' if identifier.endswith('1') else
                                   '哔哩哔哩 / 视频' if identifier.endswith('2') else '快手 / 视频')
        save_ai_settings(db,{'base_url':'https://ai.example.org/v1','key':'ui-fixture-key-no-network',
                            'model':'ui-fixture','enabled':False,'auto_apply':True})
        db.commit();owner_id=owner.id
    original=ai_analysis.fetch_report
    async def model(connection,data):
        by_name={p['name']:p['id'] for p in data['products']}
        return {'summary':'固定样本界面验收报告：6件商品，包含自动关联、人工待审核和新类目建议。',
                'findings':[{'severity':'warning','title':'核对源商品计价口径','evidence':'样本价格按人计费，展示数量为1000；相似归总不自动判定最低价。',
                             'product_ids':[by_name['抖音视频真人点赞']]}],
                'category_suggestions':[],
                'group_suggestions':[
                    {'name':'抖音视频点赞','category_path':'抖音 / 视频','product_ids':[by_name['抖音视频真人点赞'],by_name['DY视频协议赞']],'reason':'两个交易平台，业务、动作、场景一致。'},
                    {'name':'哔哩哔哩视频点赞','category_path':'哔哩哔哩 / 视频','product_ids':[by_name['B站视频点赞'],by_name['BL视频赞']],'reason':'同规格目标需要管理员确认计价数量与单位。'},
                    {'name':'快手视频收藏','category_path':'快手 / 视频 / 收藏','product_ids':[by_name['快手视频收藏'],by_name['KS视频收藏']],'reason':'需要先建立比价类目，再由管理员选择目标。'}],
                '_usage':{'prompt_tokens':1200,'completion_tokens':250,'total_tokens':1450}}
    try:
        ai_analysis.fetch_report=model
        run=ai_analysis.create_analysis(owner_id)
        result=await ai_analysis.execute_analysis(run['id'])
    finally:
        ai_analysis.fetch_report=original
    with SessionLocal() as db:
        legacy=AIAnalysisRun(schema_version=1,dedupe_key='ui-legacy-report',trigger='manual',
            actor_id=owner_id,settings_revision=1,model='legacy-fixture',status='succeeded',
            snapshot={'sample_count':6,'products':[{'id':p.id,'name':p.name,'platform_id':p.platform_id,
                'platform_name':'旧版平台样本','unit':p.unit,'currency':p.currency,
                'display_quantity':str(p.display_quantity)} for p in db.scalars(select(Product))]},
            report={'summary':'旧版只读报告：保留原始分类建议，不迁移自动应用',
                    'findings':[],
                    'category_suggestions':[{'product_id':5,'category_path':'快手 / 视频 / 收藏','group_name':'快手视频收藏','reason':'旧版分类建议保留'}],
                    'group_suggestions':[{'name':'旧版快手收藏归总','category_path':'快手 / 视频 / 收藏',
                        'product_ids':[5,6],'reason':'旧版归总建议保留，只读查看'}]})
        db.add(legacy);db.commit()
    print(json.dumps({'fixture_only':True,'database':'price_monitor_test','result':result,
                      'source_calls':0,'paid_model_calls':0},ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('seed','serve'))
    parser.add_argument('--port',type=int,default=28679)
    args=parser.parse_args()
    setup=prepare()
    try:
        if args.action=='seed':
            asyncio.run(seed())
        else:
            import uvicorn
            uvicorn.run('app.main:app',host='127.0.0.1',port=args.port,log_level='warning')
    finally:
        try:next(setup)
        except StopIteration:pass


if __name__=='__main__':
    main()
