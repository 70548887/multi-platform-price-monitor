"""Durable AI batches: budget is charged before sending; uncertain requests are never replayed."""
import asyncio
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select

from .db import SessionLocal
from .models import (AISettings, AIAnalysisRun, AIAnalysisBatch, AIDailyQuota, AIProductCheck,
                     AISuggestion,AIApplication,CanonicalProduct,AuditLog, Product, utcnow)
from .ai_selection import select_products
from .services import iso

ACTIVE = ('queued', 'running')
BATCH_ACTIVE = (*ACTIVE, 'reviewing')


def day_key(now):
    from .ai_analysis import BEIJING
    return now.astimezone(BEIJING).date().isoformat()


def quota_remaining(db, settings, now):
    quota = db.get(AIDailyQuota, day_key(now))
    return (max(0, settings.daily_product_limit-(quota.products_used if quota else 0)),
            max(0, settings.daily_request_limit-(quota.requests_used if quota else 0)))


def recover_abandoned(db):
    cutoff = utcnow()-timedelta(minutes=15)
    for run in db.scalars(select(AIAnalysisRun).where(AIAnalysisRun.status.in_(ACTIVE))):
        if (run.heartbeat_at or run.created_at) >= cutoff:
            continue
        for batch in db.scalars(select(AIAnalysisBatch).where(AIAnalysisBatch.run_id==run.id,
                                                           AIAnalysisBatch.status.in_(BATCH_ACTIVE))):
            if batch.status == 'reviewing':
                # The validated response was persisted before catalogue work.
                # Recovery creates pending proposals without another paid request.
                complete_review(db,run,batch,automatic=False)
                continue
            if batch.status == 'running':
                batch.status, batch.error_code = 'uncertain', 'AI_REQUEST_INTERRUPTED'
                batch.message = '请求被中断，是否已计费未知；不会自动再次请求'
            else:
                batch.status, batch.message = 'cancelled', '任务中断，尚未发送'
            batch.completed_at = utcnow()
        db.flush()
        batches=list(db.scalars(select(AIAnalysisBatch).where(AIAnalysisBatch.run_id==run.id)))
        if batches:
            finish_run(db,run)
        else:
            run.status,run.completed_at='failed',utcnow()
        run.stage,run.error_code = 'interrupted', None if run.status=='succeeded' else 'AI_TASK_INTERRUPTED'
        run.message = ('已恢复保存的报告与待审核建议，未再次调用模型' if run.status=='succeeded'
                       else '任务中断；已保存的报告继续保留，未发送批次需手动重试')


