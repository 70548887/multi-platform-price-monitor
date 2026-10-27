"""AI integration uses encrypted fixture keys and MockTransport, never a paid endpoint."""
from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import func, select

from test_pipeline import isolated_database, clean_test_resources, db, admin_client
from test_comparison_catalogue import offers, viewer
from app import ai_analysis as service, tasks
from app.models import AISettings, AIAnalysisRun, Product, Platform, AuditLog
from app.security import decrypt_session

KEY = 'fixture-ai-secret-123456789'


def configure(admin, **changes):
    result=admin.put('/api/ai-analysis/config',json={
        'base_url':'https://ai.example.org/v1','model':'fixture-model','key':KEY,
        'daily_time':'09:00','enabled':False,**changes})
    assert result.status_code==200,result.text
    assert KEY not in result.text
    return result.json()


def report(data):
    ids=[p['id'] for p in data['products']]
    return {'summary':'样本核对结果待人工确认','findings':[{'severity':'warning','title':'检查计价单位',
        'evidence':'部分源站未说明计价单位','product_ids':ids[:1]}],
        'category_suggestions':[],'group_suggestions':[]}


def test_configuration_is_disabled_by_default_and_key_is_write_only(admin_client,db):
    default=admin_client.get('/api/ai-analysis/config').json()
    assert default['enabled'] is False and default['key_configured'] is False
    assert default['daily_time']=='09:00'
    configure(admin_client)
    row=db.get(AISettings,1)
    assert row.encrypted_key != KEY and decrypt_session(row.encrypted_key)==KEY
    public=admin_client.get('/api/ai-analysis/config').text
    assert KEY not in public and row.encrypted_key not in public
    original=row.encrypted_key
    response=admin_client.put('/api/ai-analysis/config',json={'key':'','sample_limit':150})
    assert response.status_code==200
    db.expire_all();assert db.get(AISettings,1).encrypted_key==original
    assert admin_client.put('/api/ai-analysis/config',json={'clear_key':True}).status_code==200
    db.expire_all();assert db.get(AISettings,1).encrypted_key is None
    assert db.get(AISettings,1).enabled is False


@pytest.mark.parametrize('changes',[
    {'base_url':'http://ai.example.org/v1'},{'base_url':'https://u:private@ai.example.org/v1'},
    {'base_url':'https://127.0.0.1/v1'},{'base_url':'https://api.zxcbug.com/v1'},
    {'base_url':'https://ai.example.org/v1?token=private'},
    {'base_url':'https://host.internal/v1'},{'daily_time':'25:00'},
    {'sample_limit':True},{'sample_limit':1000},{'proxy_route':'untrusted'},
    {'enabled':'true'},{'key':['do-not-echo-private']},{'model':'bad\nmodel'},
])
def test_bad_configuration_is_rejected_without_echoing_secrets(admin_client,changes):
    response=admin_client.put('/api/ai-analysis/config',json=changes)
    assert response.status_code==422
    assert 'private' not in response.text and 'do-not-echo' not in response.text


def test_enabling_requires_configuration_and_reports_are_admin_only(admin_client):
    assert admin_client.put('/api/ai-analysis/config',json={'enabled':True}).status_code==422
    assert admin_client.post('/api/ai-analysis/run',json={}).status_code==409
    with viewer(admin_client) as client:
        for path in ('/config','/runs','/runs/1','/preview'):
            assert client.get('/api/ai-analysis'+path).status_code==403
        assert client.post('/api/ai-analysis/run',json={}).status_code==403
        assert client.put('/api/ai-analysis/config',json={'enabled':True}).status_code==403
    token=admin_client.headers.pop('X-CSRF-Token')
    try:
        assert admin_client.put('/api/ai-analysis/config',json={'key':KEY}).status_code==403
        assert admin_client.post('/api/ai-analysis/run',json={}).status_code==403
    finally:admin_client.headers['X-CSRF-Token']=token


def test_local_preview_without_key_is_read_only_and_checks_actual_observation_counts(db,admin_client):
    offers(db,[{'platform':'A','id':'a'},{'platform':'B','id':'b'}])
    response=admin_client.get('/api/ai-analysis/preview')
    assert response.status_code==200,response.text
    payload=response.json()
    assert payload['total_products']==2 and payload['sample_count']==2
    assert db.scalar(select(func.count()).select_from(AIAnalysisRun))==0
    assert db.get(AISettings,1) is None
    assert all('stored_observations' in batch for platform in payload['platforms'] for batch in platform['batches'])
    assert 'encrypted_session' not in response.text and 'account_label' not in response.text


@pytest.mark.asyncio
async def test_real_queue_worker_generates_read_only_snapshot_report_and_deduplicates(admin_client,db,monkeypatch):
    rows=offers(db,[{'platform':'A','id':'a','name':'B站点赞'},{'platform':'B','id':'b','name':'bili点赞'}])
    before=[(p.id,p.price,p.canonical_id,p.category_name) for p in db.scalars(select(Product).order_by(Product.id))]
    configure(admin_client)
    queued=[];sent=[]
    monkeypatch.setattr(tasks.analyze_ai_data,'delay',lambda identifier:queued.append(identifier))
    async def fake(connection,data):
        assert connection.key==KEY
        sent.append(data)
        assert 'encrypted_session' not in json.dumps(data) and 'account_label' not in json.dumps(data)
        assert KEY not in json.dumps(data) and 'base_url' not in json.dumps(data)
        return report(data)
    monkeypatch.setattr(service,'fetch_report',fake)
    result=admin_client.post('/api/ai-analysis/run',json={})
    assert result.status_code==202
    identifier=result.json()['id'];assert queued==[identifier]
    assert admin_client.post('/api/ai-analysis/run',json={}).status_code==409
    assert (await service.execute_analysis(identifier))['status']=='succeeded'
    assert (await service.execute_analysis(identifier))=={'skipped':True}
    assert len(sent)==1
    detail=admin_client.get(f'/api/ai-analysis/runs/{identifier}').json()
    assert detail['snapshot']['total_products']==2 and detail['snapshot']['sample_count']==2
    assert detail['report']['summary'] and detail['status']=='succeeded'
    assert KEY not in json.dumps(detail)
    db.expire_all()
    assert before==[(p.id,p.price,p.canonical_id,p.category_name) for p in db.scalars(select(Product).order_by(Product.id))]
    assert 'ai_analysis_succeeded' in list(db.scalars(select(AuditLog.action)))


