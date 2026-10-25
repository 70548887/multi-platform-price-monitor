from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from acceptance_watch import MonitorClient, build_summary, read_env, sample  # noqa: E402


def observation(successes: int, *, ok: bool = True, platform_successes: int | None = None, at=None) -> dict:
    now = (at or datetime.now(timezone.utc)).isoformat()
    return {
        "kind": "sample", "at": now, "ok": ok,
        "health": {"status": "ok"},
        "stats": {
            "batches": {"success": successes, "partial": 0, "failed": 0, "auth_expired": 0},
            "platforms": [{"id": 1, "enabled": True, "auth_status": "valid", "last_success_at": now,
                           "interval_seconds": 3600,
                           "successful_batches": successes if platform_successes is None else platform_successes}],
        },
    }


class AcceptancePolicyTests(unittest.TestCase):
    def test_once_cannot_verify_48_hours(self):
        result = build_summary([observation(100)], 1, 48 * 3600, True, False)
        self.assertEqual(result["outcome"], "sampled")
        self.assertFalse(result["full_48h_verified"])

    def test_shortened_or_interrupted_run_cannot_pass(self):
        result = build_summary([observation(1), observation(49)], 100, 48 * 3600, False, True)
        self.assertEqual(result["outcome"], "interrupted")
        self.assertFalse(result["full_48h_verified"])

    def test_global_growth_does_not_hide_one_stalled_platform(self):
        result = build_summary([observation(1, platform_successes=1), observation(100, platform_successes=1)],
                               48 * 3600, 48 * 3600, False, False)
        self.assertEqual(result["outcome"], "failed")
        self.assertTrue(any("Platform 1 has 0" in item for item in result["issues"]))

    def test_failed_sample_blocks_acceptance(self):
        result = build_summary([observation(1), observation(25, ok=False), observation(49)],
                               48 * 3600, 48 * 3600, False, False)
        self.assertEqual(result["outcome"], "failed")

    def test_partial_batch_blocks_acceptance(self):
        final = observation(49)
        final["stats"]["batches"]["partial"] = 1
        result = build_summary([observation(1), final], 48 * 3600, 48 * 3600, False, False)
        self.assertEqual(result["outcome"], "failed")

    def test_complete_evidence_is_accepted_by_policy(self):
        # Synthetic policy inputs validate the checker; this is not a real 48-hour run.
        started = datetime.now(timezone.utc) - timedelta(hours=48)
        records = [observation(1 + i // 12, at=started + timedelta(seconds=i * 300)) for i in range(577)]
        result = build_summary(records, 48 * 3600, 48 * 3600, False, False)
        self.assertEqual(result["outcome"], "passed")
        self.assertTrue(result["full_48h_verified"])

    def test_start_and_end_samples_cannot_replace_continuous_observation(self):
        started = datetime.now(timezone.utc) - timedelta(hours=48)
        result = build_summary([observation(1, at=started), observation(49)], 48 * 3600, 48 * 3600, False, False)
        self.assertEqual(result["outcome"], "failed")
        self.assertFalse(result["full_48h_verified"])


class SecretInitializationTests(unittest.TestCase):
    def test_generated_secrets_are_valid_and_existing_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / ".env"
            command = [sys.executable, str(SCRIPTS / "init_env.py"), "--output", str(output)]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            content = output.read_bytes()
            env = read_env(output)
            self.assertEqual(len(base64.urlsafe_b64decode(env["ENCRYPTION_KEY"])), 32)
            self.assertNotEqual(env["ADMIN_PASSWORD"], env["POSTGRES_PASSWORD"])
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(second.returncode, 1)
            self.assertEqual(output.read_bytes(), content)


class WatchHttpTests(unittest.TestCase):
    def test_expired_app_cookie_relogs_once_and_keeps_source_auth_failure(self):
        session = {"cookie": "none", "logins": 0, "source_expired": False}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, data, cookie=None):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if cookie:
                    self.send_header("Set-Cookie", f"session={cookie}; Path=/; HttpOnly")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def do_GET(self):
                if self.path == "/api/health":
                    self.respond(200, {"status": "ok", "database": "ok", "redis": "ok"})
                elif self.headers.get("Cookie") != f"session={session['cookie']}":
                    self.respond(401, {})
                else:
                    stats = observation(1)["stats"]
                    if session["source_expired"]:
                        stats["platforms"][0]["auth_status"] = "expired"
                    self.respond(200, stats)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                session["logins"] += 1
                session["cookie"] = f"session-{session['logins']}"
                self.respond(200, {"csrf_token": f"csrf-{session['logins']}"}, session["cookie"])

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = MonitorClient(f"http://127.0.0.1:{server.server_port}", "admin", "local-test", 2)
            self.assertTrue(sample(client)["ok"])
            self.assertEqual(session["logins"], 1)
            # Simulate the server expiring its 24-hour app cookie, without waiting 24 hours.
            session["cookie"] = "server-expired-cookie"
            session["source_expired"] = True
            refreshed = sample(client)
            self.assertTrue(refreshed["ok"])
            self.assertEqual(session["logins"], 2)
            self.assertEqual(client.csrf_token, "csrf-2")
            self.assertEqual(refreshed["stats"]["platforms"][0]["auth_status"], "expired")
            result = build_summary([refreshed], 1, 48 * 3600, True, False)
            self.assertTrue(any("requires session reconnection" in issue for issue in result["issues"]))
            self.assertFalse(result["full_48h_verified"])
        finally:
            server.shutdown()
            server.server_close()

    def test_once_uses_login_cookie_and_writes_real_sample(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, data, cookie=False):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if cookie:
                    self.send_header("Set-Cookie", "session=local-test; HttpOnly; Path=/")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def do_GET(self):
                if self.path == "/api/health":
                    self.respond(200, {"status": "ok", "database": "ok", "redis": "ok"})
                elif self.path == "/api/ops/stats":
                    if self.headers.get("Cookie") == "session=local-test":
                        self.respond(200, observation(1)["stats"])
                    else:
                        self.respond(401, {"detail": "login required"})
                else:
                    self.respond(404, {})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                if self.path == "/api/auth/login" and payload == {"username": "admin", "password": "local-test"}:
                    self.respond(200, {"ok": True}, cookie=True)
                else:
                    self.respond(401, {})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                env = Path(directory) / ".env"
                env.write_text("ADMIN_USERNAME=admin\nADMIN_PASSWORD=local-test\n")
                output = Path(directory) / "observation.jsonl"
                child_environment = os.environ.copy()
                for key in ('ADMIN_USERNAME', 'ADMIN_PASSWORD', 'MONITOR_ADMIN_PASSWORD'):
                    child_environment.pop(key, None)
                result = subprocess.run([
                    sys.executable, str(SCRIPTS / "acceptance_watch.py"),
                    "--url", f"http://127.0.0.1:{server.server_port}",
                    "--once", "--env-file", str(env), "--output", str(output),
                ], capture_output=True, text=True, env=child_environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                records = [json.loads(line) for line in output.read_text().splitlines()]
                self.assertEqual([record["kind"] for record in records], ["start", "sample", "summary"])
                self.assertTrue(records[1]["ok"])
                self.assertEqual(records[2]["outcome"], "sampled")
                self.assertFalse(records[2]["full_48h_verified"])
                self.assertNotIn("local-test", output.read_text())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