def create_run(actor_id=None, *, daily=False, now=None, retry_id=None,purpose='review',limit=None,group_id=None):
    from . import ai_analysis as service
    import uuid
    now = now or utcnow()
    with SessionLocal() as db:
        service.planning_lock(db)
        recover_abandoned(db)
        settings = db.get(AISettings,1)
        if daily and (not settings or not settings.enabled):
            db.commit()
            return None
        service.settings_connection(db,settings)
        day = day_key(now)
        if daily and (now.astimezone(service.BEIJING).strftime('%H:%M') < settings.daily_time or
                      db.scalar(select(AIAnalysisRun.id).where(AIAnalysisRun.dedupe_key=='daily:'+day))):
            db.commit()
            return None
        if db.scalar(select(AIAnalysisRun.id).where(AIAnalysisRun.status.in_(ACTIVE)).limit(1)):
            if daily:
                db.commit()
                return None
            raise HTTPException(409,'已有 AI 任务正在排队或执行')
        remaining, requests = quota_remaining(db,settings,now)
        if not remaining or not requests:
            raise service.AnalysisError('AI_DAILY_LIMIT','今日 AI 检查或请求额度已用完')
        if retry_id:
            previous = db.get(AIAnalysisRun,retry_id)
            if not previous or previous.status not in ('failed','partial','stopped'):
                raise HTTPException(409,'仅可重试失败、部分完成或已停止的任务')
            if previous.schema_version!=2:
                raise HTTPException(409,'旧版报告保留只读，请创建新的分析任务')
            purpose,limit,group_id=previous.purpose,previous.requested_limit,previous.target_group_id
        if purpose not in ('review','organize'):
            raise HTTPException(422,'分析目的无效')
        if purpose=='organize':
            from .ai_suggestions import organize_candidates
            limit=20 if limit is None else limit
            if type(limit) is not int or not 1<=limit<=100:
                raise HTTPException(422,'单次整理数量须为1到100件')
            if group_id is not None and not db.get(CanonicalProduct,group_id):
                raise HTTPException(404,'目标映射商品不存在')
            if not organize_candidates(db,group_id=group_id):
                raise HTTPException(409,'目前没有符合已建映射规则的未关联商品；请先建立对应映射或核对采集状态')
        run = AIAnalysisRun(schema_version=2,
            dedupe_key='daily:'+day if daily else 'manual:'+uuid.uuid4().hex,
            trigger='daily' if daily else 'manual', actor_id=actor_id, model=settings.model,
            api_format=settings.api_format,reasoning_effort=settings.reasoning_effort,max_output_tokens=settings.max_output_tokens,
            settings_revision=settings.revision, status='queued', stage='queued',
            purpose=purpose,requested_limit=limit,target_group_id=group_id,
            auto_apply=purpose=='organize' or settings.auto_apply,
            message='等待独立 AI 工作进程执行', created_at=now, parent_run_id=retry_id)
        db.add(run);db.flush()
        db.add(AuditLog(user_id=actor_id,action='queue_ai_analysis',target=str(run.id)))
        db.commit()
        return service.run_dict(run)


def batch_dict(batch, detail=False):
    result = {'id':batch.id,'ordinal':batch.ordinal,'status':batch.status,
              'product_count':len(batch.product_ids),'checked_count':len(batch.product_ids) if batch.status=='succeeded' else 0,
              'usage':batch.usage,'message':batch.message,'error_code':batch.error_code,
              'started_at':iso(batch.started_at),'completed_at':iso(batch.completed_at),'diagnostic':batch.diagnostic}
    if detail:
        result.update(snapshot=batch.snapshot,report=batch.report)
    return result


def run_progress(db, run, detail=False):
    if run.schema_version==1:
        sampled=run.snapshot.get('sample_count',0) if run.snapshot else 0
        result={'stage':run.status,'planned_count':sampled,
                'checked_count':sampled if run.status=='succeeded' else 0,'failed_count':0,
                'batch_count':0,'completed_batches':0,'stop_requested':False,'usage':None,
                'usage_partial':False}
        if detail:result['batches']=[]
        return result
    batches = list(db.scalars(select(AIAnalysisBatch).where(AIAnalysisBatch.run_id==run.id).order_by(AIAnalysisBatch.ordinal)))
    known = [b.usage for b in batches if b.usage]
    fields=[key for key in ('prompt_tokens','completion_tokens','total_tokens') if any(key in u for u in known)]
    usage = {key:sum(u[key] for u in known if key in u) for key in fields} if fields else None
    result = {'stage':run.stage,'planned_count':run.planned_count,'checked_count':run.checked_count,
              'failed_count':run.failed_count,'batch_count':len(batches),
              'completed_batches':sum(b.status not in BATCH_ACTIVE for b in batches),
              'stop_requested':run.stop_requested,'usage':usage,
              'usage_partial':len(known)<sum(b.started_at is not None for b in batches)
                              or any(any(key not in u for u in known) for key in fields)}
    suggestions=list(db.scalars(select(AISuggestion).where(AISuggestion.run_id==run.id)))
    applications=list(db.scalars(select(AIApplication).where(AIApplication.suggestion_id.in_([s.id for s in suggestions]),AIApplication.status=='applied')))
    applied_ids={p['id'] for item in applications for p in item.products}
    result['organized_count']=len(applied_ids)
    result['pending_count']=sum(s.status in ('pending','stale') for s in suggestions)
    result['pending_product_count']=len({p['id'] for s in suggestions if s.status in ('pending','stale') for p in s.products}-applied_ids)
    if detail:
        result['batches'] = [batch_dict(b,True) for b in batches]
    return result


