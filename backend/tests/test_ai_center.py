"""AI workflow tests use the isolated PostgreSQL database and mock model responses."""
from datetime import datetime,timedelta,timezone
import asyncio
from decimal import Decimal
import pytest
from sqlalchemy import select,func
from fastapi import HTTPException
from test_pipeline import isolated_database,clean_test_resources,db,admin_client
from test_comparison_catalogue import offers,viewer
from test_ai_analysis import configure,report
from app import ai_analysis as service
from app.db import SessionLocal
from app.models import (AIAnalysisRun,AIAnalysisBatch,AIDailyQuota,AIProductCheck,Product,
                        Platform,AISettings,utcnow)
from app.ai_jobs import day_key
from app.ai_selection import select_products,product_fingerprint


def seed_products(db,count=120):
    initial=offers(db,[{'platform':'A','id':'a'},{'platform':'B','id':'b'}])
    sources=list(initial.values())
    for i in range(count-2):
        template=sources[i%2]
        db.add(Product(platform_id=template.platform_id,account_label=template.account_label,
            source_id=f'bulk{i}',name=f'抖音视频点赞 {i}',category_name='抖音 / 视频',price=template.price,
            display_quantity=template.display_quantity,unit=template.unit,currency='CNY',
            status='active',quality=[],last_observed_at=utcnow(),last_run_id=template.last_run_id))
    db.commit()


@pytest.mark.asyncio
async def test_batches_share_daily_budget_count_success_and_no_replay(admin_client,db,monkeypatch):
    seed_products(db,1020)
    # Platform IDs deliberately occupy contiguous product ID ranges.
    rows=list(db.scalars(select(Product).order_by(Product.id)))
    platform_ids=list(db.scalars(select(Platform.id).order_by(Platform.id)))
    for i,p in enumerate(rows):
        p.platform_id=platform_ids[0 if i<510 else 1]
        p.source_id='sequential-'+str(p.id)
    db.commit()
    configure(admin_client,daily_product_limit=1000,daily_request_limit=20)
    sent=[]
    async def fake(connection,data):
        sent.append(data)
        result=report(data);result['_usage']={'prompt_tokens':123,'completion_tokens':45,'total_tokens':168}
        return result
    monkeypatch.setattr(service,'fetch_report',fake)
    run=service.create_analysis()
    assert (await service.execute_analysis(run['id']))['status']=='succeeded'
    assert len(sent)==20 and all(len(s['products'])==50 for s in sent)
    assert len({p['id'] for s in sent for p in s['products']})==1000
    assert all(len({p['platform_id'] for p in s['products']})==2 for s in sent)
    db.expire_all()
    quota=db.get(AIDailyQuota,day_key(utcnow()))
    assert (quota.products_used,quota.requests_used)==(1000,20)
    assert db.scalar(select(func.count()).select_from(AIProductCheck))==1000
    with pytest.raises(service.AnalysisError,match='额度'):
        service.create_analysis()
    assert (await service.execute_analysis(run['id']))=={'skipped':True}
    detail=admin_client.get(f"/api/ai-analysis/runs/{run['id']}").json()
    assert detail['usage']['total_tokens']==3360
    overview=admin_client.get('/api/ai-analysis/overview').json()
    assert overview['coverage']=={'total':1020,'checked':1000,'remaining':20}
    assert overview['today']['requests_used']==20


@pytest.mark.asyncio
async def test_failed_call_consumes_quota_and_only_manual_retry_resends(admin_client,db,monkeypatch):
    seed_products(db,60);configure(admin_client,daily_product_limit=200,daily_request_limit=4)
    sent=[]
    async def fail(connection,data):
        sent.append(data)
        raise service.AnalysisError('AI_AUTH_FAILED','接口拒绝访问')
    monkeypatch.setattr(service,'fetch_report',fail)
    run=service.create_analysis()
    assert (await service.execute_analysis(run['id']))['status']=='failed'
    db.expire_all()
    assert db.get(AIDailyQuota,day_key(utcnow())).requests_used==1
    assert db.scalar(select(func.count()).select_from(AIProductCheck))==0
    assert len(sent)==1
    db.expire_all()
    assert db.get(AIAnalysisRun,run['id']).message=='接口拒绝访问'
    async def okay(connection,data):return report(data)
    monkeypatch.setattr(service,'fetch_report',okay)
    retry=service.create_analysis(retry_id=run['id'])
    assert (await service.execute_analysis(retry['id']))['status']=='succeeded'
    db.expire_all()
    assert db.get(AIDailyQuota,day_key(utcnow())).requests_used==3
    assert db.scalar(select(func.count()).select_from(AIProductCheck))==60
    again=service.create_analysis(retry_id=run['id'])
    await service.execute_analysis(again['id'])
    db.expire_all()
    assert db.get(AIDailyQuota,day_key(utcnow())).requests_used==3


