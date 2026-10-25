"""Observe a real running installation. Never fabricate an elapsed acceptance period."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import http.cookiejar
import json
import math
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip("\"'")
    return result


class MonitorClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.csrf_token = ""
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path: str, body: dict[str, Any] | None = None, *, method: str | None = None,
                headers: dict[str, str] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with self.opener.open(request, timeout=self.timeout) as response:
            value = json.loads(response.read(2 * 1024 * 1024))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def login(self) -> None:
        response = self.request("/api/auth/login", {"username": self.username, "password": self.password})
        self.csrf_token = str(response.get("csrf_token") or "")

    def authenticated_request(self, path: str, *, method: str = "GET",
                              body: dict[str, Any] | None = None) -> dict[str, Any]:
        write = method.upper() not in ("GET", "HEAD", "OPTIONS")
        if write and not self.csrf_token:
            self.login()
        try:
            return self.request(path, body, method=method,
                                headers={"X-CSRF-Token": self.csrf_token} if write else None)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
        # The app session lasts 24 hours; renew it once when needed during a real 48-hour run.
        # Source-platform auth_status is returned unchanged and checked separately.
        self.login()
        return self.request(path, body, method=method,
                            headers={"X-CSRF-Token": self.csrf_token} if write else None)

    def stats(self) -> dict[str, Any]:
        return self.authenticated_request("/api/ops/stats")


def sample(client: MonitorClient) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": "sample", "at": utc_now(), "ok": True}
    for name, request in (("health", lambda: client.request("/api/health")), ("stats", client.stats)):
        try:
            payload = request()
            if name == "health":
                result[name] = {key: payload.get(key) for key in ("status", "database", "redis", "time")}
                if payload.get("status") != "ok":
                    result["ok"] = False
            else:
                # Keep operational evidence, never cookies or arbitrary remote response bodies.
                result[name] = {
                    key: payload.get(key)
                    for key in ("batches", "observations", "total_products", "uptime_seconds")
                }
                result[name]["latest_batches"] = [
                    {key: batch.get(key) for key in (
                        "id", "platform_id", "status", "scheduled_at", "started_at", "completed_at",
                        "products_seen", "expected_total", "error_count",
                    )}
                    for batch in payload.get("latest_batches", [])
                ]
                result[name]["platforms"] = [
                    {key: platform.get(key) for key in (
                        "id", "name", "enabled", "auth_status", "last_success_at", "interval_seconds",
                        "successful_batches",
                    )}
                    for platform in payload.get("platforms", [])
                ]
        except (urllib.error.URLError, OSError, ValueError, TypeError) as error:
            result["ok"] = False
            result[name] = {"error_type": type(error).__name__}
            if isinstance(error, urllib.error.HTTPError):
                result[name]["http_status"] = error.code
    return result


def build_summary(samples: list[dict[str, Any]], elapsed: float, requested: float,
                  once: bool, interrupted: bool, interval_seconds: float = 300) -> dict[str, Any]:
    good = [item for item in samples if item["ok"]]
    issues: list[str] = []
    if len(good) != len(samples):
        issues.append("One or more health/statistics samples failed")
    if not good:
        issues.append("No complete operational sample")
    if elapsed < requested and not once:
        issues.append("Requested observation time has not elapsed")
    if not once:
        minimum_samples = math.ceil(requested / interval_seconds) + 1
        if len(samples) < minimum_samples:
            issues.append(f"Too few health samples: {len(samples)}; need at least {minimum_samples}")
        for before, after in zip(samples, samples[1:]):
            gap = (datetime.fromisoformat(after["at"]) - datetime.fromisoformat(before["at"])).total_seconds()
            if gap < 0 or gap > interval_seconds * 2 + 60:
                issues.append("Health samples contain an observation gap or a reversed wall clock")
                break
    successful_growth = 0
    baseline = samples[0].get("stats", {}).get("batches", {}) if samples else {}
    final = samples[-1].get("stats", {}).get("batches", {}) if samples else {}
    if isinstance(baseline, dict) and isinstance(final, dict):
        successful_growth = max(0, int(final.get("success", 0)) - int(baseline.get("success", 0)))
    expected = max(1, math.floor(requested / 3600) - 1)
    if not once and successful_growth < expected:
        issues.append(f"Too few new successful hourly batches: {successful_growth}; need at least {expected}")
    if not once and isinstance(baseline, dict) and isinstance(final, dict):
        for status in ("partial", "failed", "auth_expired"):
            if int(final.get(status, 0)) > int(baseline.get(status, 0)):
                issues.append(f"New {status} collection batches occurred during observation")
    if not once and good:
        initial_platforms = {platform.get("id"): platform for platform in good[0]["stats"].get("platforms", [])}
        final_platforms = {platform.get("id"): platform for platform in good[-1]["stats"].get("platforms", [])}
        for platform_id, platform in initial_platforms.items():
            if platform.get("enabled") is False:
                continue
            last_platform = final_platforms.get(platform_id, {})
            if platform.get("successful_batches") is None or last_platform.get("successful_batches") is None:
                issues.append(f"Platform {platform_id} has no successful batch counter")
                continue
            interval = max(1, int(platform.get("interval_seconds") or 3600))
            required = max(1, math.floor(requested / interval) - 1)
            growth = int(last_platform["successful_batches"]) - int(platform["successful_batches"])
            if growth < required:
                issues.append(f"Platform {platform_id} has {growth} new successful batches; need at least {required}")
    for item in good:
        platforms = item["stats"].get("platforms", [])
        if not any(platform.get("enabled") is not False for platform in platforms):
            issues.append("No enabled platform was observed")
            break
        for platform in platforms:
            if platform.get("enabled") is False:
                continue
            if platform.get("auth_status") in ("expired", "auth_expired", "invalid", "error"):
                issues.append(f"Platform {platform.get('id')} requires session reconnection")
            last_success = platform.get("last_success_at")
            if last_success:
                try:
                    latest = datetime.fromisoformat(str(last_success).replace("Z", "+00:00"))
                    if latest.tzinfo is None:
                        latest = latest.replace(tzinfo=timezone.utc)
                    current = datetime.fromisoformat(item["at"])
                    max_age = max(7200, int(platform.get("interval_seconds") or 3600) * 2)
                    if (current - latest).total_seconds() > max_age:
                        issues.append(f"Platform {platform.get('id')} has stale successful collection")
                except ValueError:
                    issues.append(f"Platform {platform.get('id')} has invalid success timestamp")
            elif not once:
                issues.append(f"Platform {platform.get('id')} has no successful collection")
    unique_issues = sorted(set(issues))
    outcome = "interrupted" if interrupted else "sampled" if once else "failed" if unique_issues else "passed"
    return {
        "kind": "summary", "at": utc_now(), "outcome": outcome,
        "observed_seconds": round(elapsed, 3), "requested_seconds": requested,
        "samples": len(samples), "successful_samples": len(good),
        "new_successful_batches": successful_growth, "issues": unique_issues,
        "full_48h_verified": not once and not interrupted and not unique_issues and elapsed >= 48 * 3600,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--url", default="http://127.0.0.1:28671")
    parser.add_argument("--duration-hours", type=float, default=48)
    parser.add_argument("--interval-seconds", type=float, default=300)
    parser.add_argument("--timeout-seconds", type=float, default=20)
    parser.add_argument("--once", action="store_true", help="Take one sample; this never passes 48-hour acceptance")
    parser.add_argument("--username")
    parser.add_argument("--password-env", default="MONITOR_ADMIN_PASSWORD")
    parser.add_argument("--env-file", type=Path, default=root / ".env")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.duration_hours <= 0 or args.interval_seconds <= 0 or args.timeout_seconds <= 0:
        parser.error("Duration, interval, and timeout must be positive")
    parsed_url = urllib.parse.urlsplit(args.url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname or parsed_url.username:
        parser.error("Use an http(s) base URL without embedded credentials")
    env_file = read_env(args.env_file)
    username = args.username or os.getenv("ADMIN_USERNAME") or env_file.get("ADMIN_USERNAME", "admin")
    password = os.getenv(args.password_env) or os.getenv("ADMIN_PASSWORD") or env_file.get("ADMIN_PASSWORD", "")
    if not password:
        parser.error(f"Set {args.password_env}, ADMIN_PASSWORD, or configure --env-file")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or root / "var" / "acceptance" / f"watch-{stamp}.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    client = MonitorClient(args.url, username, password, args.timeout_seconds)
    started = time.monotonic()
    requested = args.duration_hours * 3600
    samples: list[dict[str, Any]] = []
    interrupted = False
    # Exclusive creation prevents accidentally mixing different observation runs.
    with output.open("x", encoding="utf-8", buffering=1) as stream:
        os.chmod(output, 0o600)

        def write(record: dict[str, Any]) -> None:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

        write({"kind": "start", "at": utc_now(), "url": args.url, "requested_seconds": requested,
               "interval_seconds": args.interval_seconds, "once": args.once})
        try:
            next_sample = started
            while True:
                item = sample(client)
                samples.append(item)
                write(item)
                print(f"{item['at']} sample={'ok' if item['ok'] else 'error'} count={len(samples)}", flush=True)
                if args.once or time.monotonic() - started >= requested:
                    break
                next_sample += args.interval_seconds
                deadline = min(started + requested, next_sample)
                time.sleep(max(0, deadline - time.monotonic()))
        except KeyboardInterrupt:
            interrupted = True
        summary = build_summary(samples, time.monotonic() - started, requested, args.once, interrupted, args.interval_seconds)
        write(summary)
    print(json.dumps({"output": str(output.resolve()), **summary}, ensure_ascii=False))
    if interrupted:
        return 130
    return 0 if (summary["outcome"] == "passed" or (args.once and not summary["issues"])) else 1


if __name__ == "__main__":
    raise SystemExit(main())
