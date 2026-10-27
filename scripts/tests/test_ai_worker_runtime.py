"""AI worker isolation/upgrade checks use fixtures, never live services or keys."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from update_host import APP_SERVICES, AI_APP_SERVICES, HostUpdater, UpdateError

spec = importlib.util.spec_from_file_location('ai_heartbeat_fixture_module', ROOT / 'backend/app/ai_worker.py')
heartbeat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(heartbeat)
SECRET = 'isolated-ai-worker-signing-key-with-more-than-32-characters'


class FakeRedis:
    def __init__(self):
        self.pulses = []
        self.ready = threading.Event()
        self.closed = False

    def set(self, key, value, *, ex):
        self.pulses.append((key, value, ex))
        if len(self.pulses) >= 3:
            self.ready.set()

    def close(self):
        self.closed = True


class HeartbeatTests(unittest.TestCase):
    def test_ai_pulses_continue_without_the_main_celery_loop(self):
        worker = SimpleNamespace(hostname='ai_worker@fixture',
                                 app=SimpleNamespace(conf=SimpleNamespace(broker_url='redis://fixture')))
        client = FakeRedis()
        step = heartbeat.AIWorkerHeartbeat(worker)
        self.assertTrue(step.include_if(worker))
        self.assertFalse(step.include_if(SimpleNamespace(hostname='collector@fixture')))
        with patch.object(heartbeat.redis.Redis, 'from_url', return_value=client), \
                patch.object(heartbeat, 'AI_HEARTBEAT_INTERVAL', 0.01):
            step.start(worker)
            # Simulate the solo worker main thread blocked in model I/O.
            self.assertTrue(client.ready.wait(timeout=1))
            step.stop(worker)
        self.assertTrue(client.closed)
        self.assertFalse(step.thread.is_alive())
        self.assertTrue(all(key == 'pm:ai:heartbeat' and ttl == 180 for key, _, ttl in client.pulses))
        self.assertLess(float(client.pulses[0][1]), float(client.pulses[-1][1]))

    def test_expired_future_missing_or_invalid_heartbeat_is_not_ready(self):
        self.assertTrue(heartbeat.heartbeat_fresh('990', now=1000))
        for value in (None, '819', '1001', 'NaN', 'inf', 'bad'):
            with self.subTest(value=value):
                self.assertFalse(heartbeat.heartbeat_fresh(value, now=1000))

    def test_deployments_consume_disjoint_queues_and_manage_ai_process(self):
        compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
        command = compose['services']['worker']['command']
        ai_command = compose['services']['ai-worker']['command']
        self.assertEqual(command[command.index('-Q') + 1], 'celery')
        self.assertEqual(ai_command[ai_command.index('-Q') + 1], 'ai')
        self.assertIn('--concurrency=1', ai_command)
        self.assertIn('--hostname=ai_worker@%h', ai_command)
        self.assertIn('ai-worker', compose['services']['ai-worker']['healthcheck']['test'])
        source = (ROOT / 'scripts/native.ps1').read_text(encoding='utf-8')
        self.assertIn("Start-Owned 'ai-worker'", source)
        self.assertIn("'-Q', 'ai'", source)
        self.assertIn("@('web', 'beat', 'ai-worker', 'worker', 'api')", source)
        self.assertIn("'app.tasks.analyze_ai_data': {'queue': 'ai'}",
                      (ROOT / 'backend/app/tasks.py').read_text(encoding='utf-8'))


class AIUpgradeCompatibilityTests(unittest.TestCase):
    def host(self, root, *, mode='compose', runner=lambda *a, **k: ''):
        return HostUpdater(root, root / 'data/update', root / 'data/backups', mode=mode,
                           secret=SECRET, runner=runner)

    def compose(self, root, ai):
        services = list(AI_APP_SERVICES if ai else APP_SERVICES)
        (root / 'docker-compose.yml').write_text(yaml.safe_dump({'services': {name: {} for name in services}}), encoding='utf-8')

    def test_health_requires_ai_only_for_a_version_that_declares_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [{'Service': name, 'State': 'running', 'Health': 'healthy'} for name in AI_APP_SERVICES]
            host = self.host(root, runner=lambda *a, **k: json.dumps(rows))
            self.compose(root, True)
            data = {'status': 'ok', 'version': '1.0.0', 'database': True, 'redis': True,
                    'worker': True, 'beat': True}
            host.probe = lambda *args: data
            self.assertFalse(host._healthy({'run_id': 1}, '1.0.0'))
            data['ai_worker'] = True
            self.assertTrue(host._healthy({'run_id': 1}, '1.0.0'))
            rows[-1]['Health'] = 'unhealthy'
            self.assertFalse(host._healthy({'run_id': 1}, '1.0.0'))
            self.compose(root, False)
            rows.pop()
            data.pop('ai_worker')
            self.assertTrue(host._healthy({'run_id': 1}, '1.0.0'))

    def test_preparation_and_start_follow_each_release_not_the_updater_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / 'stage'
            stage.mkdir()
            calls = []
            host = self.host(root, runner=lambda command, **kwargs: calls.append(command) or '')
            self.compose(root, False)
            self.compose(stage, True)
            activation = host._prepare_compose({'run_id': 1}, stage)
            self.assertEqual(set(activation['images']), set(AI_APP_SERVICES))
            self.assertIn('ai-worker', calls[-1])
            self.compose(root, True)
            host._start({'run_id': 1}, activation)
            self.assertIn('ai-worker', calls[-1])
            host._stop()
            self.assertIn('ai-worker', calls[-1])
            # A rollback restores the old compose before it starts its old images.
            self.compose(root, False)
            old = {'images': {name: f'fixture-{name}:old' for name in APP_SERVICES}}
            host._start({'run_id': 2}, old)
            self.assertNotIn('ai-worker', calls[-1])
            with self.assertRaises(UpdateError):
                host._start({'run_id': 3}, activation)

    def test_native_health_includes_ai_and_old_script_remains_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            native = root / 'scripts/native.ps1'
            native.write_text("Start-Owned 'ai-worker'\n", encoding='utf-8')
            rows = [{'service': name, 'running': True, 'pid': i + 1} for i, name in enumerate(AI_APP_SERVICES)]
            host = self.host(root, mode='native', runner=lambda *a, **k: json.dumps(rows))
            self.assertTrue(host._services_healthy())
            rows[-1]['running'] = False
            self.assertFalse(host._services_healthy())
            native.write_text('# old native startup\n', encoding='utf-8')
            rows.pop()
            self.assertTrue(host._services_healthy())


class ContainerProbeTests(unittest.TestCase):
    def test_ai_container_probe_checks_pid_identity_and_fresh_heartbeat(self):
        spec = importlib.util.spec_from_file_location('container_health_fixture', ROOT / 'scripts/container_health.py')
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        client = SimpleNamespace(ping=lambda: True, get=lambda key: '995')
        fixture_module = types.ModuleType('app.ai_worker')
        fixture_module.AI_HEARTBEAT_KEY = heartbeat.AI_HEARTBEAT_KEY
        fixture_module.heartbeat_fresh = lambda value: heartbeat.heartbeat_fresh(value, now=1000)
        with patch.object(sys, 'argv', ['container_health.py', 'ai-worker']), \
                patch.dict('os.environ', {'REDIS_URL': 'redis://fixture'}), \
                patch.object(probe.Path, 'read_text', return_value='123'), \
                patch.object(probe.Path, 'read_bytes', return_value=b'celery\x00worker\x00--hostname=ai_worker@fixture'), \
                patch.object(probe.os, 'kill') as kill, \
                patch('redis.Redis.from_url', return_value=client), \
                patch.dict(sys.modules, {'app.ai_worker': fixture_module}):
            probe.main()
            kill.assert_called_once_with(123, 0)
            client.get = lambda key: '800'
            with self.assertRaises(SystemExit):
                probe.main()
            client.get = lambda key: '995'
            with patch.object(probe.Path, 'read_bytes', return_value=b'celery\x00worker\x00--hostname=collector@fixture'):
                with self.assertRaises(SystemExit):
                    probe.main()


if __name__ == '__main__':
    unittest.main()
