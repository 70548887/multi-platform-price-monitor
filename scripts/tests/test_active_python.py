import importlib.util
from pathlib import Path
import sys
import unittest
import tempfile

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from active_python import active_python
from app.update_common import UpdateError, write_signed_json


class ActivePythonTests(unittest.TestCase):
    def test_only_signed_managed_runtime_is_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / 'data/update/envs/1.2.0-1/Scripts/python.exe'
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b'fixture-not-an-executable')
            state = root / 'data/update/activation.json'
            secret = 'fixture-update-session-secret-value' * 2
            write_signed_json(state, {'python': executable.relative_to(root).as_posix()}, secret)
            self.assertEqual(active_python(root, secret), executable)
            with self.assertRaises(UpdateError):
                active_python(root, 'wrong-fixture-secret' * 3)
            write_signed_json(state, {'python': 'backend/app/main.py'}, secret)
            with self.assertRaises(UpdateError):
                active_python(root, secret)
