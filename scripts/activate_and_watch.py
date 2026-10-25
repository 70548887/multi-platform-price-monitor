"""Wait for platform 1, validate sample 9585, collect a real baseline, then observe 48h."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
from urllib.parse import urlsplit

from acceptance_watch import MonitorClient, read_env

PLATFORM_ID = 1
SAMPLE_ID = "9585"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".activation-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def activation_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class ActivationError(Exception):
    pass


def configured_platform(client: MonitorClient) -> dict:
    platforms = client.authenticated_request("/api/platforms").get("items", [])
    selected = next((platform for platform in platforms if platform.get("id") == PLATFORM_ID), None)
    if selected is None:
        raise ActivationError("Configured platform 1 does not exist")
    source_url = urlsplit(str(selected.get("base_url") or ""))
    if source_url.hostname not in ("xiaohong66.com", "www.xiaohong66.com") or source_url.scheme != "https":
        raise ActivationError("Configured platform 1 is not the expected xiaohong66.com HTTPS source")
    if not selected.get("enabled"):
        raise ActivationError("Configured platform 1 is disabled")
    return selected


def choose_rule(client: MonitorClient, platform: dict) -> tuple[dict, bool]:
    rules = client.authenticated_request(f"/api/platforms/{PLATFORM_ID}/rules").get("items", [])
    active = platform.get("active_rule_id")
    if active:
        rule = next((item for item in rules if item.get("id") == active), None)
        if rule is None:
            raise ActivationError("Active rule does not belong to configured platform 1")
        return rule, False
    rule = next((item for item in rules if item.get("version") == 1), None)
    if rule is None or rule.get("status") != "draft":
        raise ActivationError("Platform 1 has no default version 1 draft to validate")
    return rule, True


def validate_sample(response: dict) -> dict:
    if not response.get("ok") or response.get("errors"):
        raise ActivationError("Rule preview failed; inspect the rule result in the website")
    sample = next((item for item in response.get("products", []) if str(item.get("source_id")) == SAMPLE_ID), None)
    if sample is None:
        raise ActivationError("Rule preview did not return source product 9585")
    try:
        price = Decimal(str(sample["price"]))
        quantity = Decimal(str(sample["display_quantity"]))
        if (not price.is_finite() or price < 0 or not quantity.is_finite()
                or quantity < 0 or (quantity == 0 and str(sample.get('unit') or '').strip())):
            raise ValueError
    except (KeyError, InvalidOperation, ValueError):
        raise ActivationError("Sample price or display quantity is invalid") from None
    if set(sample.get("quality") or []).intersection({"invalid_price", "invalid_display_quantity"}):
        raise ActivationError("Sample contains critical price-quality errors")
    return {key: sample.get(key) for key in ("source_id", "price", "display_quantity", "unit", "status", "quality")}


def activate(client: MonitorClient, args, state: dict, save) -> int:
    connected_at = time.monotonic()
    save("waiting_for_connection", "waiting")
    while True:
        platform = configured_platform(client)
        auth = platform.get("auth_status")
        state["platform_auth_status"] = auth
        if auth == "connected":
            break
        if auth not in ("missing", "pending", "disconnected"):
            raise ActivationError(f"Platform authentication requires action: {auth}")
        if args.connect_timeout_seconds and time.monotonic()-connected_at > args.connect_timeout_seconds:
            raise ActivationError("Timed out waiting for the configured platform connection")
        save("waiting_for_connection", "waiting")
        time.sleep(args.poll_seconds)

    rule, publish = choose_rule(client, platform)
    state.update(rule_id=rule["id"], rule_version=rule["version"], reusing_active_rule=not publish)
    save("testing_sample", "running")
    preview = client.authenticated_request(f"/api/rules/{rule['id']}/test", method="POST",
                                           body={"source_product_id": SAMPLE_ID})
    state["sample"] = validate_sample(preview)
    if publish:
        # Never activate an unrelated new draft, or overwrite an active rule chosen while waiting.
        current = configured_platform(client)
        if current.get("active_rule_id") not in (None, rule["id"]):
            raise ActivationError("The active rule changed during activation; stopped without publishing")
        save("publishing_default_rule", "running")
        client.authenticated_request(f"/api/rules/{rule['id']}/publish", method="POST")

    save("queuing_collection", "running")
    run = client.authenticated_request(f"/api/platforms/{PLATFORM_ID}/collect", method="POST")
    if not isinstance(run.get("id"), int):
        raise ActivationError("Collection API did not return a real batch id")
    state["run_id"] = run["id"]
    save("waiting_for_collection", "running")
    collection_started = time.monotonic()
    while True:
        rows = client.authenticated_request(f"/api/runs?platform_id={PLATFORM_ID}&page_size=200").get("items", [])
        result = next((item for item in rows if item.get("id") == run["id"]), None)
        if result is None:
            raise ActivationError("The recorded collection batch was not found")
        state["collection_status"] = result.get("status")
        state["products_seen"] = result.get("products_seen")
        state["expected_total"] = result.get("expected_total")
        if result.get("status") == "success":
            if not result.get("products_seen"):
                raise ActivationError("A successful baseline must contain at least one actual product")
            expected = result.get("expected_total")
            if expected is not None and result["products_seen"] != expected:
                raise ActivationError("Successful batch count does not match its expected catalogue size")
            break
        if result.get("status") not in ("queued", "running"):
            raise ActivationError(f"Collection ended without a complete baseline: {result.get('status')}")
        if time.monotonic()-collection_started > args.collection_timeout_seconds:
            raise ActivationError("Timed out waiting for the real collection batch")
        save("waiting_for_collection", "running")
        time.sleep(args.poll_seconds)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.root / "var" / "acceptance" / f"watch-{stamp}.jsonl"
    log_path = args.root / ".runtime" / "logs" / f"acceptance-{stamp}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(args.root / "scripts" / "acceptance_watch.py"), "--url", args.url,
               "--duration-hours", "48", "--interval-seconds", "300", "--env-file", str(args.env_file),
               "--output", str(output)]
    state.update(watcher_output=str(output), watcher_log=str(log_path))
    save("starting_48h_watch", "running")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with log_path.open("w", encoding="utf-8") as log:
        watcher = subprocess.Popen(command, cwd=args.root, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        state["watcher_pid"] = watcher.pid
        save("watching_48h", "watching")
        try:
            while watcher.poll() is None:
                try:
                    watcher.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    save("watching_48h", "watching")
        except KeyboardInterrupt:
            watcher.terminate()
            watcher.wait(timeout=10)
            raise
    state["watcher_exit_code"] = watcher.returncode
    summary = None
    if output.is_file():
        with output.open(encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                if record.get("kind") == "summary":
                    summary = record
    if watcher.returncode != 0 or not summary or not summary.get("full_48h_verified"):
        raise ActivationError("The real 48-hour watcher did not pass; inspect its recorded evidence")
    state["acceptance_summary"] = summary
    save("verified_48h", "complete")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--url", default="http://127.0.0.1:28671")
    parser.add_argument("--env-file", type=Path, default=root / ".env")
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--connect-timeout-seconds", type=float, default=10800, help="0 waits until connected")
    parser.add_argument("--collection-timeout-seconds", type=float, default=7200)
    parser.add_argument("--request-timeout-seconds", type=float, default=1800)
    args = parser.parse_args()
    args.root = root
    url = urlsplit(args.url)
    if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost") or url.path not in ("", "/") or url.query or url.username:
        parser.error("Activation is restricted to a local HTTP API without embedded credentials")
    if args.poll_seconds <= 0 or args.connect_timeout_seconds < 0 or args.collection_timeout_seconds <= 0 or args.request_timeout_seconds <= 0:
        parser.error("Polling/collection timeouts must be positive; connection timeout can be zero")
    env = read_env(args.env_file)
    username = os.getenv("ADMIN_USERNAME") or env.get("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD") or env.get("ADMIN_PASSWORD", "")
    if not password:
        parser.error("Admin credentials were not configured in --env-file or environment")
    state = {"pid": os.getpid(), "platform_id": PLATFORM_ID, "sample_id": SAMPLE_ID, "started_at": now()}
    state_path = root / ".runtime" / "activation.json"

    def save(phase, status):
        history = state.setdefault("phases", [])
        if not history or history[-1]["phase"] != phase or history[-1]["status"] != status:
            history.append({"phase": phase, "status": status, "at": now()})
        state.update(phase=phase, status=status, updated_at=now())
        atomic_json(state_path, state)

    try:
        with activation_lock(root / ".runtime" / "activation.lock"):
            try:
                return activate(MonitorClient(args.url.rstrip("/"), username, password, args.request_timeout_seconds), args, state, save)
            except KeyboardInterrupt:
                save(state.get("phase", "starting"), "interrupted")
                return 130
            except Exception as error:
                # Never persist arbitrary HTTP bodies or credentials from failed requests.
                state["error_type"] = type(error).__name__
                if isinstance(error, urllib.error.HTTPError):
                    state["http_status"] = error.code
                if isinstance(error, ActivationError):
                    state["error"] = str(error)
                save(state.get("phase", "starting"), "failed")
                print(f"Activation failed in {state.get('phase')}: {state['error_type']}", file=sys.stderr)
                return 1
    except OSError:
        parser.exit(1, "Another activation process owns the lock, or its local state is unavailable.\n")


if __name__ == "__main__":
    raise SystemExit(main())
