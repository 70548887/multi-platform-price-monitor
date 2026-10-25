from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from acceptance_watch import build_summary
from verify_acceptance import main, review_file, review_records


BEGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)
DURATION = 48 * 3600
NOW = BEGIN + timedelta(hours=49)


def stamp(seconds):
    return (BEGIN + timedelta(seconds=seconds)).isoformat()


def fixture(*, missing=(), extra=(), incomplete=False, unknown=False, restart=False, hours=48):
    batches = [{"id": 1, "platform_id": 1, "status": "success", "scheduled_at": stamp(-120),
                "started_at": stamp(-120), "completed_at": stamp(-60), "products_seen": 100,
                "expected_total": 100, "error_count": 0}]
    for h in range(1, 49):
        if h not in missing:
            batches.append({"id": h + 1, "platform_id": 1, "status": "success",
                "scheduled_at": stamp(h * 3600 - 120), "started_at": stamp(h * 3600 - 120),
                "completed_at": stamp(h * 3600 - 60), "products_seen": 99 if incomplete and h == 24 else 100,
                "expected_total": None if unknown and h == 24 else 100, "error_count": 0})
    for index, second in enumerate(extra, start=101):
        batches.append({"id": index, "platform_id": 1, "status": "success", "scheduled_at": stamp(second),
            "started_at": stamp(second), "completed_at": stamp(second + 60), "products_seen": 100,
            "expected_total": 100, "error_count": 0})
    batches.sort(key=lambda b: b["completed_at"])
    records = [{"kind": "start", "at": stamp(0), "requested_seconds": DURATION,
                "interval_seconds": 300, "once": False}]
    for i in range(hours * 12 + 1):
        second = i * 300
        done = [b for b in batches if b["completed_at"] <= stamp(second)]
        records.append({"kind": "sample", "at": stamp(second), "ok": True,
            "health": {"status": "ok", "database": "ok", "redis": "ok"},
            "stats": {"batches": {"success": len(done), "partial": 0, "failed": 0, "auth_expired": 0},
                "latest_batches": copy.deepcopy(done[-100:]),
                "uptime_seconds": second - 24 * 3600 + 5 if restart and second >= 24 * 3600 else second + 1000,
                "platforms": [{"id": 1, "enabled": True, "auth_status": "connected", "interval_seconds": 3600,
                    "successful_batches": len(done), "last_success_at": done[-1]["completed_at"]}]}})
    if hours == 48:
        summary = build_summary(records[1:], DURATION, DURATION, False, False, 300)
        summary["at"] = stamp(DURATION)
        records.append(summary)
    return records


