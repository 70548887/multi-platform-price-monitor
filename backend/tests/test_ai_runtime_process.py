"""Two actual solo worker processes prove collection progresses during model I/O."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import timedelta
from types import SimpleNamespace
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from celery import Celery
from sqlalchemy import func, select

from test_pipeline import (ROOT, isolated_database, clean_test_resources, db, admin_client,
                           platform_and_rule, run_for)
from test_comparison_catalogue import offers
from test_ai_analysis import configure
from app import ai_analysis, tasks
from app.models import AIAnalysisRun, AIAnalysisBatch, AIDailyQuota, CollectionRun, Observation, utcnow
from app.ai_jobs import day_key


class OwnedJob:
    """A Windows kill-on-close job owns only the workers created by this test."""
    def __init__(self):
        self.handle = None
        if os.name == 'nt':
            from ctypes import wintypes
            class Basic(ctypes.Structure):
                _fields_ = [('PerProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),
                            ('LimitFlags',wintypes.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),
                            ('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',wintypes.DWORD),
                            ('Affinity',ctypes.c_size_t),('PriorityClass',wintypes.DWORD),('SchedulingClass',wintypes.DWORD)]
            class IO(ctypes.Structure):
                _fields_ = [(field,ctypes.c_uint64) for field in ('ReadOperationCount','WriteOperationCount',
                            'OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
            class Extended(ctypes.Structure):
                _fields_ = [('BasicLimitInformation',Basic),('IoInfo',IO),('ProcessMemoryLimit',ctypes.c_size_t),
                            ('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
            self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            self.kernel.CreateJobObjectW.restype=wintypes.HANDLE
            self.kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
            self.kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
            self.kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
            self.kernel.CloseHandle.argtypes=[wintypes.HANDLE]
            self.handle=self.kernel.CreateJobObjectW(None,None)
            if not self.handle:raise ctypes.WinError(ctypes.get_last_error())
            limits=Extended();limits.BasicLimitInformation.LimitFlags=0x2000
            if not self.kernel.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
                self.close();raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle,int(process._handle)):
            process.terminate();process.wait(timeout=5)
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle);self.handle=None


def wait_until(predicate, timeout=20):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=predicate()
        if value:return value
        time.sleep(.05)
    raise AssertionError('Isolated worker fixture did not reach the expected state before timeout')


@contextmanager
def runtime_workers(prefix, evidence, queues):
    """Launch only test-owned solo workers, with a new broker/result client."""
    rc=tasks.redis_client()
    child_env=os.environ.copy()
    child_env['AI_RUNTIME_TEST_PREFIX']=prefix
    # Bypass the venv launcher, preserving this venv's installed libraries.
    interpreter=getattr(sys,'_base_executable',sys.executable)
    package_paths=[p for p in sys.path if p and 'site-packages' in p]
    child_env['PYTHONPATH']=os.pathsep.join([str(ROOT/'backend/tests'),str(ROOT/'backend'),*package_paths])
    client=Celery('ai-runtime-test-client',broker=tasks.config.REDIS_URL,backend=tasks.config.REDIS_URL)
    client.conf.update(task_serializer='json',result_serializer='json',accept_content=['json'],
        broker_transport_options={'global_keyprefix':prefix+':'},
        result_backend_transport_options={'global_keyprefix':prefix+':'})
    evidence.mkdir(parents=True,exist_ok=True)
    processes={};jobs={}
    names={queue:f"{'ai_worker' if queue=='ai' else 'collector'}@{prefix}" for queue in queues}
    try:
        with ExitStack() as resources:
            for queue,name in names.items():
                log=resources.enter_context((evidence/(queue+'.log')).open('w',encoding='utf-8'))
                job=OwnedJob();jobs[queue]=job
                process=subprocess.Popen([interpreter,'-m','celery','-A','ai_runtime_harness:celery_app',
                    'worker','--pool=solo','--concurrency=1','--loglevel=warning','--without-gossip',
                    '--without-mingle','--hostname',name,'-Q',queue],cwd=ROOT/'backend',env=child_env,
                    stdout=log,stderr=subprocess.STDOUT,
                    creationflags=(subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP) if os.name=='nt' else 0)
                processes[queue]=process;job.assign(process)
            def ready():
                assert all(p.poll() is None for p in processes.values()),'A test worker exited; inspect fixture logs'
                return len(client.control.ping(destination=list(names.values()),timeout=.5))==len(names)
            wait_until(ready,25)
            yield SimpleNamespace(client=client,rc=rc,processes=processes,jobs=jobs,names=names)
    finally:
        rc.set(prefix+':release','1',ex=10)
        live=[names[q] for q,p in processes.items() if p.poll() is None]
        if live:client.control.broadcast('shutdown',destination=live)
        for job in jobs.values():job.close()
        for process in processes.values():
            if process.poll() is None and os.name!='nt':process.terminate()
            process.wait(timeout=8)
        client.close()


def clean_fixture_keys(rc,prefix):
    for key in rc.scan_iter(prefix+'*'):rc.delete(key)


def test_actual_collection_finishes_while_ai_http_request_is_blocked(db,admin_client):
    offers(db,[{'platform':'A','id':'a'},{'platform':'B','id':'b'}])
    configure(admin_client)
    platform,rule=platform_and_rule(db,name='isolated-collector-concurrency')
    platform.next_due_at=utcnow()+timedelta(days=1)
    collection=run_for(db,platform,rule,status='queued');db.commit()
    analysis=ai_analysis.create_analysis()
    prefix='ai-runtime-test-'+uuid.uuid4().hex
    evidence=ROOT/'.runtime/ai-center-20261027/runtime-process'
    try:
        with runtime_workers(prefix,evidence,('celery','ai')) as workers:
            ai_result=workers.client.send_task('app.tasks.analyze_ai_data',args=[analysis['id']],queue='ai')
            wait_until(lambda:workers.rc.exists(prefix+':ai-started'))
            db.expire_all()
            assert db.get(AIAnalysisRun,analysis['id']).status=='running'
            assert db.scalar(select(AIAnalysisBatch).where(AIAnalysisBatch.run_id==analysis['id'])).status=='running'
            collect_result=workers.client.send_task('app.tasks.collect_platform',args=[collection.id],queue='celery')
            assert collect_result.get(timeout=20)['status']=='success'
            db.expire_all()
            assert db.get(AIAnalysisRun,analysis['id']).status=='running'
            saved=db.get(CollectionRun,collection.id)
            assert saved.status=='success' and saved.products_seen==saved.expected_total==1
            assert db.scalar(select(func.count()).select_from(Observation).where(Observation.run_id==collection.id))==1
            workers.rc.set(prefix+':release','1',ex=90)
            assert ai_result.get(timeout=20)['status']=='succeeded'
            db.expire_all()
            assert db.get(AIAnalysisRun,analysis['id']).checked_count>=2
            (evidence/'verification.json').write_text(json.dumps({
                'real_processes':2,'source_requests':'fixture_only','model_transport':'httpx.MockTransport',
                'collection_succeeded_while_ai_running':True,'collection_observations':1,
                'ai_completed_after_release':True,'test_database':'price_monitor_test','redis_database':15,
                'production_services_touched':False},indent=2),encoding='utf-8')
    finally:clean_fixture_keys(tasks.redis_client(),prefix)


def test_actual_ai_worker_crash_is_uncertain_and_redelivery_never_repeats_request(db,admin_client):
    offers(db,[{'platform':'A','id':'a'},{'platform':'B','id':'b'}])
    configure(admin_client)
    analysis=ai_analysis.create_analysis()
    prefix='ai-runtime-test-'+uuid.uuid4().hex
    evidence=ROOT/'.runtime/ai-center-20261027/runtime-crash'
    try:
        with runtime_workers(prefix,evidence/'before-crash',('ai',)) as original:
            original.client.send_task('app.tasks.analyze_ai_data',args=[analysis['id']],queue='ai')
            wait_until(lambda:original.rc.exists(prefix+':ai-started'))
            first_request_marker=original.rc.get(prefix+':ai-started')
            db.expire_all()
            assert db.get(AIAnalysisRun,analysis['id']).status=='running'
            batch=db.scalar(select(AIAnalysisBatch).where(AIAnalysisBatch.run_id==analysis['id']))
            assert batch.status=='running'
            assert db.get(AIDailyQuota,day_key(utcnow())).requests_used==1
            # Terminate only the job containing this exact test worker. The
            # model fixture gate is still closed, so no response was received.
            original.jobs['ai'].close()
            process=original.processes['ai']
            if os.name!='nt':process.terminate()
            process.wait(timeout=8)
            assert process.poll() is not None
            db.expire_all()
            run=db.get(AIAnalysisRun,analysis['id'])
            assert run.status=='running' and db.get(AIAnalysisBatch,batch.id).status=='running'
            run.heartbeat_at=utcnow()-timedelta(minutes=16)
            db.commit()
            # Exercise normal scheduler recovery, not GET report side effects.
            # Daily automation is disabled in the fixture, so this does not
            # enqueue any additional analysis or touch a real model endpoint.
            assert ai_analysis.scheduled_analysis()=={'queued':None}
            db.expire_all()
            assert db.get(AIAnalysisRun,analysis['id']).status=='failed'
            assert db.get(AIAnalysisBatch,batch.id).status=='uncertain'
            assert db.get(AIDailyQuota,day_key(utcnow())).requests_used==1
            with runtime_workers(prefix,evidence/'after-crash',('ai',)) as restarted:
                replay=restarted.client.send_task('app.tasks.analyze_ai_data',args=[analysis['id']],queue='ai')
                assert replay.get(timeout=15)=={'skipped':True}
                assert restarted.rc.get(prefix+':ai-started')==first_request_marker
                db.expire_all()
                assert db.get(AIDailyQuota,day_key(utcnow())).requests_used==1
                retry=ai_analysis.create_analysis(retry_id=analysis['id'])
                result=restarted.client.send_task('app.tasks.analyze_ai_data',args=[retry['id']],queue='ai')
                wait_until(lambda:restarted.rc.get(prefix+':ai-started')!=first_request_marker)
                db.expire_all()
                assert db.get(AIDailyQuota,day_key(utcnow())).requests_used==2
                restarted.rc.set(prefix+':release','1',ex=90)
                assert result.get(timeout=20)['status']=='succeeded'
                db.expire_all()
                assert db.get(AIAnalysisRun,retry['id']).checked_count==2
                (evidence/'verification.json').write_text(json.dumps({
                    'real_worker_terminated_during_mock_http':True,'charged_before_crash':1,
                    'recovery_status':'failed','batch_status':'uncertain',
                    'redelivery_skipped_without_another_request':True,'quota_after_redelivery':1,
                    'explicit_retry_completed':True,'quota_after_explicit_retry':2,
                    'test_database':'price_monitor_test','redis_database':15,
                    'production_services_touched':False},indent=2),encoding='utf-8')
    finally:clean_fixture_keys(tasks.redis_client(),prefix)
