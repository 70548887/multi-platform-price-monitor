"""Saved AI connection diagnostics use only MockTransport and isolated test resources."""
from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest
from sqlalchemy import func, select

from test_pipeline import isolated_database, clean_test_resources, db, admin_client
from test_comparison_catalogue import viewer
from test_ai_analysis import KEY, configure
from app import ai_diagnostics as service, tasks
from app.ai_analysis import Connection
from app.db import SessionLocal
from app.models import AIAnalysisRun, AIDailyQuota, AISettings, AuditLog


def connection(**changes):
    return Connection(**{'base_url':'https://ai.example.org/v1','model':'fixture-model','key':KEY,**changes})


def completed_result(revision=1):
    return {'status':'connected','authenticated':True,'model_available':True,
            'models':['fixture-model'],'models_truncated':False,'latency_ms':1,
            'checked_at':'2026-10-27T08:00:00+00:00','config_revision':revision,
            'message':'固定连接诊断结果；未发送推理请求','error_code':None}


@pytest.mark.asyncio
async def test_models_check_is_one_authenticated_get_without_inference_or_body():
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.method=='GET' and str(request.url)=='https://ai.example.org/v1/models'
        assert request.content==b'' and request.headers['Authorization']=='Bearer '+KEY
        assert request.headers['Accept']=='application/json'
        return httpx.Response(200,json={'data':[{'id':'fixture-model'},{'id':'other-model'},{'id':'fixture-model'}]})
    result=await service.check_connection(connection(),7,transport=httpx.MockTransport(handler))
    assert len(calls)==1
    assert result['status']=='connected' and result['authenticated'] is True
    assert result['model_available'] is True and result['config_revision']==7
    assert result['models']==['fixture-model','other-model']
    assert result['latency_ms']>=0 and result['checked_at']
    assert KEY not in json.dumps(result)


@pytest.mark.asyncio
async def test_model_identifiers_are_bounded_deduplicated_and_do_not_echo_credentials(monkeypatch):
    proxy='http://fixture-user:fixture-password@proxy.example.org:8080'
    values=[KEY,'prefix-'+KEY,'fixture-user-model','fixture-password-model',proxy,
            'sk-provider-leak','github_pat_secret','ghp_secret','bad model','<script>','x'*121,
            None,17,'fixture-model','fixture-model']
    values += [f'model-{i}' for i in range(210)]
    def handler(request):
        return httpx.Response(200,json={'data':[{'id':value} for value in values]})
    original_client=httpx.AsyncClient
    def fixture_client(**options):
        # Explicit proxy transports otherwise take precedence over MockTransport.
        assert options.pop('proxy')==proxy
        return original_client(**options)
    monkeypatch.setattr(service.httpx,'AsyncClient',fixture_client)
    result=await service.check_connection(connection(proxy=proxy),3,transport=httpx.MockTransport(handler))
    assert len(result['models'])==200 and result['models_truncated'] is True
    assert len(result['models'])==len(set(result['models']))
    assert result['models'][0]=='fixture-model'
    text=json.dumps(result)
    for secret in (KEY,proxy,'fixture-user','fixture-password','sk-provider-leak','github_pat_secret','ghp_secret'):
        assert secret not in text


@pytest.mark.asyncio
async def test_missing_model_is_a_warning_and_not_an_inference_readiness_claim():
    result=await service.check_connection(connection(),1,transport=httpx.MockTransport(
        lambda request:httpx.Response(200,json={'data':[{'id':'other-model'}]})))
    assert result['status']=='warning' and result['authenticated'] is True
    assert result['model_available'] is False and result['error_code']=='AI_MODEL_NOT_LISTED'


@pytest.mark.asyncio
@pytest.mark.parametrize('status,expected,authenticated',[
    (401,'AI_AUTH_FAILED',False),(403,'AI_AUTH_FAILED',False),
    (429,'AI_RATE_LIMITED',None),(404,'AI_MODELS_UNSUPPORTED',None),
    (405,'AI_MODELS_UNSUPPORTED',None),(501,'AI_MODELS_UNSUPPORTED',None),
    (500,'AI_CONNECTION_HTTP_FAILED',None),
])
async def test_remote_failures_return_fixed_summaries_and_unknown_authentication(status,expected,authenticated):
    def handler(request):
        return httpx.Response(status,text='private provider body '+KEY)
    result=await service.check_connection(connection(),2,transport=httpx.MockTransport(handler))
    assert result['error_code']==expected and result['authenticated'] is authenticated
    assert result['models']==[] and result['model_available'] is None
    assert KEY not in json.dumps(result) and 'private provider body' not in result['message']