class IndependentReviewTests(unittest.TestCase):
    def assert_issue(self, records, issue):
        result = review_records(records, now=NOW)
        self.assertEqual(result["status"], "failed", result)
        self.assertFalse(result["full_48h_verified"])
        self.assertIn(issue, result["issue_codes"])
        return result

    def test_normal_48_batches_verified_with_duration_statistics(self):
        result = review_records(fixture(), now=NOW)
        self.assertEqual(result["status"], "verified", result)
        self.assertEqual(result["new_successful_batches"], 48)
        self.assertEqual(result["batch_duration_seconds"], {"count": 48, "min": 60.0, "max": 60.0})

    def test_five_original_false_positives_are_rejected(self):
        cases = [
            ({"missing": (24,)}, "platform_collection_gap"),
            ({"missing": (12, 36), "extra": (11 * 3600 + 300, 35 * 3600 + 300)}, "platform_collection_gap"),
            ({"incomplete": True}, "successful_batch_incomplete_or_unknown_total"),
            ({"unknown": True}, "successful_batch_incomplete_or_unknown_total"),
            ({"restart": True}, "api_uptime_regressed"),
        ]
        for options, issue in cases:
            with self.subTest(options=options):
                records = fixture(**options)
                self.assertTrue(records[-1]["full_48h_verified"])
                self.assertEqual(records[-1]["outcome"], "passed")
                self.assert_issue(records, issue)

    def test_short_observation_is_in_progress(self):
        result = review_records(fixture(hours=1), now=BEGIN + timedelta(hours=1, minutes=1))
        self.assertEqual(result["status"], "in_progress")
        self.assertFalse(result["full_48h_verified"])

    def test_mature_run_without_summary_is_incomplete(self):
        result = review_records(fixture()[:-1], now=NOW)
        self.assertEqual(result["status"], "incomplete")

    def test_health_auth_scope_and_intervals_are_checked(self):
        mutations = [
            (lambda s: s["health"].update(database="error"), "health_dependency_not_ok"),
            (lambda s: s["health"].update(redis=None), "health_dependency_not_ok"),
            (lambda s: s["stats"]["platforms"][0].update(auth_status="missing"), "source_account_not_connected"),
            (lambda s: s["stats"].update(platforms=[]), "platform_acceptance_scope_changed"),
            (lambda s: s["stats"]["platforms"][0].update(enabled=False), "platform_acceptance_scope_changed"),
            (lambda s: s["stats"]["platforms"][0].update(interval_seconds=7200), "platform_acceptance_scope_changed"),
            (lambda s: s["stats"]["platforms"].append({**s["stats"]["platforms"][0], "id": 2}),
             "platform_acceptance_scope_changed"),
        ]
        for mutate, issue in mutations:
            with self.subTest(issue=issue):
                records = fixture()
                mutate(records[200])
                self.assert_issue(records, issue)

    def test_uptime_allows_two_second_jitter(self):
        records = fixture()
        records[200]["stats"]["uptime_seconds"] = records[199]["stats"]["uptime_seconds"] - 2
        self.assertEqual(review_records(records, now=NOW)["status"], "verified")
        records[201]["stats"]["uptime_seconds"] = records[200]["stats"]["uptime_seconds"] - 2
        self.assert_issue(records, "api_uptime_regressed")

    def test_baseline_bad_batch_is_excluded_but_new_bad_batch_fails(self):
        for baseline in (True, False):
            records = fixture()
            bad = {"id": 999, "platform_id": 1, "status": "partial", "scheduled_at": stamp(-300),
                   "started_at": stamp(-300), "completed_at": stamp(-200), "products_seen": 99,
                   "expected_total": 100, "error_count": 1}
            first = 1 if baseline else 2
            for sample in records[first:-1]:
                sample["stats"]["latest_batches"].append(copy.deepcopy(bad))
                sample["stats"]["batches"]["partial"] = 1
            if baseline:
                self.assertEqual(review_records(records, now=NOW)["status"], "verified")
            else:
                self.assert_issue(records, "new_unsuccessful_batch")

    def test_invalid_success_fields_and_timestamps_fail(self):
        mutations = [
            ({"products_seen": True}, "successful_batch_incomplete_or_unknown_total"),
            ({"expected_total": "100"}, "successful_batch_incomplete_or_unknown_total"),
            ({"products_seen": -1}, "successful_batch_incomplete_or_unknown_total"),
            ({"started_at": stamp(DURATION)}, "invalid_batch_time_order"),
            ({"scheduled_at": "bad"}, "invalid_batch_timestamp"),
            ({"completed_at": (NOW + timedelta(seconds=1)).isoformat()}, "future_timestamp"),
        ]
        for change, issue in mutations:
            with self.subTest(change=change):
                records = fixture()
                records[-2]["stats"]["latest_batches"][-1].update(change)
                self.assert_issue(records, issue)

    def test_summary_wallclock_and_health_coverage_fail(self):
        records = fixture()
        records[-1]["observed_seconds"] += 3600
        self.assert_issue(records, "summary_elapsed_wallclock_mismatch")
        records = fixture()
        del records[200]
        self.assert_issue(records, "health_sample_gap")

    def test_record_order_and_json_damage_never_pass(self):
        records = fixture()
        self.assertEqual(review_records([records[0], *records], now=NOW)["status"], "invalid")
        self.assertEqual(review_records([*records, records[1]], now=NOW)["status"], "invalid")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.jsonl"
            path.write_text('{"kind":"start"}\n{"broken":', encoding="utf-8")
            self.assertEqual(review_file(path, now=NOW)["status"], "invalid")

    def test_malformed_types_produce_a_report(self):
        records = fixture()
        records[-2]["stats"]["latest_batches"][-1]["status"] = []
        self.assert_issue(records, "invalid_batch_metadata")
        for requested, interval in ((10 ** 1000, 300), (DURATION, 1e-308)):
            records = fixture(hours=1)
            records[0].update(requested_seconds=requested, interval_seconds=interval)
            self.assertEqual(review_records(records, now=NOW)["status"], "invalid")

    def test_cli_preserves_input_and_writes_sidecar_with_exit_codes(self):
        for full in (True, False):
            with self.subTest(full=full), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "watch.jsonl"
                records = fixture()
                if not full:
                    current = datetime.now(timezone.utc)
                    records = [{"kind": "start", "at": current.isoformat(), "requested_seconds": DURATION,
                                "interval_seconds": 300, "once": False}]
                original = "\n".join(json.dumps(r) for r in records) + "\n"
                path.write_text(original, encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    code = main([str(path)])
                self.assertEqual(code, 0 if full else 2)
                self.assertEqual(path.read_text(encoding="utf-8"), original)
                review = json.loads(path.with_suffix(".review.json").read_text(encoding="utf-8"))
                self.assertEqual(review["status"], "verified" if full else "in_progress")
                self.assertNotIn("stats", review)
                self.assertFalse(list(Path(directory).glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