def test_daily_check_is_disabled_until_enabled_and_runs_once_per_beijing_day(admin_client,db,monkeypatch):
    queued=[]
    monkeypatch.setattr(tasks.analyze_ai_data,'delay',lambda identifier:queued.append(identifier))
    assert service.scheduled_analysis()=={'queued':None}
    configure(admin_client,enabled=True,daily_time='09:00')
    before=datetime(2026,10,27,0,59,tzinfo=timezone.utc)
    assert service.create_analysis(daily=True,now=before) is None
    due=datetime(2026,10,27,1,0,tzinfo=timezone.utc)
    first=service.create_analysis(daily=True,now=due)
    assert first and first['trigger']=='daily'
    assert service.create_analysis(daily=True,now=due+timedelta(hours=2)) is None
    db.expire_all();row=db.get(AIAnalysisRun,first['id']);row.status='succeeded';db.commit()
    assert service.create_analysis(daily=True,now=due+timedelta(hours=3)) is None
    assert service.create_analysis(daily=True,now=due+timedelta(days=1)) is not None
    assert tasks.celery_app.conf.beat_schedule['ai-analysis-check']['task']=='app.tasks.check_ai_analysis'


@pytest.mark.asyncio
async def test_changed_configuration_cancels_queued_run_and_no_remote_call(admin_client,monkeypatch):
    configure(admin_client)
    queued=service.create_analysis()
    assert admin_client.put('/api/ai-analysis/config',json={'enabled':False}).status_code==200
    async def forbidden(*args):pytest.fail('Changed configuration must not send data')
    monkeypatch.setattr(service,'fetch_report',forbidden)
    assert (await service.execute_analysis(queued['id']))['status']=='failed'
    detail=admin_client.get(f"/api/ai-analysis/runs/{queued['id']}").json()
    assert detail['error_code']=='AI_SETTINGS_CHANGED'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['redirect','auth','network','unknown_product','bad_json','truncated','large'])
async def test_transport_failures_are_bounded_sanitized_and_cannot_invent_product_ids(failure):
    requests=[]
    def handler(request):
        requests.append(request)
        assert request.headers['Authorization']=='Bearer '+KEY
        assert KEY not in request.content.decode()
        if failure=='redirect':return httpx.Response(302,headers={'Location':'https://other.example.org'})
        if failure=='auth':return httpx.Response(401,text=KEY)
        if failure=='network':raise httpx.ConnectError(KEY,request=request)
        if failure=='large':return httpx.Response(200,content=b'x'*270000)
        value=report({'products':[{'id':9999}]}) if failure=='unknown_product' else {'summary':'okay'}
        content='not-json' if failure=='bad_json' else json.dumps(value)
        return httpx.Response(200,json={'choices':[{'message':{'content':content},'finish_reason':'length' if failure=='truncated' else 'stop'}]})
    connection=service.Connection('https://ai.example.org/v1','fixture',KEY)
    with pytest.raises(service.AnalysisError) as caught:
        await service.fetch_report(connection,{'products':[{'id':1}]},transport=httpx.MockTransport(handler))
    assert len(requests)==1
    assert KEY not in caught.value.message and KEY not in str(caught.value)


@pytest.mark.asyncio
async def test_valid_json_report_preserves_decimal_input_and_redacts_echoed_key():
    data={'products':[{'id':1,'price':'0.000000000000000001'}]}
    def handler(request):
        body=json.loads(request.content)
        assert body['response_format']=={'type':'json_object'}
        assert data['products'][0]['price'] in body['messages'][1]['content']
        value=report(data);value['summary']='检测完成 '+KEY
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(value)},'finish_reason':'stop'}]})
    result=await service.fetch_report(service.Connection('https://ai.example.org/v1','fixture',KEY),data,transport=httpx.MockTransport(handler))
    assert KEY not in json.dumps(result) and '[REDACTED]' in result['summary']


@pytest.mark.asyncio
async def test_reasoning_model_uses_modern_token_parameter_and_http_errors_are_sanitized():
    def handler(request):
        body=json.loads(request.content)
        assert body['max_completion_tokens']==4000 and 'max_tokens' not in body
        return httpx.Response(400,json={'error':{'message':'Unsupported response_format '+KEY}})
    connection=service.Connection('https://ai.example.org/v1','gpt-5.4-mini',KEY)
    with pytest.raises(service.AnalysisError) as exc:
        await service.fetch_report(connection,{'products':[{'id':1}]},transport=httpx.MockTransport(handler))
    assert exc.value.code=='AI_JSON_MODE_UNSUPPORTED'
    assert KEY not in exc.value.message
