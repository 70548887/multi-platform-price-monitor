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
    elif mode == "beat":
        from redis import Redis

        pid = int(Path("/tmp/celerybeat.pid").read_text().strip())
        os.kill(pid, 0)
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
        if b"celery" not in command or b"beat" not in command:
            raise SystemExit("Unexpected beat process")
        Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=3, socket_timeout=3).ping()
    else:
        raise SystemExit("Unknown probe")


if __name__ == "__main__":
    main()
