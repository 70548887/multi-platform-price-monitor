"""Small container probes. Batch progress is checked separately by acceptance_watch.py."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import urllib.request


def main() -> None:
    mode = sys.argv[1]
    if mode == "api":
        with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=5) as response:
            if response.status != 200:
                raise SystemExit(1)
    elif mode in {"beat", "ai-worker"}:
        from redis import Redis

        pid_path = "/tmp/celerybeat.pid" if mode == 'beat' else '/tmp/ai-worker.pid'
        pid = int(Path(pid_path).read_text().strip())
        os.kill(pid, 0)
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
        expected = b'beat' if mode == 'beat' else b'ai_worker@'
        if b"celery" not in command or expected not in command:
            raise SystemExit("Unexpected managed process")
        client = Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=3, socket_timeout=3)
        client.ping()
        if mode == 'ai-worker':
            from app.ai_worker import AI_HEARTBEAT_KEY, heartbeat_fresh
            if not heartbeat_fresh(client.get(AI_HEARTBEAT_KEY)):
                raise SystemExit('AI worker heartbeat expired')
    else:
        raise SystemExit("Unknown probe")


if __name__ == "__main__":
    main()
