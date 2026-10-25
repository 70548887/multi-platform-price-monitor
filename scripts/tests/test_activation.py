"""Activation sequencing tests use local fixtures, never a real source or acceptance run."""
from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import activate_and_watch as activation


class FixtureClient:
    def __init__(self, *, active=None, status="success"):
        self.active = active
        self.status = status
        self.calls = []

    def authenticated_request(self, path, *, method="GET", body=None):
        self.calls.append((method, path, body))
        if path == "/api/platforms":
            return {"items": [{"id": 1, "enabled": True, "auth_status": "connected", "active_rule_id": self.active,
                               "base_url": "https://www.xiaohong66.com"}]}
        if path == "/api/platforms/1/rules":
            return {"items": [{"id": 10, "version": 1, "status": "draft"},
                              {"id": 11, "version": 2, "status": "draft"}]}
        if path.endswith("/test"):
            return {"ok": True, "errors": [], "products": [
                {"source_id": "9585", "price": "0.007486", "display_quantity": "1000", "quality": []},
            ]}
        if path.endswith("/publish"):
            return {"id": 10, "status": "published"}
        if path.endswith("/collect"):
            return {"id": 99, "status": "queued"}
        if path.startswith("/api/runs?"):
            return {"items": [{"id": 99, "status": self.status, "products_seen": 12, "expected_total": 12}]}
        raise AssertionError(f"Unexpected request: {method} {path}")


class ActivationTests(unittest.TestCase):
    def test_different_source_cannot_activate_platform_one(self):
        class DifferentSource:
            def authenticated_request(self, path):
                return {"items": [{"id": 1, "enabled": True, "base_url": "https://different.example"}]}
        with self.assertRaises(activation.ActivationError):
            activation.configured_platform(DifferentSource())

    def test_default_version_one_is_chosen_instead_of_newer_user_draft(self):
        rule, publish = activation.choose_rule(FixtureClient(), {"active_rule_id": None})
        self.assertEqual(rule["id"], 10)
        self.assertTrue(publish)

    def test_existing_active_rule_is_reused(self):
        rule, publish = activation.choose_rule(FixtureClient(active=11), {"active_rule_id": 11})
        self.assertEqual(rule["id"], 11)
        self.assertFalse(publish)

    def test_invalid_sample_is_not_accepted(self):
        with self.assertRaises(activation.ActivationError):
            activation.validate_sample({"ok": True, "products": [{"source_id": "different"}]})
        with self.assertRaises(activation.ActivationError):
            activation.validate_sample({"ok": True, "products": [{
                "source_id": "9585", "price": "0", "display_quantity": "1", "quality": ["invalid_price"],
            }]})

    def test_zero_quantity_without_unit_accepts_real_variable_source_price(self):
        result = activation.validate_sample({"ok": True, "products": [{
            "source_id": "9585", "price": "0.007447", "display_quantity": "0", "unit": "",
            "quality": ["unit_requires_review"],
        }]})
        self.assertEqual(result["price"], "0.007447")
        self.assertEqual(result["display_quantity"], "0")

    def test_zero_quantity_with_declared_unit_and_negative_quantity_are_rejected(self):
        for quantity, unit in [("0", "人"), ("-1", "")]:
            with self.assertRaises(activation.ActivationError):
                activation.validate_sample({"ok": True, "products": [{
                    "source_id": "9585", "price": "0.007447", "display_quantity": quantity, "unit": unit,
                }]})

    def test_partial_batch_stops_before_starting_any_watcher(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FixtureClient(status="partial")
            args = Namespace(root=Path(directory), env_file=Path(directory)/".env", url="http://127.0.0.1:8000",
                             poll_seconds=10, connect_timeout_seconds=0, collection_timeout_seconds=7200)
            with patch.object(activation.subprocess, "Popen") as process:
                with self.assertRaises(activation.ActivationError):
                    activation.activate(client, args, {}, lambda phase, status: None)
                process.assert_not_called()
            self.assertIn(("POST", "/api/rules/10/publish", None), client.calls)
            self.assertNotIn(("POST", "/api/rules/11/publish", None), client.calls)

    def test_complete_baseline_launches_48h_but_failed_watcher_never_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FixtureClient(active=11)
            args = Namespace(root=Path(directory), env_file=Path(directory)/".env", url="http://127.0.0.1:8000",
                             poll_seconds=10, connect_timeout_seconds=0, collection_timeout_seconds=7200)
            phases = []
            fake = Namespace(pid=123, returncode=1, poll=lambda: 1)
            with patch.object(activation.subprocess, "Popen", return_value=fake) as process:
                with self.assertRaises(activation.ActivationError):
                    activation.activate(client, args, {}, lambda phase, status: phases.append(phase))
                command = process.call_args.args[0]
                self.assertEqual(command[command.index("--duration-hours")+1], "48")
            self.assertNotIn("verified_48h", phases)
            self.assertFalse(any(path.endswith("/publish") for _, path, _ in client.calls))

    def test_activation_state_is_complete_json_and_process_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"activation.json"
            activation.atomic_json(path, {"phase": "waiting", "status": "waiting"})
            activation.atomic_json(path, {"phase": "test", "status": "running"})
            self.assertEqual(json.loads(path.read_text())["phase"], "test")
            lock = Path(directory)/"activation.lock"
            with activation.activation_lock(lock):
                with self.assertRaises(OSError):
                    with activation.activation_lock(lock):
                        self.fail("Concurrent activation should not acquire the lock")
