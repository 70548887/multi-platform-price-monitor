"""Account initialization is exercised with temporary files and fixture values."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dotenv import dotenv_values


SCRIPT = Path(__file__).resolve().parents[1] / 'init_env.py'
FIELDS = ('ADMIN_USERNAME', 'ADMIN_PASSWORD', 'DEFAULT_USER_USERNAME', 'DEFAULT_USER_PASSWORD')


class InitialAccountEnvironmentTests(unittest.TestCase):
    def invoke(self, output, values=None):
        environment = os.environ.copy()
        for key in FIELDS:
            environment.pop(key, None)
        environment.update(values or {})
        return subprocess.run([sys.executable, str(SCRIPT), '--output', str(output), '--quiet-secret'],
                              env=environment, capture_output=True, text=True)

    def test_default_accounts_are_written_without_printing_passwords(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / '.env'
            result = self.invoke(output)
            self.assertEqual(result.returncode, 0)
            values = dotenv_values(output)
            self.assertEqual(tuple(values[key] for key in FIELDS), ('admin', 'admin888', 'user', 'user123'))
            self.assertNotIn('admin888', result.stdout)
            self.assertNotIn('user123', result.stdout)

    def test_environment_overrides_are_kept_and_existing_file_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / '.env'
            overrides = dict(zip(FIELDS, ('custom-owner', 'owner-fixture-pass', 'custom-viewer', 'viewer-fixture-pass')))
            result = self.invoke(output, overrides)
            self.assertEqual(result.returncode, 0)
            self.assertTrue(all(dotenv_values(output)[key] == value for key, value in overrides.items()))
            original = output.read_bytes()
            self.assertEqual(self.invoke(output).returncode, 1)
            self.assertEqual(output.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
