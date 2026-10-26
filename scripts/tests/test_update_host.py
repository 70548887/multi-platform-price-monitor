"""Host update drills use temporary trees and fake service runners only.

These tests do not connect to Docker, install dependencies, contact GitHub,
stop services, read the project's .env, or replace this checkout.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / "backend"))
from package_release import build_release, managed_path  # noqa: E402
import update_host
from update_host import HostUpdater, HostProbe, NoHostRedirect, UpdateError, process_lock  # noqa: E402
from app.update_common import read_signed_json, write_signed_json  # noqa: E402


SECRET = "local-fixture-signing-key-with-more-than-thirty-two-chars"


def application_tree(root: Path, version="1.0.0", *, dist=True):
    files = {
        "backend/app/version.py": f"VERSION = '{version}'\n",
        "backend/app/main.py": f"# fixture application {version}\n",
        "backend/requirements.txt": "httpx==0.28.1\n",
        "frontend/package.json": '{"name":"fixture","version":"1.0.0"}',
        "frontend/package-lock.json": '{"name":"fixture","lockfileVersion":3}',
        "frontend/src/main.ts": f"// fixture frontend {version}\n",
        "docker-compose.yml": "services: {}\n",
        "scripts/update_host.py": "# fixture host entry\n",
    }
    if dist:
        files["frontend/dist/index.html"] = "<h1>Fixture</h1>"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class FakeHost(HostUpdater):
    def __init__(self, root, *, fail_new_health=False, native=False):
        self.commands = []
        self.starts = []
        self.stops = 0
        self.fail_new_health = fail_new_health
        super().__init__(root, root / "data/update", root / "data/backups",
                         mode="native" if native else "compose", secret=SECRET,
                         runner=lambda *a, **k: "", probe=self.fake_probe,
                         drain_timeout=0.1, health_timeout=0.01, sleep=lambda _: None)

    def fake_probe(self, purpose, run_id):
        if purpose == "drain":
            return {"running": 0, "queued": 2, "ready": True}
        from package_release import version_from_source
        version = version_from_source(self.root)
        return {"status": "ok", "version": version, "worker": True, "beat": True,
                "database": True, "redis": not (self.fail_new_health and version == "1.1.0")}

    def _prepare_compose(self, job, stage):
        return {"images": {name: f"fixture-{name}:new" for name in ("api", "worker", "beat", "web")}}

    def _prepare_native(self, job, stage):
        path = self.data_dir / "envs" / "1.1.0-1" / "Scripts/python.exe"
        path.parent.mkdir(parents=True)
        path.write_text("fixture executable")
        nodes = stage / "frontend/node_modules"
        nodes.mkdir()
        (nodes / "fixture.txt").write_text("new dependency")
        return {"python": path.relative_to(self.root).as_posix()}

    def _current_images(self, job):
        return {name: f"fixture-{name}:old" for name in ("api", "worker", "beat", "web")}

    def _stop(self):
        self.stops += 1

    def _start(self, job, activation):
        self.starts.append(activation)

    def _services_healthy(self):
        return True


class ReleasePackageTests(unittest.TestCase):
    def test_only_allowlisted_files_publish_and_package_is_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application_tree(root)
            secrets = (".env", ".env.production", "config/update.yaml", "data/session.json", "var/quotes.json",
                       ".runtime/tokens.json", "backend/app/debug.log", "backend/app/__pycache__/a.pyc",
                       "frontend/src/.env", "scripts/tests/private.py", "frontend/node_modules/private.txt")
            for name in secrets:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("PRIVATE_FIXTURE_NEVER_PUBLISH")
            package, checksum = build_release(root, root / "out1")
            other, _ = build_release(root, root / "out2")
            self.assertEqual(package.read_bytes(), other.read_bytes())
            self.assertIn(hashlib.sha256(package.read_bytes()).hexdigest(), checksum.read_text())
            import tarfile
            with tarfile.open(package) as archive:
                names = set(archive.getnames())
                self.assertFalse(names.intersection(secrets))
                self.assertIn("frontend/dist/index.html", names)
                for member in archive:
                    self.assertNotIn(b"PRIVATE_FIXTURE_NEVER_PUBLISH", archive.extractfile(member).read())
                manifest = json.load(archive.extractfile("release-manifest.json"))
                self.assertEqual(set(manifest["files"]), names - {"release-manifest.json"})

    def test_version_mismatch_and_missing_dist_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application_tree(root, dist=False)
            with self.assertRaises(ValueError):
                build_release(root, root / "out", version="9.0.0")
            with self.assertRaises(ValueError):
                build_release(root, root / "out")

    def test_sensitive_or_escape_paths_are_not_managed(self):
        for path in ("../README.md", "/README.md", "C:/secret", "backend/app/a.log", "config/update.yaml",
                     ".env", "backend/app/.env.production", "frontend/node_modules/x.js", "scripts/tests/x.py"):
            self.assertFalse(managed_path(path), path)


class HostTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        application_tree(self.root)
        (self.root / ".env").write_text("PRIVATE_FIXTURE_ENV")
        private = self.root / "var/quotes.json"
        private.parent.mkdir()
        private.write_text("PRIVATE_FIXTURE_QUOTES")
        config = self.root / "config/update.yaml"
        config.parent.mkdir()
        config.write_text("PRIVATE_FIXTURE_UPDATE_CONFIG")
        self.host = FakeHost(self.root)
        source = self.root / "fixture-source"
        application_tree(source, "1.1.0")
        package, _ = build_release(source, self.host.data_dir / "packages/1")
        self.job = {"protocol": 1, "run_id": 1, "action": "apply", "current_version": "1.0.0",
                    "target_version": "1.1.0", "tag": "v1.1.0", "repository": "fixture/price-monitor",
                    "package": "packages/1/" + package.name, "sha256": hashlib.sha256(package.read_bytes()).hexdigest(), "auto_restart": True}
        write_signed_json(self.host.data_dir / "jobs/1.json", self.job, SECRET)

    def tearDown(self):
        self.temp.cleanup()

    def assert_private_preserved(self):
        self.assertEqual((self.root / ".env").read_text(), "PRIVATE_FIXTURE_ENV")
        self.assertEqual((self.root / "var/quotes.json").read_text(), "PRIVATE_FIXTURE_QUOTES")
        self.assertEqual((self.root / "config/update.yaml").read_text(), "PRIVATE_FIXTURE_UPDATE_CONFIG")

    def test_complete_update_and_repeat_does_not_reapply(self):
        result = self.host.run_once()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["version"], "1.1.0")
        self.assertEqual(self.host.stops, 1)
        self.assertEqual(len(self.host.starts), 1)
        self.assertFalse((self.host.data_dir / "maintenance.json").exists())
        self.assertFalse(self.host.journal_path(1).exists())
        self.assertIsNone(self.host.run_once())
        self.assertEqual(self.host.stops, 1)
        self.assert_private_preserved()

    def test_bad_sha_does_not_stop_or_mutate_current_app(self):
        self.job["sha256"] = "0" * 64
        result = self.host.process_job(self.job)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "PACKAGE_HASH_MISMATCH")
        self.assertEqual(self.host.stops, 0)
        self.assertIn("1.0.0", (self.root / "backend/app/version.py").read_text())
        self.assert_private_preserved()

    def test_unhealthy_update_automatically_restores_old_files_and_images(self):
        self.host.fail_new_health = True
        result = self.host.run_once()
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["version"], "1.0.0")
        self.assertEqual(len(self.host.starts), 2)
        self.assertTrue(all(image.endswith(":old") for image in self.host.starts[-1]["images"].values()))
        self.assertIn("1.0.0", (self.root / "backend/app/version.py").read_text())
        self.assertFalse((self.host.data_dir / "maintenance.json").exists())
        self.assert_private_preserved()

    def test_process_interruption_in_mutating_phase_restores_on_next_pass(self):
        original = self.host._replace_files
        def interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt("fixture interruption")
        with patch.object(self.host, "_replace_files", interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.host.run_once()
        self.assertTrue(self.host.journal_path(1).exists())
        resumed = self.host.run_once()
        self.assertEqual(resumed["status"], "rolled_back")
        self.assertIn("1.0.0", (self.root / "backend/app/version.py").read_text())
        self.assert_private_preserved()

    def test_manual_restart_never_reports_success_until_target_is_healthy(self):
        self.job["auto_restart"] = False
        write_signed_json(self.host.data_dir / "jobs/1.json", self.job, SECRET)
        result = self.host.run_once()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.host.starts, [])
        self.assertTrue((self.host.data_dir / "maintenance.json").exists())
        self.host.probe = lambda *args: {"status": "degraded"}
        self.assertEqual(self.host.run_once()["status"], "applied")
        self.host.probe = self.host.fake_probe
        self.assertEqual(self.host.run_once()["status"], "succeeded")
        self.assertFalse((self.host.data_dir / "maintenance.json").exists())

    def test_explicit_rollback_uses_verified_backup_and_retains_new_undo_backup(self):
        first = self.host.run_once()
        rollback = {"protocol": 1, "run_id": 2, "action": "rollback", "current_version": "1.1.0",
                    "target_version": "1.0.0", "tag": "v1.0.0", "repository": "fixture/price-monitor",
                    "backup_id": first["backup_id"], "auto_restart": True}
        write_signed_json(self.host.data_dir / "jobs/2.json", rollback, SECRET)
        result = self.host.run_once()
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["version"], "1.0.0")
        self.assertEqual(result["backup_version"], "1.1.0")
        self.assert_private_preserved()

    def test_tampered_backup_is_rejected_before_stop(self):
        first = self.host.run_once()
        backup_file = self.host.backup_dir / first["backup_id"] / "files/backend/app/version.py"
        backup_file.write_text("tampered")
        rollback = {"run_id": 2, "action": "rollback", "current_version": "1.1.0", "target_version": "1.0.0",
                    "tag": "v1.0.0", "repository": "fixture/price-monitor", "backup_id": first["backup_id"], "auto_restart": True}
        result = self.host.process_job(rollback)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "BACKUP_HASH_MISMATCH")
        self.assertEqual(self.host.stops, 1)

    def test_invalid_job_or_unsigned_job_rejected(self):
        for key, value in (("package", "packages/../private.tar.gz"), ("run_id", "1"), ("tag", "main"), ("target_version", "0.9.0")):
            with self.subTest(key=key):
                job = {**self.job, key: value}
                with self.assertRaises(UpdateError):
                    self.host.validate_job(job, "1.json")
        (self.host.data_dir / "jobs/1.json").write_text(json.dumps(self.job))
        self.assertIsNone(self.host.run_once())
        self.assertFalse((self.host.data_dir / "jobs/1.json").exists())
        self.assertEqual(len(list((self.host.data_dir / 'rejected_jobs').iterdir())), 1)
        result = read_signed_json(self.host.data_dir / 'results/1.json', SECRET)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'INVALID_HOST_JOB')

    def test_crash_before_terminal_publication_replays_only_completion(self):
        original = self.host.result
        def interrupt(job, status, *args, **kwargs):
            if status == 'succeeded':
                raise KeyboardInterrupt('fixture interruption before final result')
            return original(job, status, *args, **kwargs)
        with patch.object(self.host, 'result', interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.host.run_once()
        record = read_signed_json(self.host.journal_path(1), SECRET)
        self.assertEqual(record['phase'], 'completed')
        self.assertTrue((self.host.data_dir / 'maintenance.json').exists())
        stops, starts = self.host.stops, len(self.host.starts)
        resumed = self.host.run_once()
        self.assertEqual(resumed['status'], 'succeeded')
        self.assertEqual(self.host.stops, stops)
        self.assertEqual(len(self.host.starts), starts)
        self.assertFalse(self.host.journal_path(1).exists())
        self.assertFalse((self.host.data_dir / 'maintenance.json').exists())

    def test_crash_after_terminal_publication_does_not_restart(self):
        with patch.object(self.host, '_clear_maintenance', side_effect=KeyboardInterrupt('fixture cleanup crash')):
            with self.assertRaises(KeyboardInterrupt):
                self.host.run_once()
        self.assertEqual(read_signed_json(self.host.data_dir / 'results/1.json', SECRET)['status'], 'succeeded')
        stops, starts = self.host.stops, len(self.host.starts)
        self.assertEqual(self.host.run_once()['status'], 'succeeded')
        self.assertEqual(self.host.stops, stops)
        self.assertEqual(len(self.host.starts), starts)

    def test_crash_before_rollback_terminal_publication_does_not_reapply(self):
        self.host.fail_new_health = True
        original = self.host.result
        def interrupt(job, status, *args, **kwargs):
            if status == 'rolled_back':
                raise KeyboardInterrupt('fixture rollback terminal crash')
            return original(job, status, *args, **kwargs)
        with patch.object(self.host, 'result', interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.host.run_once()
        starts = len(self.host.starts)
        resumed = self.host.run_once()
        self.assertEqual(resumed['status'], 'rolled_back')
        self.assertEqual(resumed['version'], '1.0.0')
        self.assertEqual(len(self.host.starts), starts)
        self.assert_private_preserved()

    def test_bad_first_job_does_not_starve_valid_next_job(self):
        (self.host.data_dir / 'jobs/1.json').write_text('unsigned fixture input')
        old_package = self.host.data_dir / self.job['package']
        new_package = self.host.data_dir / 'packages/2' / old_package.name
        new_package.parent.mkdir(parents=True)
        new_package.write_bytes(old_package.read_bytes())
        next_job = {**self.job, 'run_id': 2, 'package': new_package.relative_to(self.host.data_dir).as_posix()}
        write_signed_json(self.host.data_dir / 'jobs/2.json', next_job, SECRET)
        result = self.host.run_once()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['run_id'], 2)

    def test_native_dependency_rollback_preserves_original_node_tree(self):
        nodes = self.root / "frontend/node_modules"
        nodes.mkdir()
        (nodes / "fixture.txt").write_text("old dependency")
        host = FakeHost(self.root, native=True, fail_new_health=True)
        result = host.run_once()
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual((nodes / "fixture.txt").read_text(), "old dependency")
        self.assert_private_preserved()

    def test_two_host_daemons_cannot_hold_os_lock(self):
        with process_lock(self.host.data_dir / "test.lock"):
            with self.assertRaises(UpdateError):
                with process_lock(self.host.data_dir / "test.lock"):
                    self.fail("duplicate lock acquired")


class ActualServiceProbeTests(unittest.TestCase):
    def test_compose_process_health_rejects_stale_api_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [{'Service': name, 'State': 'running', 'Health': 'healthy'}
                    for name in ('api', 'worker', 'beat', 'web')]
            runner = lambda *args, **kwargs: '\n'.join(json.dumps(row) for row in rows)
            host = HostUpdater(root, root / 'data/update', root / 'data/backups', mode='compose',
                               secret=SECRET, runner=runner,
                               probe=lambda *args: {'status': 'ok', 'version': '1.0.0', 'worker': True,
                                                    'beat': True, 'database': True, 'redis': True})
            self.assertTrue(host._healthy({'run_id': 1}, '1.0.0'))
            rows[2]['State'] = 'exited'
            rows[2]['Health'] = ''
            self.assertFalse(host._healthy({'run_id': 1}, '1.0.0'))

    def test_native_process_health_rejects_dead_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [{'service': name, 'running': True, 'pid': index + 1}
                    for index, name in enumerate(('api', 'worker', 'beat', 'web'))]
            host = HostUpdater(root, root / 'data/update', root / 'data/backups', mode='native',
                               secret=SECRET, runner=lambda *args, **kwargs: json.dumps(rows))
            self.assertTrue(host._services_healthy())
            rows[1]['running'] = False
            self.assertFalse(host._services_healthy())

    def test_host_proof_does_not_follow_any_redirect(self):
        request = Request('http://127.0.0.1:28671/api/update/host/health', headers={'X-Update-Host-Proof': 'fixture-proof'})
        handler = NoHostRedirect()
        for url in ('http://127.0.0.1:28671/elsewhere', 'https://other.example/health'):
            self.assertIsNone(handler.redirect_request(request, None, 302, 'Found', {}, url))

    def test_cli_loads_backup_directory_from_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            (root / 'config/update.yaml').write_text('update:\n  backup_dir: ./data/custom-backups\n')
            (root / '.env').write_text('SESSION_SECRET=' + SECRET + '\n')
            with patch.object(sys, 'argv', ['update_host.py', '--root', str(root), '--once']), \
                    patch.object(update_host, 'HostUpdater') as constructor:
                constructor.return_value.data_dir = root / 'data/update'
                constructor.return_value.run_once.return_value = None
                self.assertEqual(update_host.main(), 0)
                self.assertEqual(constructor.call_args.args[2], root.resolve() / 'data/custom-backups')


class DaemonCodeReloadTests(unittest.TestCase):
    def test_verified_code_change_reloads_after_unlock_and_preserves_arguments(self):
        for status in ('succeeded', 'rolled_back'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                application_tree(root)
                (root / '.env').write_text('SESSION_SECRET=' + SECRET + '\n')
                arguments = ['--root', str(root), '--mode', 'compose', '--poll-seconds', '7']
                held = {'value': False}
                @contextmanager
                def lock(path):
                    held['value'] = True
                    try:
                        yield
                    finally:
                        held['value'] = False
                def completed():
                    (root / 'scripts/update_host.py').write_text('# changed fixture updater\n')
                    return {'status': status}
                def execute(executable, argv):
                    self.assertFalse(held['value'])
                    self.assertEqual(executable, sys.executable)
                    self.assertEqual(argv, [sys.executable, '-u', str(root.resolve() / 'scripts/update_host.py'), *arguments])
                with patch.object(sys, 'argv', ['update_host.py', *arguments]), \
                        patch.object(update_host, 'HostUpdater') as constructor, \
                        patch.object(update_host, 'process_lock', lock), \
                        patch.object(update_host.os, 'execv', side_effect=execute) as execv:
                    constructor.return_value.data_dir = root / 'data/update'
                    constructor.return_value.run_once.side_effect = completed
                    self.assertEqual(update_host.main(), 0)
                    execv.assert_called_once()

    def test_unchanged_code_does_not_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application_tree(root)
            (root / '.env').write_text('SESSION_SECRET=' + SECRET + '\n')
            with patch.object(sys, 'argv', ['update_host.py', '--root', str(root)]), \
                    patch.object(update_host, 'HostUpdater') as constructor, \
                    patch.object(update_host.os, 'execv') as execv, \
                    patch.object(update_host.time, 'sleep', side_effect=KeyboardInterrupt):
                constructor.return_value.data_dir = root / 'data/update'
                constructor.return_value.run_once.return_value = {'status': 'succeeded'}
                self.assertEqual(update_host.main(), 0)
                execv.assert_not_called()

    def test_once_does_not_reload_changed_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application_tree(root)
            (root / '.env').write_text('SESSION_SECRET=' + SECRET + '\n')
            def completed():
                (root / 'scripts/update_host.py').write_text('# changed fixture updater\n')
                return {'status': 'succeeded'}
            with patch.object(sys, 'argv', ['update_host.py', '--root', str(root), '--once']), \
                    patch.object(update_host, 'HostUpdater') as constructor, \
                    patch.object(update_host.os, 'execv') as execv:
                constructor.return_value.data_dir = root / 'data/update'
                constructor.return_value.run_once.side_effect = completed
                self.assertEqual(update_host.main(), 0)
                execv.assert_not_called()

    def test_failed_pending_or_unfinished_journal_never_reload(self):
        for status, journal in (('failed', False), ('applied', False), ('succeeded', True)):
            with self.subTest(status=status, journal=journal), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                application_tree(root)
                (root / '.env').write_text('SESSION_SECRET=' + SECRET + '\n')
                def completed():
                    (root / 'scripts/update_host.py').write_text('# changed fixture updater\n')
                    if journal:
                        path = root / 'data/update/journal/1.json'
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text('fixture unfinished transaction')
                    return {'status': status}
                with patch.object(sys, 'argv', ['update_host.py', '--root', str(root)]), \
                        patch.object(update_host, 'HostUpdater') as constructor, \
                        patch.object(update_host.os, 'execv') as execv, \
                        patch.object(update_host.time, 'sleep', side_effect=KeyboardInterrupt):
                    constructor.return_value.data_dir = root / 'data/update'
                    constructor.return_value.run_once.side_effect = completed
                    self.assertEqual(update_host.main(), 0)
                    execv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
