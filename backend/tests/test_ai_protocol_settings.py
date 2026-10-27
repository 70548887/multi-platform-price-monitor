"""Persisted protocol choices and the actual request/parse integration."""
import json
import httpx
import pytest
from sqlalchemy import select

from test_pipeline import isolated_database,clean_test_resources,db,admin_client
from test_ai_analysis import configure,KEY
from app import ai_analysis as service
from app.models import AISettings,AIAnalysisRun
from app.ai_config import endpoint_url


def test_existing_defaults_and_full_endpoint_normalization(admin_client,db):
    public=admin_client.get('/api/ai-analysis/config').json()
    assert public['api_format']=='chat_completions' and public['reasoning_effort']=='auto'
    assert public['max_output_tokens']==4000
    for suffix in ('/chat/completions','/responses','/messages'):
        assert endpoint_url('https://ai.example.org/v1'+suffix)=='https://ai.example.org/v1'
    configure(admin_client,api_format='responses',reasoning_effort='high',max_output_tokens=8192)
    task=service.create_analysis()
    run=db.get(AIAnalysisRun,task['id'])
    assert (run.api_format,run.reasoning_effort,run.max_output_tokens)==('responses','high',8192)
    assert admin_client.put('/api/ai-analysis/config',json={'reasoning_effort':'auto'}).status_code==200
    db.expire_all()
    assert run.reasoning_effort=='high'


@pytest.mark.parametrize('changes',[
    {'api_format':'arbitrary'}, {'reasoning_effort':'huge'}, {'max_output_tokens':True},
    {'max_output_tokens':1023},{'max_output_tokens':32769},
    {'api_format':'anthropic_messages','reasoning_effort':'none'},
    {'api_format':'anthropic_messages','reasoning_effort':'minimal'},
    {'api_format':'anthropic_messages','reasoning_effort':'ultra'},
    {'base_url':'https://api.openai.com/v1','reasoning_effort':'ultra'},
])
def test_invalid_protocol_or_effort_rejected(admin_client,changes):
    assert admin_client.put('/api/ai-analysis/config',json=changes).status_code==422


@pytest.mark.asyncio
@pytest.mark.parametrize('api_format,path', [('chat_completions','/v1/chat/completions'),('responses','/v1/responses'),('anthropic_messages','/v1/messages')])
async def test_protocol_roundtrip_keeps_report_contract_and_auth_boundary(api_format,path):
    expected={'summary':'多协议固定报告','findings':[], 'category_suggestions':[], 'group_suggestions':[]}
    requests=[]
    def handler(request):
        requests.append(request)
        assert request.method=='POST' and request.url.path==path
        body=json.loads(request.content)
        if api_format=='chat_completions':
            assert body['reasoning_effort']=='medium' and 'thinking' not in body
            assert request.headers['Authorization']=='Bearer '+KEY
            return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':json.dumps(expected)},'finish_reason':'stop'}]})
        if api_format=='responses':
            assert body['reasoning']=={'effort':'medium'} and body['store'] is False
            assert body['text']['format']['type']=='json_object'
            return httpx.Response(200,json={'status':'completed','output':[{'type':'reasoning','summary':[{'type':'summary_text','text':'internal-do-not-store'}]},
                {'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(expected)}]}],
                'usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15}})
        assert 'authorization' not in request.headers
        assert request.headers['x-api-key']==KEY and request.headers['anthropic-version']
        assert body['thinking']['type']=='adaptive' and body['output_config']['effort']=='medium'
        assert body['output_config']['format']['type']=='json_schema'
        return httpx.Response(200,json={'type':'message','role':'assistant','stop_reason':'end_turn',
            'content':[{'type':'thinking','thinking':'internal-do-not-store'},{'type':'text','text':json.dumps(expected)}],
            'usage':{'input_tokens':10,'output_tokens':5}})
    conn=service.Connection('https://ai.example.org/v1','fixture-model',KEY,api_format=api_format,reasoning_effort='medium',max_output_tokens=8192)
    result=await service.fetch_report(conn,{'products':[{'id':1}]},transport=httpx.MockTransport(handler))
    assert result['summary']==expected['summary'] and len(requests)==1
    assert 'internal-do-not-store' not in json.dumps(result)
    if api_format!='chat_completions':assert result['_usage']['prompt_tokens']==10


@pytest.mark.asyncio
async def test_anthropic_diagnostic_uses_protocol_headers_without_inference():
    from app.ai_diagnostics import check_connection
    def handler(request):
        assert request.method=='GET' and request.url.path=='/v1/models'
        assert request.headers['x-api-key']==KEY and 'authorization' not in request.headers
        return httpx.Response(200,json={'data':[{'id':'fixture-model'}]})
    result=await check_connection(service.Connection('https://ai.example.org/v1','fixture-model',KEY,api_format='anthropic_messages'),1,transport=httpx.MockTransport(handler))
    assert result['status']=='connected'


def test_report_usage_does_not_invent_total_for_messages_api(admin_client,db):
    from app.models import AIAnalysisBatch
    from app.ai_jobs import run_progress
    configure(admin_client,api_format='anthropic_messages')
    task=service.create_analysis()
    run=db.get(AIAnalysisRun,task['id'])
    db.add_all([AIAnalysisBatch(run_id=run.id,ordinal=ordinal,status='succeeded',
        product_ids=[ordinal],usage={'prompt_tokens':10,'completion_tokens':5}) for ordinal in (1,2)])
    db.commit()
    progress=run_progress(db,run)
    assert progress['usage']=={'prompt_tokens':20,'completion_tokens':10}
    assert progress['usage_partial'] is False
