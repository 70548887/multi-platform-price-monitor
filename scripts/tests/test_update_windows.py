"""Exercise the real Windows host entry point against an empty temporary spool."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent


@unittest.skipUnless(os.name == 'nt', 'Windows updater ACL and PowerShell entry point')
class WindowsUpdaterEntryTests(unittest.TestCase):
    @contextmanager
    def isolated_entry(self, *, yaml=None, arguments=lambda root: []):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ('scripts/update_host.py', 'scripts/package_release.py',
                             'backend/app/update_common.py', 'backend/app/update_config.py', 'backend/app/__init__.py'):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            secret = 'isolated-windows-updater-fixture-key' * 2
            (root / '.env').write_text('SESSION_SECRET=' + secret + '\n', encoding='utf-8')
            if yaml is not None:
                (root / 'config').mkdir()
                (root / 'config/update.yaml').write_text(yaml, encoding='utf-8')
            environment = os.environ.copy()
            environment.pop('SESSION_SECRET', None)
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                     '-File', str(SCRIPTS / 'update_host.ps1'), '-Root', str(root),
                                     '-PythonExe', sys.executable, '-Mode', 'native', '-Once', *arguments(root)],
                                    capture_output=True, text=True, env=environment, timeout=30)
            self.assertNotIn(secret, result.stdout + result.stderr)
            yield root, result

    def test_real_sid_acl_and_once_entry_point(self):
        with self.isolated_entry() as (root, result):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / 'data/update/host.json').is_file())
            self.assertTrue((root / 'data/backups').is_dir())

    def test_yaml_backup_directory_is_used_and_protected(self):
        with self.isolated_entry(yaml='update:\n  backup_dir: ./data/custom backups\n') as (root, result):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / 'data/custom backups').is_dir())
            self.assertFalse((root / 'data/backups').exists())
            self.assertTrue((root / 'data/update/host.json').is_file())
            literal_path = str(root / 'data/custom backups').replace("'", "''")
            acl = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                  f"[IO.Directory]::GetAccessControl('{literal_path}').AreAccessRulesProtected"],
                                 capture_output=True, text=True, timeout=15)
            self.assertEqual(acl.returncode, 0, acl.stderr)
            self.assertEqual(acl.stdout.strip(), 'True')

    def test_explicit_backup_directory_overrides_yaml(self):
        with self.isolated_entry(yaml='update:\n  backup_dir: ./data/configured-backups\n',
                                 arguments=lambda root: ['-BackupDir', str(root / 'data/explicit backups')]) as (root, result):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / 'data/explicit backups').is_dir())
            self.assertFalse((root / 'data/configured-backups').exists())

    def test_explicit_backup_directory_outside_project_data_is_rejected(self):
        with self.isolated_entry(arguments=lambda root: ['-BackupDir', str(root / 'outside')]) as (root, result):
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'outside').exists())
            self.assertFalse((root / 'data/update/host.json').exists())

    def test_backup_directory_cannot_contain_runtime(self):
        with self.isolated_entry(arguments=lambda root: ['-BackupDir', str(root / 'data/update/backups')]) as (root, result):
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'data/update/backups').exists())