@pytest.mark.asyncio
async def test_stop_inflight_records_response_but_never_sends_later_batches(admin_client,db,monkeypatch):
    seed_products(db,60);configure(admin_client)
    run=service.create_analysis();calls=[]
    async def fake(connection,data):
        calls.append(data)
        response=admin_client.post(f"/api/ai-analysis/runs/{run['id']}/stop",json={})
        assert response.status_code==200
        return report(data)
    monkeypatch.setattr(service,'fetch_report',fake)
    assert (await service.execute_analysis(run['id']))['status']=='stopped'
    assert len(calls)==1
    detail=admin_client.get(f"/api/ai-analysis/runs/{run['id']}").json()
    assert detail['checked_count']==50
    assert [b['status'] for b in detail['batches']]==['succeeded','cancelled']


def test_report_get_never_recovers_or_changes_interrupted_tasks(admin_client,db):
    configure(admin_client)
    run=service.create_analysis()
    row=db.get(AIAnalysisRun,run['id']);row.created_at=utcnow()-timedelta(hours=1);db.commit()
    assert admin_client.get('/api/ai-analysis/runs').status_code==200
    assert admin_client.get('/api/ai-analysis/overview').status_code==200
    db.expire_all();assert db.get(AIAnalysisRun,run['id']).status=='queued'
    service.recover_abandoned(db);db.commit()
    assert db.get(AIAnalysisRun,run['id']).status=='failed'


def test_legacy_successful_report_shows_old_sample_count_and_terminal_stage(admin_client,db):
    row=AIAnalysisRun(schema_version=1,dedupe_key='legacy-count-fixture',settings_revision=1,
                      model='fixture',status='succeeded',snapshot={'sample_count':100},report={'summary':'旧报告'})
    db.add(row);db.commit()
    response=admin_client.get(f'/api/ai-analysis/runs/{row.id}').json()
    assert response['schema_version']==1 and response['stage']=='succeeded'
    assert response['checked_count']==response['planned_count']==100
    assert response['batches']==[]


def test_rotation_and_same_day_dedupe_are_fair(db,admin_client):
    seed_products(db,80);configure(admin_client)
    run=service.create_analysis()
    batch=AIAnalysisBatch(run_id=run['id'],ordinal=1,product_ids=[],status='succeeded')
    db.add(batch);db.flush()
    products=list(db.scalars(select(Product).order_by(Product.id)))
    for p in products[:40]:
        db.add(AIProductCheck(product_id=p.id,fingerprint=product_fingerprint(p),last_checked_at=utcnow(),last_batch_id=batch.id))
    batch.product_ids=[p.id for p in products[:40]];batch.request_day=day_key(utcnow());db.commit()
    ids=select_products(db,20,utcnow()-timedelta(days=1),utcnow())
    assert len(ids)==20 and not set(ids)&set(batch.product_ids)
    platform_ids={p.id:p.platform_id for p in products}
    assert len({platform_ids[i] for i in ids})==2


@pytest.mark.asyncio
async def test_content_fingerprint_excludes_observation_time_and_preserves_precision(db,admin_client,monkeypatch):
    rows=offers(db,[{'id':'a','price':'0.0000000000000000001'},{'id':'b'}]);p=rows['a']
    before=product_fingerprint(p);p.last_observed_at=utcnow()+timedelta(hours=1)
    assert product_fingerprint(p)==before
    p.price*=2
    assert product_fingerprint(p)!=before
    p.display_quantity=1
    fixed=product_fingerprint(p)
    p.display_quantity=Decimal('1.000000000000000000000')
    assert product_fingerprint(p)==fixed


def test_new_endpoints_permissions_and_csrf(admin_client):
    with viewer(admin_client) as client:
        for endpoint in ('overview','suggestions','applications'):
            assert client.get('/api/ai-analysis/'+endpoint).status_code==403
        for endpoint in ('runs/1/stop','runs/1/retry','suggestions/1/apply','suggestions/1/reject','applications/1/undo'):
            assert client.post('/api/ai-analysis/'+endpoint,json={'revision':1}).status_code==403
    token=admin_client.headers.pop('X-CSRF-Token')
    try:
        assert admin_client.post('/api/ai-analysis/runs/1/stop',json={}).status_code==403
    finally:admin_client.headers['X-CSRF-Token']=token


