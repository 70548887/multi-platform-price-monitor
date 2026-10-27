"""End-to-end automatic organization uses only isolated DB and model fixtures."""
import json
from decimal import Decimal
import httpx
import pytest
from sqlalchemy import select,func
from test_pipeline import isolated_database,clean_test_resources,db,admin_client
from test_ai_analysis import configure,KEY
from test_comparison_catalogue import offers,category,group,viewer
from app import ai_analysis as service,tasks
from app.models import Product,AISettings,AIAnalysisRun,AIApplication,AIAnalysisBatch,AISuggestion,CanonicalProduct
from app.ai_protocols import strict_report_schema


def setup_targets(db,admin):
    root=category(admin,'抖音');leaf=category(admin,'综合互动',parent_id=root['id'])
    target=group(admin,'抖音点赞',category_id=leaf['id'],match_mode='similar',product_ids=[])
    products=offers(db,[{'id':'a','platform':'A','name':'DY快速点赞','unit':'','quantity':'0','quality':['unit_requires_review']},
                        {'id':'b','platform':'B','name':'抖音真人点赞','unit':'','quantity':'1','quality':['unit_requires_review']},
                        {'id':'c','platform':'A','name':'快手视频播放','unit':'个'},
                        {'id':'d','platform':'B','name':'抖音评论点赞套餐','unit':'个'}])
    for product in products.values():product.category_name='互动业务'
    db.commit()
    return target,leaf,products


@pytest.mark.asyncio
async def test_organize_only_unlinked_matches_existing_groups_and_applies_without_daily_toggle(db,admin_client,monkeypatch):
    target,leaf,products=setup_targets(db,admin_client)
    configure(admin_client,auto_apply=False,enabled=False)
    submitted=[]
    monkeypatch.setattr(tasks.analyze_ai_data,'delay',lambda id:submitted.append(id))
    before={p.id:(p.name,p.price,p.category_name,p.unit,p.display_quantity) for p in products.values()}
    async def fake(connection,data):
        assert data['purpose']=='organize'
        assert {p['id'] for p in data['products']}=={products['a'].id,products['b'].id}
        directory=data['existing_comparison_groups']
        assert directory[0]['id']==target['id'] and directory[0]['category_id']==leaf['id']
        return {'summary':'分类固定测试','findings':[],'group_suggestions':[],
                'category_suggestions':[{'product_id':p['id'],'group_id':target['id'],'category_id':leaf['id'],
                    'category_path':'抖音 / 综合互动','group_name':'抖音点赞','reason':'业务与动作匹配现有综合互动映射'} for p in data['products']]}
    monkeypatch.setattr(service,'fetch_report',fake)
    response=admin_client.post('/api/ai-analysis/organize',json={'limit':20,'group_id':target['id']})
    assert response.status_code==202,response.text
    run=response.json();assert submitted==[run['id']]
    assert run['purpose']=='organize' and run['auto_apply'] is True
    assert (await service.execute_analysis(run['id']))['status']=='succeeded'
    db.expire_all()
    assert products['a'].canonical_id==products['b'].canonical_id==target['id']
    assert products['c'].canonical_id is None and products['d'].canonical_id is None
    assert before=={p.id:(p.name,p.price,p.category_name,p.unit,p.display_quantity) for p in products.values()}
    assert db.get(AISettings,1).auto_apply is False and db.get(AISettings,1).enabled is False
    details=admin_client.get(f"/api/ai-analysis/runs/{run['id']}").json()
    assert details['organized_count']==2 and details['pending_count']==0
    assert details['pending_product_count']==0
    assert db.scalar(select(func.count()).select_from(CanonicalProduct))==1
    repeated=admin_client.post('/api/ai-analysis/organize',json={'limit':20,'group_id':target['id']})
    assert repeated.status_code==409


def test_organize_refuses_unknown_targets_and_respects_permission(admin_client,db):
    configure(admin_client)
    assert admin_client.post('/api/ai-analysis/organize',json={'limit':101}).status_code==422
    assert admin_client.post('/api/ai-analysis/organize',json={'group_id':999}).status_code==404
    assert admin_client.post('/api/ai-analysis/organize',json={}).status_code==409
    with viewer(admin_client) as client:
        assert client.post('/api/ai-analysis/organize',json={}).status_code==403


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['schema','json','reference'])
async def test_invalid_report_has_safe_precise_diagnostic_and_preserves_usage(kind):
    data={'products':[{'id':1}]}
    if kind=='json':content='not a valid json '+KEY
    elif kind=='schema':content=json.dumps({'summary':'ok','category_suggestions':[{'product_id':'secret-'+KEY}]})
    else:content=json.dumps({'summary':'ok','findings':[{'severity':'warning','title':'fixture','evidence':'fixture','product_ids':[999]}]})
    def handler(request):return httpx.Response(200,json={'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':content}]}],'usage':{'input_tokens':20,'output_tokens':30,'total_tokens':50}})
    with pytest.raises(service.AnalysisError) as result:
        await service.fetch_report(service.Connection('https://ai.example.org/v1','fixture',KEY,api_format='responses'),data,transport=httpx.MockTransport(handler))
    error=result.value
    assert error.usage['total_tokens']==50 and error.diagnostic
    assert KEY not in str(error.diagnostic) and KEY not in error.message
    assert error.code in ('AI_REPORT_SCHEMA_INVALID','AI_REPORT_JSON_INVALID','AI_REPORT_REFERENCE_INVALID')


def test_strict_schema_requires_all_fields_without_leaking_defaults():
    schema=strict_report_schema(service.AIReport.model_json_schema())
    assert set(schema['required'])==set(schema['properties'])
    for definition in schema['$defs'].values():
        assert set(definition['required'])==set(definition['properties'])
        assert definition['additionalProperties'] is False
    assert 'default' not in json.dumps(schema)
