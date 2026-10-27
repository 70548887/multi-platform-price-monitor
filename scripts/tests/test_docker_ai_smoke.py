"""Disposable smoke orchestration is mocked here; no Docker, DB or credentials."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

SCRIPTS=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SCRIPTS))
import docker_update_smoke as smoke


def fixture_tree(root, *, ai=True,dist=True,version='1.2.1'):
    files={'backend/app/main.py':'# fixture\n','backend/app/version.py':f'VERSION = "{version}"\n',
           'backend/requirements.txt':'httpx==0.28.1\n','frontend/package.json':json.dumps({'version':version}),
           'frontend/package-lock.json':json.dumps({'version':version,'packages':{'':{'version':version}}}),
           'docker-compose.yml':yaml.safe_dump({'services':{name:{} for name in (('api','worker','beat','web','ai-worker') if ai else ('api','worker','beat','web'))}}),
           'scripts/update_host.py':'# fixture updater\n','config/update.example.yaml':'update:\n  enabled: false\n'}
    if dist:files['frontend/dist/index.html']='fixture dist'
    for relative,value in files.items():
        path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value,encoding='utf-8')


class FingerprintTests(unittest.TestCase):
    def test_embedded_scripts_compile_and_ai_readback_does_not_import_new_models(self):
        for source in (smoke.DATA_FINGERPRINT_SCRIPT,smoke.AI_SEED_SCRIPT,smoke.AI_FINGERPRINT_SCRIPT):
            ast.parse(source)
        self.assertNotIn('from app.models',smoke.AI_FINGERPRINT_SCRIPT)
        for table in ('ai_analysis_runs','ai_analysis_batches','ai_suggestions','ai_applications','ai_daily_quotas','ai_product_checks'):
            self.assertIn(table,smoke.AI_FINGERPRINT_SCRIPT)
        self.assertIn('mapping_origin',smoke.AI_FINGERPRINT_SCRIPT)
        self.assertIn('enabled=False',smoke.AI_SEED_SCRIPT)
        self.assertIn('auto_apply=False',smoke.AI_SEED_SCRIPT)

    def test_ai_fingerprint_checks_seed_acknowledgement_and_sha_only(self):
        calls=[]
        def run(command,**kwargs):
            calls.append(command)
            return 'fixture_seeded' if command[-1]==smoke.AI_SEED_SCRIPT else 'a'*64
        self.assertEqual(smoke.ai_data_fingerprint(run,['compose'],Path('.'),seed=True),'a'*64)
        self.assertEqual(len(calls),2)
        self.assertTrue(all(command[1:6]==['exec','-T','api','python','-c'] for command in calls))
        with self.assertRaises(smoke.SmokeFailure):
            smoke.ai_data_fingerprint(lambda *a,**k:'unsafe raw output',['compose'],Path('.'))

    def test_old_source_checkout_without_dist_can_be_copied_for_container_build(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);old=root/'old';target=root/'copy'
            fixture_tree(old,ai=False,dist=False)
            smoke.copy_release_tree(old,target,require_dist=False)
            self.assertTrue((target/'frontend/package.json').is_file())
            self.assertFalse((target/'frontend/dist').exists())
            with self.assertRaises(ValueError):smoke.copy_release_tree(old,root/'bad',require_dist=True)


class OrchestrationTests(unittest.TestCase):
    def scenario(self, old_baseline):
        calls=[];fingerprints=[];updaters=[]
        class Runner:
            last_error=''
            def __init__(self,environment):self.environment=environment
            def __call__(self,arguments,**kwargs):calls.append(arguments);return ''
        class Updater:
            def __init__(self,root,data_dir,backup_dir,**kwargs):
                self.root,self.data_dir,self.secret=root,data_dir,kwargs['secret'];self.runs=0
                self.original_compose=(root/'docker-compose.yml').read_bytes();updaters.append(self)
            def _app_services(self):return tuple(yaml.safe_load((self.root/'docker-compose.yml').read_text())['services'])
            def _wait_health(self,*args):pass
            def _healthy(self,*args):return True
            def run_once(self):
                self.runs+=1
                if self.runs==1:
                    (self.root/'docker-compose.yml').write_bytes((self.root/'data/target-source/docker-compose.yml').read_bytes())
                    return {'status':'succeeded','backup_id':'fixture-backup'}
                (self.root/'docker-compose.yml').write_bytes(self.original_compose)
                return {'status':'rolled_back'}
        def ai_fingerprint(runner,compose,root,*,seed=False):
            fingerprints.append(seed);return 'b'*64
        def release(root,output,**kwargs):
            output.mkdir(parents=True,exist_ok=True);package=output/'fixture.tar.gz';package.write_bytes(b'fixture');return package,package
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source';baseline=Path(folder)/'baseline'
            fixture_tree(source,ai=True)
            fixture_tree(baseline,ai=False,dist=False)
            report={}
            with patch.object(smoke,'IsolatedRunner',Runner),patch.object(smoke,'HostUpdater',Updater),\
                 patch.object(smoke.subprocess,'check_output',return_value='a'*40),\
                 patch.object(smoke,'unused_loopback_port',return_value=28679),\
                 patch.object(smoke.os,'getuid',return_value=1000,create=True),\
                 patch.object(smoke.os,'getgid',return_value=1000,create=True),\
                 patch.object(smoke,'data_fingerprint',return_value='c'*64),\
                 patch.object(smoke,'ai_data_fingerprint',side_effect=ai_fingerprint),\
                 patch.object(smoke,'build_release',side_effect=release),\
                 patch.object(smoke,'read_signed_json',return_value={'version':'1.2.1'}):
                smoke.exercise(source,report,baseline_root=baseline if old_baseline else None)
            self.assertEqual(report['status'],'passed')
            self.assertTrue(report['ai_rollback_data_preserved'])
            self.assertTrue(report['environment_and_redis_preserved'])
            self.assertEqual(report['target_version'],'1.2.2')
            self.assertEqual(report['baseline_mode'],'old_release' if old_baseline else 'current_source')
            self.assertIn('ai-worker',report['upgraded_services'])
            self.assertEqual('ai-worker' in report['initial_services'],not old_baseline)
            self.assertEqual('ai-worker' in report['restored_services'],not old_baseline)
            self.assertEqual(fingerprints,[True,False] if old_baseline else [True,False,False])
            self.assertEqual(report['ai_tables_created_by_upgrade'],old_baseline)
            self.assertEqual(updaters[0].runs,2)
            self.assertEqual(report['cleanup'],'passed')
            self.assertTrue(any('down' in command and '--volumes' in command for command in calls))
            self.assertFalse(updaters[0].root.exists())
    def test_old_four_service_to_new_five_service_to_old_drill(self):self.scenario(True)
    def test_current_ai_data_is_preserved_across_upgrade_and_rollback(self):self.scenario(False)


class WorkflowTests(unittest.TestCase):
    def test_manual_workflow_uses_old_baseline_and_always_saves_sanitized_report(self):
        workflow=yaml.safe_load((SCRIPTS.parent/'.github/workflows/release.yml').read_text(encoding='utf-8'))
        steps=workflow['jobs']['test-and-package']['steps']
        baseline=next(step for step in steps if step.get('with',{}).get('path')=='.runtime/baseline')
        self.assertEqual(baseline['with']['ref'],'v1.2.1')
        commands=[step['run'] for step in steps if 'docker_update_smoke.py' in step.get('run','')]
        self.assertEqual(len(commands),2)
        command=next(command for command in commands if '--baseline-root' in command)
        self.assertIn('--baseline-root .runtime/baseline',command)
        self.assertIn('--report .runtime/docker-ai-smoke.json',command)
        self.assertTrue(any('--baseline-root' not in command and '--report .runtime/docker-ai-current-smoke.json' in command for command in commands))
        artifact=next(step for step in steps if step.get('with',{}).get('name')=='docker-ai-smoke-evidence')
        self.assertEqual(artifact['if'],'always()')
        self.assertIn("github.event_name == 'push'",workflow['jobs']['publish']['if'])


if __name__=='__main__':unittest.main()