def stop_run(db, run_id, actor_id):
    from .ai_analysis import planning_lock
    planning_lock(db)
    run = db.get(AIAnalysisRun,run_id)
    if not run:
        raise HTTPException(404,'任务不存在')
    if run.status in ACTIVE:
        run.stop_requested = True
        run.message = '已请求停止后续批次；正在发送的请求仍会完成并记录'
        if run.status=='queued':
            run.status,run.stage,run.completed_at='stopped','stopped',utcnow()
        db.add(AuditLog(user_id=actor_id,action='stop_ai_analysis',target=str(run_id)))
    return run


def finish_run(db, run):
    batches=list(db.scalars(select(AIAnalysisBatch).where(AIAnalysisBatch.run_id==run.id)))
    run.checked_count=sum(len(b.product_ids) for b in batches if b.status=='succeeded')
    run.failed_count=sum(len(b.product_ids) for b in batches if b.status in ('failed','uncertain'))
    if run.stop_requested:
        run.status='stopped'
    elif any(b.status in ('failed','uncertain','cancelled') for b in batches):
        run.status='partial' if run.checked_count else 'failed'
    else:
        run.status='succeeded'
    run.stage,run.completed_at=run.status,utcnow()
    reports=[b.report for b in batches if b.status=='succeeded']
    run.report={'summary':f'已成功检查 {run.checked_count} / {run.planned_count} 件商品；详见各批次证据与建议。',
                'findings':[f for r in reports for f in r.get('findings',[])],
                'category_suggestions':[],'group_suggestions':[]}
    failed = next((batch for batch in batches if batch.status in ('failed','uncertain')),None)
    run.message = ('没有待检查商品' if not batches else
                   failed.message if failed else
                   '已停止发送后续批次，已返回报告保留' if run.status=='stopped' else
                   run.message if run.error_code else '批次处理结束，请查看报告及建议处理记录')
    db.add(AuditLog(user_id=run.actor_id,action='ai_analysis_'+run.status,target=str(run.id)))


