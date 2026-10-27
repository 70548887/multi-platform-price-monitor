"""Read-only operations dashboard; task recovery belongs to the background scheduler."""
from datetime import timedelta
import time
from sqlalchemy import select,func
from .models import (AISettings,AIAnalysisRun,AIAnalysisBatch,AIDailyQuota,AIProductCheck,
                     AIApplication,AISuggestion,Product,Platform,utcnow)
from .ai_config import public_ai_settings
from .ai_analysis import BEIJING,run_dict
from .ai_jobs import day_key,run_progress
from .services import iso
from .ai_diagnostics import issue_description


def overview(db):
    from .ai_suggestions import organize_candidates
    now=utcnow();local=now.astimezone(BEIJING);day=day_key(now)
    settings=db.get(AISettings,1);config=public_ai_settings(settings)
    quota=db.get(AIDailyQuota,day)
    active=db.scalar(select(AIAnalysisRun).where(AIAnalysisRun.status.in_(('queued','running'))).order_by(AIAnalysisRun.id.desc()))
    current={**run_dict(active),**run_progress(db,active)} if active else None
    latest=db.scalar(select(AIAnalysisRun).order_by(AIAnalysisRun.id.desc()).limit(1))
    last_issue=None
    if latest and latest.status in ('failed','partial'):
        message,hint=issue_description(latest.error_code)
        last_issue={'id':latest.id,'error_code':latest.error_code,'message':message,'action_hint':hint,'created_at':iso(latest.created_at)}
    next_at=None
    schedule_reason='每日分析未启用'
    if settings and settings.enabled:
        hour,minute=map(int,settings.daily_time.split(':'))
        next_at=local.replace(hour=hour,minute=minute,second=0,microsecond=0)
        today_run=db.scalar(select(AIAnalysisRun.id).where(AIAnalysisRun.dedupe_key=='daily:'+day))
        exhausted=quota and (quota.products_used>=settings.daily_product_limit or quota.requests_used>=settings.daily_request_limit)
        schedule_reason='等待每日计划时间'
        if today_run or exhausted:
            next_at+=timedelta(days=1)
            schedule_reason='今日额度已用完' if exhausted else '今日定时任务已创建'
        elif active:
            next_at=None
            schedule_reason='等待当前任务结束，再检查每日计划'
        elif next_at<local:
            next_at=local
            schedule_reason='等待下一次定时检查（每分钟）'
    start=local.replace(hour=0,minute=0,second=0,microsecond=0)
    batches=list(db.scalars(select(AIAnalysisBatch).where(AIAnalysisBatch.request_day==day)))
    eligible=select(Product.id).join(Platform).where(Platform.enabled.is_(True))
    total=db.scalar(select(func.count()).select_from(eligible.subquery()))
    checked=db.scalar(select(func.count()).select_from(AIProductCheck).where(AIProductCheck.product_id.in_(eligible)))
    worker={'ready':False,'heartbeat_at':None}
    try:
        from .tasks import redis_client
        heartbeat=redis_client().get('pm:ai:heartbeat')
        if heartbeat:
            stamp=float(heartbeat)
            worker={'ready':0<=time.time()-stamp<180,'heartbeat_at':__import__('datetime').datetime.fromtimestamp(stamp,BEIJING).isoformat()}
    except Exception:
        pass
    return {'config':config,'current_run':current,'next_run_at':iso(next_at),'schedule_reason':schedule_reason,
        'today':{'day':day,'products_used':quota.products_used if quota else 0,'requests_used':quota.requests_used if quota else 0,
                 'checked_count':sum(len(b.product_ids) for b in batches if b.status=='succeeded'),
                 'automatic_applied':db.scalar(select(func.count()).select_from(AIApplication).where(AIApplication.mode=='auto',AIApplication.created_at>=start)),
                 'findings':sum(len(b.report.get('findings',[])) for b in batches)},
        'coverage':{'total':total,'checked':checked,'remaining':max(0,total-checked)},
        'pending_suggestions':db.scalar(select(func.count()).select_from(AISuggestion).where(AISuggestion.status=='pending')),
        'worker':worker,'last_issue':last_issue,
        'unorganized_count':db.scalar(select(func.count()).select_from(Product).where(Product.canonical_id.is_(None))),
        'organizable_count':len(organize_candidates(db))}