@pytest.mark.asyncio
async def test_redirect_is_not_followed_or_sent_the_bearer_token():
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(302,headers={'Location':'https://different.example.org/v1/models'})
    result=await service.check_connection(connection(),4,transport=httpx.MockTransport(handler))
    assert len(calls)==1 and calls[0].url.host=='ai.example.org'
    assert result['status']=='failed' and result['error_code']=='AI_CONNECTION_HTTP_FAILED'
    assert result['authenticated'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('payload',[
    b'<html>gateway page</html>',b'{invalid',b'{"data":{}}',b'{"data":[1]}',b'[]',
    b'x'*(256*1024+1),
],ids=['html','bad-json','data-not-array','row-not-object','top-level-array','oversized'])
async def test_invalid_or_oversized_models_response_is_not_treated_as_authentication_success(payload):
    result=await service.check_connection(connection(),1,transport=httpx.MockTransport(
        lambda request:httpx.Response(200,content=payload)))
    assert result['status']=='failed' and result['error_code']=='AI_MODELS_INVALID'
    assert result['authenticated'] is None and result['models']==[]


@pytest.mark.asyncio
async def test_network_exception_text_is_never_returned():
    def handler(request):raise httpx.ConnectError('credential '+KEY,request=request)
    result=await service.check_connection(connection(),1,transport=httpx.MockTransport(handler))
    assert result['error_code']=='AI_CONNECTION_FAILED'
    assert KEY not in json.dumps(result)


@pytest.mark.asyncio
async def test_non_public_dns_prevents_any_http_client_from_being_created(monkeypatch):
    calls=[]
    async def local_dns(*args,**kwargs):
        calls.append(args[0])
        return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
    def forbidden_client(*args,**kwargs):pytest.fail('HTTP client must not be created for a private DNS result')
    loop=asyncio.get_running_loop()
    monkeypatch.setattr(loop,'getaddrinfo',local_dns)
    monkeypatch.setattr(service.httpx,'AsyncClient',forbidden_client)
    result=await service.check_connection(connection(),1)
    assert calls==['ai.example.org']
    assert result['error_code']=='AI_ENDPOINT_UNAVAILABLE' and result['authenticated'] is None


@pytest.mark.asyncio
async def test_dns_timeout_is_bounded_and_returns_safe_failure(monkeypatch):
    async def timeout(*args,**kwargs):raise asyncio.TimeoutError
    monkeypatch.setattr(asyncio.get_running_loop(),'getaddrinfo',timeout)
    result=await service.check_connection(connection(),1)
    assert result['status']=='failed' and result['error_code']=='AI_ENDPOINT_UNAVAILABLE'


def test_diagnostic_endpoint_requires_admin_and_csrf_without_remote_requests(admin_client,monkeypatch):
    async def forbidden(*args,**kwargs):pytest.fail('Unauthorized request must not call a provider')
    monkeypatch.setattr(service,'check_connection',forbidden)
    with viewer(admin_client) as client:
        assert client.post('/api/ai-analysis/connection-check',json={}).status_code==403
    token=admin_client.headers.pop('X-CSRF-Token')
    try:
        assert admin_client.post('/api/ai-analysis/connection-check',json={}).status_code==403
    finally:admin_client.headers['X-CSRF-Token']=token


def test_diagnostic_uses_only_saved_config_and_never_creates_analysis_or_quota(admin_client,db,monkeypatch):
    config=configure(admin_client)
    calls=[]
    async def fake(saved,revision):
        calls.append((saved.base_url,saved.model,revision))
        assert saved.key==KEY
        return completed_result(revision)
    monkeypatch.setattr(service,'check_connection',fake)
    response=admin_client.post('/api/ai-analysis/connection-check',json={})
    assert response.status_code==200 and response.json()['status']=='connected'
    assert len(calls)==1 and calls[0][:2]==('https://ai.example.org/v1','fixture-model')
    assert KEY not in response.text
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(AIAnalysisRun))==0
    assert db.scalar(select(func.count()).select_from(AIDailyQuota))==0
    assert db.scalar(select(AuditLog.action).where(AuditLog.action=='ai_connection_check'))=='ai_connection_check'
    assert tasks.redis_client().get('pm:ai:connection-check') is None


def test_unconfigured_saved_connection_is_a_safe_conflict(admin_client,monkeypatch):
    async def forbidden(*args,**kwargs):pytest.fail('Unconfigured request must not call a provider')
    monkeypatch.setattr(service,'check_connection',forbidden)
    response=admin_client.post('/api/ai-analysis/connection-check',json={})
    assert response.status_code==409 and response.json()['error_code']=='AI_NOT_CONFIGURED'


