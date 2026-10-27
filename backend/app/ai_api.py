"""AI settings and reports are administrator-only, including every source-data summary."""
import json
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, or_
from sqlalchemy.orm import Session

from .ai_analysis import AnalysisError, build_snapshot, create_analysis, dispatch_analysis, run_dict,settings_connection
from .ai_config import public_ai_settings, save_ai_settings
from .db import get_db
from .models import AISettings, AIAnalysisRun, AIAnalysisBatch, AISuggestion, AIApplication, utcnow
from .security import admin_user, audit
from fastapi import Query
from pydantic import BaseModel, Field, ConfigDict
from typing import Annotated
from datetime import date,datetime,time,timedelta
from sqlalchemy.dialects.postgresql import JSONB
from .ai_jobs import run_progress, stop_run
from .ai_overview import overview
from .ai_analysis import BEIJING

router = APIRouter(prefix='/api/ai-analysis')


@router.get('/overview')
def get_overview(db:Session=Depends(get_db),user=Depends(admin_user)):
    return overview(db)


@router.get('/config')
def configuration(db:Session=Depends(get_db),user=Depends(admin_user)):
    return public_ai_settings(db.get(AISettings,1))


@router.post('/connection-check')
async def connection_check(db:Session=Depends(get_db),user=Depends(admin_user)):
    from . import ai_diagnostics
    from .tasks import redis_client
    import secrets
    try:
        connection=settings_connection(db,db.get(AISettings,1))
    except AnalysisError as exc:
        return JSONResponse({'detail':exc.message,'error_code':exc.code},status_code=409)
    revision=db.get(AISettings,1).revision
    owner=secrets.token_hex(12)
    lock='pm:ai:connection-check'
    try:
        rc=redis_client()
        acquired=rc.set(lock,owner,nx=True,ex=45)
    except Exception:
        raise HTTPException(503,'连接检查暂不可用，请检查 Redis 服务') from None
    if not acquired:
        raise HTTPException(429,'已有连接检查正在进行，请稍后再试')
    db.rollback()
    try:
        result=await ai_diagnostics.check_connection(connection,revision)
        current=db.get(AISettings,1)
        if not current or current.revision!=revision:
            return {'status':'warning','authenticated':None,'model_available':None,'models':[],
                    'models_truncated':False,'latency_ms':result['latency_ms'],'checked_at':result['checked_at'],
                    'config_revision':revision,'error_code':'AI_SETTINGS_CHANGED',
                    'message':'检查期间配置已变化，请检查新配置。旧结果已丢弃。'}
        audit(db,user,'ai_connection_check',result['status']);db.commit()
        return result
    finally:
        try:
            rc.eval("if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) end return 0",1,lock,owner)
        except Exception:
            # The bounded lease expires even if Redis drops during HTTP I/O.
            pass


@router.put('/config')
async def configure(request:Request,db:Session=Depends(get_db),user=Depends(admin_user)):
    content=await request.body()
    if len(content)>16000:
        raise HTTPException(422,'AI 配置内容过大')
    try:
        payload=json.loads(content)
    except (ValueError,UnicodeError):
        raise HTTPException(422,'AI 配置须为 JSON 对象') from None
    row=save_ai_settings(db,payload)
    audit(db,user,'update_ai_settings','ai')
    db.commit()
    return public_ai_settings(row)


@router.post('/run',status_code=202)
def run_analysis(user=Depends(admin_user)):
    try:
        run=create_analysis(user.id)
        dispatch_analysis(run)
        return run
    except AnalysisError as exc:
        return JSONResponse({'detail':exc.message,'error_code':exc.code},status_code=409)


@router.get('/preview')
def preview(db:Session=Depends(get_db),user=Depends(admin_user)):
    settings=db.get(AISettings,1) or SimpleNamespace(sample_limit=100)
    return build_snapshot(db,settings,SimpleNamespace(trigger='manual',created_at=utcnow()))


