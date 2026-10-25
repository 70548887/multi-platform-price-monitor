"""Independently review a watch JSONL without changing the running observer."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

from acceptance_watch import build_summary


TERMINAL = {"success", "partial", "failed", "auth_expired"}
BAD = TERMINAL - {"success"}
STATUSES = TERMINAL | {"queued", "running"}


def integer(value: Any) -> bool:
    return type(value) is int and value >= 0


def positive_number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timezone")
    return result.astimezone(timezone.utc)


def review_records(records: list[Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issues: set[str] = set()
    report: dict[str, Any] = {
        "kind": "acceptance_review", "at": now.isoformat(), "status": "invalid",
        "full_48h_verified": False, "sample_count": 0, "platform_count": 0,
        "unique_batch_count": 0, "new_successful_batches": 0,
        "batch_duration_seconds": {"count": 0, "min": None, "max": None},
    }

    def finish(status: str) -> dict[str, Any]:
        report["status"] = status
        report["full_48h_verified"] = status == "verified"
        report["issue_codes"] = sorted(issues)
        return report

    if not records or any(not isinstance(row, dict) for row in records):
        issues.add("invalid_record_structure")
        return finish("invalid")
    starts = [row for row in records if row.get("kind") == "start"]
    summaries = [row for row in records if row.get("kind") == "summary"]
    if len(starts) != 1 or records[0].get("kind") != "start":
        issues.add("start_must_be_unique_and_first")
    if len(summaries) > 1 or (summaries and records[-1].get("kind") != "summary"):
        issues.add("summary_must_be_unique_and_last")
    if any(row.get("kind") not in ("start", "sample", "summary") for row in records):
        issues.add("unknown_record_kind")
    if issues:
        return finish("invalid")
    start = starts[0]
    requested = start.get("requested_seconds")
    interval = start.get("interval_seconds")
    if not positive_number(requested) or not positive_number(interval):
        issues.add("invalid_observation_configuration")
        return finish("invalid")
    try:
        minimum_samples = math.ceil(requested / interval) + 1
    except (ValueError, OverflowError):
        issues.add("invalid_observation_configuration")
        return finish("invalid")
    if requested < 48 * 3600 or start.get("once") is not False:
        issues.add("not_a_full_48h_observation")
    try:
        begin = instant(start.get("at"))
        end = begin + timedelta(seconds=requested)
    except (ValueError, TypeError, OverflowError):
        issues.add("invalid_start_timestamp")
        return finish("invalid")
    report.update(window_start=begin.isoformat(), window_end=end.isoformat())
    if begin > now:
        issues.add("future_timestamp")

    samples = [row for row in records if row.get("kind") == "sample"]
    report["sample_count"] = len(samples)
    sample_times: list[datetime] = []
    enabled_initial: dict[int, int] | None = None
    previous_uptime: int | None = None
    latest: dict[int, dict[str, Any]] = {}
    baseline_terminal: set[int] = set()
    new_bad: set[int] = set()
    valid_for_base = True
    health_tolerance = max(2.0, min(60.0, interval * 0.1))

    for index, row in enumerate(samples):
        try:
            at = instant(row.get("at"))
            if at > now:
                issues.add("future_timestamp")
            if at < begin or (sample_times and at <= sample_times[-1]):
                issues.add("invalid_sample_chronology")
            if sample_times and (at - sample_times[-1]).total_seconds() > interval + health_tolerance:
                issues.add("health_sample_gap")
            sample_times.append(at)
        except (ValueError, TypeError, OverflowError):
            issues.add("invalid_sample_timestamp")
            valid_for_base = False
        health, stats = row.get("health"), row.get("stats")
        if row.get("ok") is not True or not isinstance(health, dict) or any(
            health.get(key) != "ok" for key in ("status", "database", "redis")
        ):
            issues.add("health_dependency_not_ok")
        if not isinstance(stats, dict):
            issues.add("invalid_statistics")
            valid_for_base = False
            continue
        counters = stats.get("batches")
        if not isinstance(counters, dict) or any(not integer(counters.get(key)) for key in TERMINAL):
            issues.add("invalid_batch_counters")
            valid_for_base = False
        uptime = stats.get("uptime_seconds")
        if not integer(uptime):
            issues.add("invalid_uptime")
        else:
            if previous_uptime is not None and uptime + 2 < previous_uptime:
                issues.add("api_uptime_regressed")
            previous_uptime = max(previous_uptime or 0, uptime)
        platforms = stats.get("platforms")
        enabled: dict[int, int] = {}
        platform_ids: set[int] = set()
        if not isinstance(platforms, list):
            issues.add("invalid_platform_metadata")
            valid_for_base = False
            platforms = []
        for platform in platforms:
            if not isinstance(platform, dict) or not integer(platform.get("id")):
                issues.add("invalid_platform_metadata")
                valid_for_base = False
                continue
            platform_id = platform["id"]
            if platform_id in platform_ids or type(platform.get("enabled")) is not bool:
                issues.add("invalid_platform_metadata")
            platform_ids.add(platform_id)
            if platform.get("enabled") is not True:
                continue
            cadence = platform.get("interval_seconds")
            if not integer(cadence) or cadence == 0:
                issues.add("invalid_platform_interval")
                valid_for_base = False
                continue
            enabled[platform_id] = cadence
            if platform.get("auth_status") != "connected":
                issues.add("source_account_not_connected")
            if not integer(platform.get("successful_batches")):
                issues.add("invalid_platform_counter")
                valid_for_base = False
            if platform.get("last_success_at") is not None:
                try:
                    if instant(platform["last_success_at"]) > now:
                        issues.add("future_timestamp")
                except (ValueError, TypeError, OverflowError):
                    issues.add("invalid_last_success_timestamp")
                    valid_for_base = False
        if enabled_initial is None:
            enabled_initial = enabled
        elif enabled != enabled_initial:
            issues.add("platform_acceptance_scope_changed")
        batches = stats.get("latest_batches")
        if not isinstance(batches, list):
            issues.add("invalid_batch_metadata")
            continue
        for batch in batches:
            if (not isinstance(batch, dict) or not integer(batch.get("id"))
                    or not integer(batch.get("platform_id")) or not isinstance(batch.get("status"), str)
                    or batch["status"] not in STATUSES):
                issues.add("invalid_batch_metadata")
                continue
            batch_id = batch["id"]
            if index == 0 and batch["status"] in TERMINAL:
                baseline_terminal.add(batch_id)
            if batch["status"] in BAD and batch_id not in baseline_terminal:
                new_bad.add(batch_id)
            latest[batch_id] = batch
            for field in ("scheduled_at", "started_at", "completed_at"):
                if batch.get(field) is not None:
                    try:
                        if instant(batch[field]) > now:
                            issues.add("future_timestamp")
                    except (ValueError, TypeError, OverflowError):
                        issues.add("invalid_batch_timestamp")

    report["platform_count"] = len(enabled_initial or {})
    report["unique_batch_count"] = len(latest)
    report["new_bad_batch_count"] = len(new_bad)
    if not enabled_initial:
        issues.add("no_initial_enabled_platform")
    if new_bad:
        issues.add("new_unsuccessful_batch")
    completed: dict[int, list[datetime]] = {key: [] for key in enabled_initial or {}}
    durations: list[float] = []
    for batch_id, batch in latest.items():
        if batch["status"] != "success":
            continue
        expected, seen = batch.get("expected_total"), batch.get("products_seen")
        if not integer(expected) or not integer(seen) or expected != seen:
            issues.add("successful_batch_incomplete_or_unknown_total")
        if not integer(batch.get("error_count")) or batch["error_count"] != 0:
            issues.add("successful_batch_has_errors")
        try:
            scheduled, started, finished = [instant(batch.get(key)) for key in (
                "scheduled_at", "started_at", "completed_at")]
            if not scheduled <= started <= finished:
                issues.add("invalid_batch_time_order")
                continue
        except (ValueError, TypeError, OverflowError):
            issues.add("invalid_batch_timestamp")
            continue
        if begin <= finished <= end and batch_id not in baseline_terminal:
            report["new_successful_batches"] += 1
            durations.append((finished - started).total_seconds())
            if batch["platform_id"] in completed:
                completed[batch["platform_id"]].append(finished)
    if durations:
        report["batch_duration_seconds"] = {"count": len(durations), "min": min(durations), "max": max(durations)}
    if sample_times:
        report.update(first_sample_at=sample_times[0].isoformat(), last_sample_at=sample_times[-1].isoformat())
        if (sample_times[0] - begin).total_seconds() > health_tolerance:
            issues.add("initial_health_sample_gap")
    else:
        issues.add("no_operational_samples")
    finished_window = now >= end
    review_end = end if finished_window else min(end, sample_times[-1] if sample_times else begin)
    for platform_id, cadence in (enabled_initial or {}).items():
        points = [begin] + sorted(set(t for t in completed[platform_id] if t <= review_end)) + [review_end]
        if any((after - before).total_seconds() > cadence + max(180, cadence * 0.1)
               for before, after in zip(points, points[1:])):
            issues.add("platform_collection_gap")
    if not summaries:
        issues.add("original_summary_missing")
        return finish("incomplete" if finished_window else "in_progress")

    summary = summaries[0]
    if summary.get("outcome") != "passed" or summary.get("full_48h_verified") is not True:
        issues.add("original_summary_not_passed")
    elapsed = summary.get("observed_seconds")
    if not positive_number(elapsed):
        issues.add("invalid_summary_elapsed")
        valid_for_base = False
    try:
        summary_at = instant(summary.get("at"))
        if summary_at > now:
            issues.add("future_timestamp")
        if summary_at < begin or (sample_times and summary_at < sample_times[-1]):
            issues.add("invalid_summary_chronology")
        if positive_number(elapsed) and abs((summary_at - begin).total_seconds() - elapsed) > 2:
            issues.add("summary_elapsed_wallclock_mismatch")
    except (ValueError, TypeError, OverflowError):
        issues.add("invalid_summary_timestamp")
    if (summary.get("requested_seconds") != requested or summary.get("samples") != len(samples)
            or summary.get("successful_samples") != sum(row.get("ok") is True for row in samples)):
        issues.add("summary_metadata_mismatch")
    if len(samples) < minimum_samples:
        issues.add("too_few_health_samples")
    if not sample_times or sample_times[-1] < end - timedelta(seconds=health_tolerance):
        issues.add("health_window_not_covered")
    if not finished_window:
        issues.add("observation_window_not_elapsed")
    if valid_for_base:
        try:
            base = build_summary(samples, elapsed, requested, False, False, interval)
            if base["outcome"] != "passed" or base["full_48h_verified"] is not True:
                issues.add("base_summary_check_failed")
        except (ValueError, TypeError, KeyError, OverflowError):
            issues.add("base_summary_check_error")
    else:
        issues.add("base_summary_input_invalid")
    return finish("failed" if issues else "verified")


def review_file(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8-sig") as stream:
            records = [json.loads(line) for line in stream if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {"kind": "acceptance_review", "at": (now or datetime.now(timezone.utc)).isoformat(),
                "status": "invalid", "full_48h_verified": False, "issue_codes": [type(error).__name__]}
    return review_records(records, now=now)


def atomic_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    output = args.output or args.jsonl.with_suffix(".review.json")
    if output.resolve() == args.jsonl.resolve():
        parser.error("Review output must differ from the original JSONL")
    report = review_file(args.jsonl)
    atomic_report(output, report)
    print(json.dumps({"status": report["status"], "output": str(output.resolve()),
                      "full_48h_verified": report["full_48h_verified"],
                      "issue_codes": report["issue_codes"]}, ensure_ascii=False))
    return 0 if report["status"] == "verified" else 2 if report["status"] == "in_progress" else 1


if __name__ == "__main__":
    raise SystemExit(main())