@pytest.mark.parametrize('field,value',[('daily_product_limit',0),('daily_product_limit',1001),('daily_request_limit',21),('daily_request_limit',True),('auto_apply','yes')])
def test_limits_are_server_validated(admin_client,field,value):
    assert admin_client.put('/api/ai-analysis/config',json={field:value}).status_code==422


@pytest.mark.asyncio
async def test_duplicate_delivery_while_model_request_inflight_is_skipped(admin_client,db,monkeypatch):
    seed_products(db,2);configure(admin_client)
    started=asyncio.Event();release=asyncio.Event();calls=[]
    async def fake(connection,data):
        calls.append(data);started.set();await release.wait();return report(data)
    monkeypatch.setattr(service,'fetch_report',fake)
    run=service.create_analysis()
    first=asyncio.create_task(service.execute_analysis(run['id']))
    await started.wait()
    assert await service.execute_analysis(run['id'])=={'skipped':True}
    release.set();await first
    assert len(calls)==1


def test_recovery_marks_sent_batch_uncertain_without_refunding_or_resending(admin_client,db):
    configure(admin_client)
    run=service.create_analysis()
    row=db.get(AIAnalysisRun,run['id']);row.status='running';row.heartbeat_at=utcnow()-timedelta(minutes=16)
    db.add(AIAnalysisBatch(run_id=row.id,ordinal=1,product_ids=[1],status='running',request_day=day_key(utcnow())))
    db.add(AIDailyQuota(day=day_key(utcnow()),products_used=1,requests_used=1));db.commit()
    service.recover_abandoned(db);db.commit()
    batch=db.scalar(select(AIAnalysisBatch))
    assert batch.status=='uncertain' and db.get(AIDailyQuota,day_key(utcnow())).requests_used==1
    assert row.status=='failed'
    assert row.failed_count==1


@pytest.mark.asyncio
async def test_retry_before_any_batch_was_planned_builds_a_new_plan(admin_client,db,monkeypatch):
    seed_products(db,2);configure(admin_client)
    old=service.create_analysis()
    assert admin_client.post(f"/api/ai-analysis/runs/{old['id']}/stop",json={}).status_code==200
    calls=[]
    async def fake(connection,data):calls.append(data);return report(data)
    monkeypatch.setattr(service,'fetch_report',fake)
    retry=service.create_analysis(retry_id=old['id'])
    assert (await service.execute_analysis(retry['id']))['status']=='succeeded'
    assert len(calls)==1 and len(calls[0]['products'])==2


@pytest.mark.asyncio
async def test_full_mock_report_auto_apply_and_api_undo(db,admin_client,monkeypatch):
    from test_ai_suggestions import catalogue,source_products
    from app.models import AIApplication,CanonicalProduct
    classification=catalogue(admin_client);products=source_products(db)
    before=[(p.id,p.name,p.price,p.category_name) for p in products]
    configure(admin_client,auto_apply=True)
    async def fake(connection,data):
        return {'summary':'固定模型建议','findings':[],'category_suggestions':[],
                'group_suggestions':[{'name':'抖音视频点赞','category_path':'抖音 / 视频',
                    'product_ids':[p['id'] for p in data['products']],'reason':'测试关联理由'}]}
    monkeypatch.setattr(service,'fetch_report',fake)
    run=service.create_analysis()
    assert (await service.execute_analysis(run['id']))['status']=='succeeded'
    listing=admin_client.get('/api/ai-analysis/suggestions').json()['items']
    assert listing[0]['status']=='applied'
    assert all(p['name'] and p['platform_name'] for p in listing[0]['products'])
    applied=admin_client.get('/api/ai-analysis/applications').json()['items'][0]
    assert applied['mode']=='auto' and applied['before']['created_group']
    response=admin_client.post(f"/api/ai-analysis/applications/{applied['id']}/undo",json={})
    assert response.status_code==200 and response.json()['status']=='undone'
    db.expire_all()
    assert before==[(p.id,p.name,p.price,p.category_name) for p in products]
    assert all(p.canonical_id is None for p in products)


def test_exhausted_daily_budget_shows_next_day_not_due_now(admin_client,db):
    from app.ai_overview import overview
    configure(admin_client,enabled=True,daily_time='00:00')
    db.add(AIDailyQuota(day=day_key(utcnow()),products_used=1000,requests_used=20));db.commit()
    result=overview(db)
    assert result['next_run_at'].startswith(day_key(utcnow()+timedelta(days=1)))
    assert result['schedule_reason']=='今日额度已用完'