async def execute_run(run_id):
    from . import ai_analysis as service
    from .update_service import maintenance_active
    with SessionLocal() as db:
        service.planning_lock(db)
        recover_abandoned(db)
        run=db.get(AIAnalysisRun,run_id)
        if not run or run.status!='queued':
            db.commit();return {'skipped':True}
        try:
            settings=db.get(AISettings,1)
            validate_run(settings,run)
            if run.schema_version!=2:
                raise service.AnalysisError('AI_LEGACY_TASK','旧版任务不自动执行，请重新创建分析任务')
            if maintenance_active():
                raise service.AnalysisError('AI_MAINTENANCE','系统维护中，请稍后重新创建任务')
            service.settings_connection(db,settings)
            run.status,run.stage,run.started_at,run.heartbeat_at='running','planning',utcnow(),utcnow()
            db.commit()
            remaining,requests=quota_remaining(db,settings,utcnow())
            end=(run.created_at.astimezone(service.BEIJING).replace(hour=0,minute=0,second=0,microsecond=0)
                 if run.trigger=='daily' else run.created_at)
            retry_ids=None
            if run.parent_run_id:
                original=db.get(AIAnalysisRun,run.parent_run_id)
                original_batches=list(db.scalars(select(AIAnalysisBatch).where(AIAnalysisBatch.run_id==run.parent_run_id)))
                if original_batches:
                    completed_elsewhere=set(db.scalars(select(AIProductCheck.product_id).where(
                        AIProductCheck.last_checked_at>=original.created_at)))
                    retry_ids=[i for b in original_batches if b.status!='succeeded'
                               for i in b.product_ids if i not in completed_elsewhere]
            capacity=min(remaining,requests*50,run.requested_limit or remaining)
            ids=select_products(db,capacity,end-timedelta(days=1),end,retry_ids,
                                purpose=run.purpose,group_id=run.target_group_id)
            data=service.build_snapshot(db,settings,run,ids)
            # Frozen inputs are kept per batch; historical facts and current states have separate timestamps.
            run.snapshot={**data,'products':[]}
            run.planned_count=len(data['products'])
            for offset in range(0,len(data['products']),50):
                products=data['products'][offset:offset+50]
                db.add(AIAnalysisBatch(run_id=run.id,ordinal=offset//50+1,
                    product_ids=[p['id'] for p in products],snapshot={**data,'products':products,
                        'sample_count':len(products),'sample_truncated':data['total_products']>len(products)}))
            run.stage,run.message,run.heartbeat_at='analyzing','正在分批请求 AI 分析',utcnow()
            db.commit()
        except Exception as exc:
            db.rollback();run=db.get(AIAnalysisRun,run_id)
            run.status,run.stage,run.completed_at='failed','failed',utcnow()
            run.error_code=exc.code if isinstance(exc,service.AnalysisError) else 'AI_PLANNING_FAILED'
            run.message=exc.message if isinstance(exc,service.AnalysisError) else '检查计划未生成，请稍后重试'
            db.commit();return {'run_id':run_id,'status':'failed'}

    # One batch is committed before any HTTP request. Redelivery sees running, never replays it.
    while True:
        with SessionLocal() as db:
            service.planning_lock(db)
            run=db.get(AIAnalysisRun,run_id)
            if run.status!='running':
                return {'run_id':run.id,'status':run.status}
            batch=db.scalar(select(AIAnalysisBatch).where(AIAnalysisBatch.run_id==run.id,
                AIAnalysisBatch.status=='queued').order_by(AIAnalysisBatch.ordinal).limit(1))
            settings=db.get(AISettings,1)
            try:
                validate_run(settings,run)
                if maintenance_active():
                    run.stop_requested=True
                if run.stop_requested or not batch:
                    for pending in db.scalars(select(AIAnalysisBatch).where(AIAnalysisBatch.run_id==run.id,
                        AIAnalysisBatch.status=='queued')):
                        pending.status,pending.message,pending.completed_at='cancelled','未发送：任务已停止',utcnow()
                    finish_run(db,run);db.commit();return {'run_id':run.id,'status':run.status}
                connection=service.settings_connection(db,settings)
                remaining,requests=quota_remaining(db,settings,utcnow())
                if remaining<len(batch.product_ids) or not requests:
                    raise service.AnalysisError('AI_DAILY_LIMIT','今日额度不足，后续批次未发送')
                day=day_key(utcnow())
                quota=db.get(AIDailyQuota,day)
                if quota is None:
                    quota=AIDailyQuota(day=day,products_used=0,requests_used=0);db.add(quota)
                quota.products_used+=len(batch.product_ids);quota.requests_used+=1
                batch.status,batch.started_at,batch.request_day='running',utcnow(),day
                run.heartbeat_at=utcnow()
                data,batch_id=batch.snapshot,batch.id
                db.commit()
            except Exception as exc:
                db.rollback();run=db.get(AIAnalysisRun,run_id)
                run.error_code=exc.code if isinstance(exc,service.AnalysisError) else 'AI_BATCH_FAILED'
                run.message=exc.message if isinstance(exc,service.AnalysisError) else '批次准备失败'
                for pending in db.scalars(select(AIAnalysisBatch).where(AIAnalysisBatch.run_id==run.id,
                    AIAnalysisBatch.status=='queued')):
                    pending.status,pending.message,pending.completed_at='cancelled',run.message,utcnow()
                finish_run(db,run);db.commit();return {'run_id':run.id,'status':run.status}
        try:
            report=await asyncio.wait_for(service.fetch_report(connection,data),timeout=120)
            failure=None
        except Exception as exc:
            failure=exc
        with SessionLocal() as db:
            service.planning_lock(db)
            run=db.get(AIAnalysisRun,run_id);batch=db.get(AIAnalysisBatch,batch_id)
            if batch.status!='running' or run.status!='running':
                db.commit();return {'run_id':run_id,'status':run.status}
            if failure:
                batch.status='failed'
                batch.error_code=failure.code if isinstance(failure,service.AnalysisError) else 'AI_REQUEST_FAILED'
                batch.message=failure.message if isinstance(failure,service.AnalysisError) else '请求失败或超时，未自动重试'
                batch.diagnostic=failure.diagnostic if isinstance(failure,service.AnalysisError) else {}
                batch.usage=failure.usage if isinstance(failure,service.AnalysisError) else None
                run.error_code=batch.error_code
                for pending in db.scalars(select(AIAnalysisBatch).where(AIAnalysisBatch.run_id==run.id,
                    AIAnalysisBatch.status=='queued')):
                    pending.status,pending.message,pending.completed_at='cancelled','前一批次失败，未发送；可手动重试',utcnow()
            else:
                batch.report={k:v for k,v in report.items() if k!='_usage'}
                batch.usage=report.get('_usage')
                batch.status,batch.message='reviewing','模型报告已保存，正在整理建议'
                run.stage='reviewing'
            if failure:
                run.failed_count+=len(batch.product_ids)
            batch.completed_at=utcnow();run.heartbeat_at=utcnow()
            db.commit()
        if not failure:
            with SessionLocal() as db:
                service.planning_lock(db)
                run=db.get(AIAnalysisRun,run_id);batch=db.get(AIAnalysisBatch,batch_id)
                if run.status!='running' or batch.status!='reviewing':
                    db.commit();return {'run_id':run_id,'status':run.status}
                settings=db.get(AISettings,1)
                automatic=bool(settings and run.auto_apply and (run.purpose=='organize' or settings.auto_apply) and settings.revision==run.settings_revision
                               and not run.stop_requested and not maintenance_active())
                try:
                    complete_review(db,run,batch,automatic=automatic)
                    run.stage,run.heartbeat_at='analyzing',utcnow()
                    db.commit()
                except Exception:
                    db.rollback()
                    run=db.get(AIAnalysisRun,run_id)
                    run.message='模型报告已保存，建议整理中断，后台将恢复；未再次调用模型'
                    # Leave the sent receipt intact for the existing recovery path.
                    db.commit()
                    return {'run_id':run_id,'status':'running','recovery_pending':True}


def complete_review(db,run,batch,*,automatic=False):
    from .ai_suggestions import ingest_suggestions
    ingest_suggestions(db,run,batch,batch.report,auto_apply=automatic)
    for product in batch.snapshot.get('products',[]):
        if db.get(Product,product['id']) is None:
            continue
        checked=db.get(AIProductCheck,product['id'])
        if checked is None:
            checked=AIProductCheck(product_id=product['id']);db.add(checked)
        checked.fingerprint,checked.last_checked_at,checked.last_batch_id=product['fingerprint'],utcnow(),batch.id
    batch.status,batch.message,batch.completed_at='succeeded','分析报告与建议已保存',utcnow()
    run.checked_count+=len(batch.product_ids)


def validate_run(settings,run):
    from .ai_analysis import AnalysisError
    if not settings or settings.revision!=run.settings_revision:
        raise AnalysisError('AI_SETTINGS_CHANGED','配置已变化，后续批次未发送，请重新创建任务')
    if run.trigger=='daily' and not settings.enabled:
        raise AnalysisError('AI_AUTO_DISABLED','每日分析已关闭，后续批次未发送')