def test_concurrent_diagnostic_is_rejected_and_does_not_remove_other_owners_lock(admin_client,monkeypatch):
    configure(admin_client)
    rc=tasks.redis_client();rc.set('pm:ai:connection-check','another-test-owner',ex=45)
    async def forbidden(*args,**kwargs):pytest.fail('Busy diagnostic must not call a provider')
    monkeypatch.setattr(service,'check_connection',forbidden)
    response=admin_client.post('/api/ai-analysis/connection-check',json={})
    assert response.status_code==429
    assert rc.get('pm:ai:connection-check')=='another-test-owner'


def test_config_revision_changed_during_request_discards_old_model_list(admin_client,db,monkeypatch):
    configure(admin_client)
    async def changed(saved,revision):
        with SessionLocal() as other:
            row=other.get(AISettings,1);row.model='new-fixture';row.revision+=1;other.commit()
        return completed_result(revision)
    monkeypatch.setattr(service,'check_connection',changed)
    response=admin_client.post('/api/ai-analysis/connection-check',json={})
    payload=response.json()
    assert response.status_code==200 and payload['status']=='warning'
    assert payload['error_code']=='AI_SETTINGS_CHANGED' and payload['models']==[]
    assert payload['authenticated'] is None and payload['model_available'] is None
    assert tasks.redis_client().get('pm:ai:connection-check') is None


def test_endpoint_releases_only_its_own_lock_if_timeout_is_reported(admin_client,monkeypatch):
    configure(admin_client)
    async def replaced(saved,revision):
        tasks.redis_client().set('pm:ai:connection-check','replacement-owner',ex=45)
        return {**completed_result(revision),'status':'failed','error_code':'AI_CONNECTION_FAILED'}
    monkeypatch.setattr(service,'check_connection',replaced)
    response=admin_client.post('/api/ai-analysis/connection-check',json={})
    assert response.status_code==200 and response.json()['error_code']=='AI_CONNECTION_FAILED'
    assert tasks.redis_client().get('pm:ai:connection-check')=='replacement-owner'


def test_redis_unavailable_returns_safe_service_error_before_remote_call(admin_client,monkeypatch):
    configure(admin_client)
    def unavailable():raise RuntimeError('private broker '+KEY)
    async def forbidden(*args,**kwargs):pytest.fail('Redis lock failure must not call a provider')
    monkeypatch.setattr(tasks,'redis_client',unavailable)
    monkeypatch.setattr(service,'check_connection',forbidden)
    response=admin_client.post('/api/ai-analysis/connection-check',json={})
    assert response.status_code==503
    assert KEY not in response.text and 'private broker' not in response.text


def test_redis_release_error_does_not_replace_a_completed_diagnostic(admin_client,monkeypatch):
    configure(admin_client)
    class ReleasingConnection:
        def set(self,*args,**kwargs):return True
        def eval(self,*args,**kwargs):raise RuntimeError('private broker '+KEY)
    async def fake(saved,revision):return completed_result(revision)
    monkeypatch.setattr(tasks,'redis_client',lambda:ReleasingConnection())
    monkeypatch.setattr(service,'check_connection',fake)
    response=admin_client.post('/api/ai-analysis/connection-check',json={})
    assert response.status_code==200 and response.json()['status']=='connected'
    assert KEY not in response.text and 'private broker' not in response.text


def test_overview_shows_safe_latest_failure_and_clears_it_after_a_success(admin_client,db):
    configure(admin_client)
    failed=AIAnalysisRun(dedupe_key='diagnostic-failure-fixture',settings_revision=1,
                         model='fixture',status='failed',error_code='AI_RATE_LIMITED',
                         message='unsafe-provider-message '+KEY)
    db.add(failed);db.commit()
    payload=admin_client.get('/api/ai-analysis/overview').json()
    assert payload['last_issue']['id']==failed.id
    assert payload['last_issue']['error_code']=='AI_RATE_LIMITED'
    assert '429' in payload['last_issue']['message'] and payload['last_issue']['action_hint']
    assert KEY not in json.dumps(payload)
    success=AIAnalysisRun(dedupe_key='diagnostic-success-fixture',settings_revision=1,
                          model='fixture',status='succeeded')
    db.add(success);db.commit()
    assert admin_client.get('/api/ai-analysis/overview').json()['last_issue'] is None