@router.get('/runs')
def history(page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100),
            status:str='',platform_id:int|None=Query(None,gt=0),date_from:date|None=None,date_to:date|None=None,
            db:Session=Depends(get_db),user=Depends(admin_user)):
    query=select(AIAnalysisRun)
    if status: query=query.where(AIAnalysisRun.status==status)
    if date_from: query=query.where(AIAnalysisRun.created_at>=datetime.combine(date_from,time.min,tzinfo=BEIJING))
    if date_to: query=query.where(AIAnalysisRun.created_at<datetime.combine(date_to+timedelta(days=1),time.min,tzinfo=BEIJING))
    if platform_id:
        query=query.where(or_(
            AIAnalysisRun.id.in_(select(AIAnalysisBatch.run_id).where(
                AIAnalysisBatch.snapshot.cast(JSONB)['products'].contains([{'platform_id':platform_id}]))),
            (AIAnalysisRun.schema_version==1) & AIAnalysisRun.snapshot.cast(JSONB)['platforms'].contains([{'platform_id':platform_id}])))
    count=db.scalar(select(func.count()).select_from(query.subquery()))
    rows=db.scalars(query.order_by(AIAnalysisRun.id.desc()).offset((page-1)*page_size).limit(page_size)).all()
    return {'items':[{**run_dict(row),**run_progress(db,row)} for row in rows],'total':count,'page':page,'page_size':page_size}


@router.get('/runs/{run_id}')
def detail(run_id:int,db:Session=Depends(get_db),user=Depends(admin_user)):
    row=db.get(AIAnalysisRun,run_id)
    if row is None:
        raise HTTPException(404,'AI 分析记录不存在')
    return {**run_dict(row,detail=True),**run_progress(db,row,True)}


@router.post('/runs/{run_id}/stop')
def stop(run_id:int,db:Session=Depends(get_db),user=Depends(admin_user)):
    row=stop_run(db,run_id,user.id);db.commit()
    return {**run_dict(row),**run_progress(db,row)}


@router.post('/runs/{run_id}/retry',status_code=202)
def retry(run_id:int,user=Depends(admin_user)):
    try:
        run=create_analysis(user.id,retry_id=run_id)
        dispatch_analysis(run)
        return run
    except AnalysisError as exc:
        return JSONResponse({'detail':exc.message,'error_code':exc.code},status_code=409)


def paged(db,query,page,page_size,serialize):
    count=db.scalar(select(func.count()).select_from(query.subquery()))
    rows=db.scalars(query.offset((page-1)*page_size).limit(page_size)).all()
    return {'items':[serialize(db,row) for row in rows],'total':count,'page':page,'page_size':page_size}


@router.get('/suggestions')
def suggestions(status:str='',page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100),
                db:Session=Depends(get_db),user=Depends(admin_user)):
    from .ai_suggestions import suggestion_dict
    query=select(AISuggestion).order_by(AISuggestion.id.desc())
    if status: query=query.where(AISuggestion.status==status)
    return paged(db,query,page,page_size,suggestion_dict)


class RevisionInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision: Annotated[int,Field(strict=True,gt=0)]


class SuggestionEdit(RevisionInput):
    category_id: Annotated[int,Field(strict=True,gt=0)]
    group_id: Annotated[int,Field(strict=True,gt=0)]|None=None
    group_name:str=Field(min_length=1,max_length=200)


@router.patch('/suggestions/{suggestion_id}')
def edit_suggestion(suggestion_id:int,body:SuggestionEdit,db:Session=Depends(get_db),user=Depends(admin_user)):
    from .ai_suggestions import edit_suggestion as edit,suggestion_dict
    row=edit(db,suggestion_id,user.id,body.model_dump());db.commit()
    return suggestion_dict(db,row)


@router.post('/suggestions/{suggestion_id}/apply')
def apply_suggestion(suggestion_id:int,body:RevisionInput,db:Session=Depends(get_db),user=Depends(admin_user)):
    from .ai_suggestions import apply_suggestion as apply,suggestion_dict
    row=apply(db,suggestion_id,user.id,body.revision);db.commit()
    return suggestion_dict(db,row)


@router.post('/suggestions/{suggestion_id}/reject')
def reject_suggestion(suggestion_id:int,body:RevisionInput,db:Session=Depends(get_db),user=Depends(admin_user)):
    from .ai_suggestions import reject_suggestion as reject,suggestion_dict
    row=reject(db,suggestion_id,user.id,body.revision);db.commit()
    return suggestion_dict(db,row)


@router.get('/applications')
def applications(page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100),status:str='',
                 db:Session=Depends(get_db),user=Depends(admin_user)):
    from .ai_suggestions import application_dict
    query=select(AIApplication).order_by(AIApplication.id.desc())
    if status:query=query.where(AIApplication.status==status)
    return paged(db,query,page,page_size,application_dict)


@router.post('/applications/{application_id}/undo')
def undo(application_id:int,db:Session=Depends(get_db),user=Depends(admin_user)):
    from .ai_suggestions import undo_application,application_dict
    row=undo_application(db,application_id,user.id);db.commit()
    return application_dict(db,row)
