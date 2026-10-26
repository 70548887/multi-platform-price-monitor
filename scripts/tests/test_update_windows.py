"""Exercise the real Windows host entry point against an empty temporary spool."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent


@unittest.skipUnless(os.name == 'nt', 'Windows updater ACL and PowerShell entry point')
class WindowsUpdaterEntryTests(unittest.TestCase):
    def test_real_sid_acl_and_once_entry_point(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ('scripts/update_host.py', 'scripts/package_release.py',
                             'backend/app/update_common.py', 'backend/app/update_config.py', 'backend/app/__init__.py'):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            secret = 'isolated-windows-updater-fixture-key' * 2
            (root / '.env').write_text('SESSION_SECRET=' + secret + '\n', encoding='utf-8')
            environment = os.environ.copy()
            environment.pop('SESSION_SECRET', None)
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                     '-File', str(SCRIPTS / 'update_host.ps1'), '-Root', str(root),
                                     '-PythonExe', sys.executable, '-Mode', 'native', '-Once'],
                                    capture_output=True, text=True, env=environment, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertTrue((root / 'data/update/host.json').is_file())
