"""Backup command and archive publication tests; no database is modified."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup


class BackupTests(unittest.TestCase):
    def test_native_url_decodes_credentials_without_printable_password_repr(self):
        config = backup.parse_database_url("postgresql+psycopg://sample-user:p%40ss%3Aword@127.0.0.1:55432/price_monitor")
        self.assertEqual((config.host, config.port, config.database, config.username), ("127.0.0.1", 55432, "price_monitor", "sample-user"))
        self.assertEqual(config.password, "p@ss:word")
        self.assertNotIn("p@ss:word", repr(config))

    def test_native_url_rejects_remote_or_malformed_targets(self):
        for value in ("postgresql://u:p@remote.example/db", "https://localhost/db", "postgresql://u:p@127.0.0.1/", "postgresql://u:p@localhost:bad/db", "postgresql://u:p@localhost/db?host=remote.example"):
            with self.subTest(value=value), self.assertRaises(backup.BackupError):
                backup.parse_database_url(value)

    def test_native_dump_password_is_only_in_child_env_and_validated_before_publish(self):
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)

            def run(command, **kwargs):
                calls.append((command, dict(kwargs.get("env", {}))))
                self.assertNotIn("unit-secret-password", " ".join(command))
                self.assertEqual(kwargs["env"]["PGPASSWORD"], "unit-secret-password")
                self.assertFalse(list(directory.glob("*.dump")))
                if len(calls) == 1:
                    kwargs["stdout"].write(b"PGDMPtest archive")
                else:
                    self.assertEqual(command, ["pg_restore", "--list"])
                    self.assertEqual(kwargs["stdin"].read(5), b"PGDMP")
                return subprocess.CompletedProcess(command, 0)

            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://unit-user:unit-secret-password@127.0.0.1:55432/unit-db"}), patch.object(backup, "read_env", return_value={}), patch.object(backup, "postgres_tools", return_value=("pg_dump", "pg_restore")), patch.object(backup.subprocess, "run", side_effect=run):
                output = backup.create_backup(mode="native", directory=directory)
            self.assertTrue(output.is_file())
            self.assertEqual(output.read_bytes(), b"PGDMPtest archive")
            self.assertEqual(len(calls), 2)
            self.assertIn("--host=127.0.0.1", calls[0][0])
            self.assertIn("--port=55432", calls[0][0])
            self.assertEqual([entry.name for entry in directory.iterdir()], [output.name])

    def test_dump_or_validation_failure_does_not_publish_archive_or_raw_error(self):
        for failure in ("dump", "header", "list"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                directory, calls = Path(folder), []

                def run(command, **kwargs):
                    calls.append(command)
                    if len(calls) == 1:
                        kwargs["stdout"].write(b"broken" if failure == "header" else b"PGDMParchive")
                    code = 1 if failure == "dump" or (failure == "list" and len(calls) == 2) else 0
                    return subprocess.CompletedProcess(command, code, stderr=b"unit-private-credentials")

                with patch.dict(os.environ, {"DATABASE_URL": "postgresql://u:p@localhost/db"}), patch.object(backup, "read_env", return_value={}), patch.object(backup, "postgres_tools", return_value=("pg_dump", "pg_restore")), patch.object(backup.subprocess, "run", side_effect=run):
                    with self.assertRaises(backup.BackupError) as error:
                        backup.create_backup(mode="native", directory=directory)
                self.assertNotIn("unit-private-credentials", str(error.exception))
                self.assertFalse(list(directory.iterdir()))

    def test_docker_remains_default_and_output_has_no_secret_or_long_hash(self):
        output = io.StringIO()
        with patch.object(backup, "create_backup", return_value=Path("/verified/backup.dump")) as create, contextlib.redirect_stdout(output):
            self.assertEqual(backup.main([]), 0)
        self.assertEqual(create.call_args.kwargs["mode"], "docker")
        self.assertIn("pg_restore --list passed", output.getvalue())
        self.assertNotIn("SHA256", output.getvalue())

    def test_docker_also_validates_custom_archive_before_publication(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                kwargs["stdout"].write(b"PGDMParchive")
            else:
                self.assertEqual(command[-2:], ["pg_restore", "--list"])
                self.assertEqual(kwargs["stdin"].read(5), b"PGDMP")
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as folder, patch.object(backup, "read_env", return_value={}), patch.object(backup.shutil, "which", return_value="docker"), patch.object(backup.subprocess, "run", side_effect=run):
            result = backup.create_backup(mode="docker", directory=Path(folder))
            self.assertTrue(result.is_file())
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