@pytest.mark.asyncio
async def test_cross_beijing_midnight_charges_each_request_on_actual_send_day(admin_client,db,monkeypatch):
    from app import ai_jobs,ai_selection
    seed_products(db,60);configure(admin_client)
    clock=[datetime(2026,10,27,15,59,tzinfo=timezone.utc)]
    for module in (ai_jobs,ai_selection,service):monkeypatch.setattr(module,'utcnow',lambda:clock[0])
    calls=[]
    async def fake(connection,data):
        calls.append(data)
        clock[0]=datetime(2026,10,27,16,1,tzinfo=timezone.utc)
        return report(data)
    monkeypatch.setattr(service,'fetch_report',fake)
    run=service.create_analysis()
    assert (await service.execute_analysis(run['id']))['status']=='succeeded'
    db.expire_all()
    before=db.get(AIDailyQuota,'2026-10-27');after=db.get(AIDailyQuota,'2026-10-28')
    assert (before.products_used,before.requests_used)==(50,1)
    assert (after.products_used,after.requests_used)==(10,1)
    assert len(calls)==2


@pytest.mark.asyncio
async def test_saved_response_survives_review_failure_and_recovers_without_model_replay(admin_client,db,monkeypatch):
    from app import ai_jobs,ai_suggestions
    from test_ai_suggestions import catalogue,source_products
    from app.models import AISuggestion,AIApplication
    catalogue(admin_client);source_products(db);configure(admin_client,auto_apply=True)
    calls=[]
    async def fake(connection,data):
        calls.append(data)
        return {'summary':'有效报告不能因整理失败丢失','findings':[],'category_suggestions':[],
                'group_suggestions':[{'name':'抖音视频点赞','category_path':'抖音 / 视频',
                                     'product_ids':[p['id'] for p in data['products']],'reason':'固定测试'}]}
    monkeypatch.setattr(service,'fetch_report',fake)
    original=ai_suggestions.ingest_suggestions
    def interrupted(*args,**kwargs):raise RuntimeError('fixture-only storage interruption')
    monkeypatch.setattr(ai_suggestions,'ingest_suggestions',interrupted)
    run=service.create_analysis()
    result=await service.execute_analysis(run['id'])
    assert result['recovery_pending'] is True
    db.expire_all();batch=db.scalar(select(AIAnalysisBatch).where(AIAnalysisBatch.run_id==run['id']))
    assert batch.status=='reviewing' and batch.report['summary']=='有效报告不能因整理失败丢失'
    assert db.get(AIDailyQuota,day_key(utcnow())).requests_used==1
    monkeypatch.setattr(ai_suggestions,'ingest_suggestions',original)
    row=db.get(AIAnalysisRun,run['id']);row.heartbeat_at=utcnow()-timedelta(minutes=16);db.commit()
    service.recover_abandoned(db);db.commit()
    assert row.status=='succeeded' and batch.status=='succeeded' and row.checked_count==2
    assert db.scalar(select(func.count()).select_from(AISuggestion))==1
    assert db.scalar(select(AISuggestion)).status=='pending'
    assert db.scalar(select(func.count()).select_from(AIApplication))==0
    assert len(calls)==1 and db.get(AIDailyQuota,day_key(utcnow())).requests_used==1


@pytest.mark.asyncio
async def test_settings_changed_during_http_preserves_report_without_auto_application(admin_client,db,monkeypatch):
    from test_ai_suggestions import catalogue,source_products
    from app.models import AISuggestion,AIApplication
    catalogue(admin_client);source_products(db);configure(admin_client,auto_apply=True)
    async def fake(connection,data):
        changed=admin_client.put('/api/ai-analysis/config',json={'auto_apply':False})
        assert changed.status_code==200
        return {'summary':'配置变化期间的报告','findings':[],'category_suggestions':[],
                'group_suggestions':[{'name':'抖音视频点赞','category_path':'抖音 / 视频',
                                     'product_ids':[p['id'] for p in data['products']],'reason':'固定测试'}]}
    monkeypatch.setattr(service,'fetch_report',fake)
    run=service.create_analysis();await service.execute_analysis(run['id'])
    db.expire_all();batch=db.scalar(select(AIAnalysisBatch).where(AIAnalysisBatch.run_id==run['id']))
    assert batch.status=='succeeded' and batch.report['summary']=='配置变化期间的报告'
    assert db.scalar(select(AISuggestion)).status=='pending'
    assert db.scalar(select(func.count()).select_from(AIApplication))==0
