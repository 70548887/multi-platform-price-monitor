"""An AI worker heartbeat independent of Celery's solo task execution loop."""
from __future__ import annotations

import threading
import time

from celery import bootsteps
import redis


AI_HEARTBEAT_KEY = 'pm:ai:heartbeat'
AI_HEARTBEAT_INTERVAL = 30
AI_HEARTBEAT_TTL = 180


class AIWorkerHeartbeat(bootsteps.StartStopStep):
    # Starting after the pool avoids starting a thread before a prefork pool.
    requires = {'celery.worker.components:Pool'}

    def __init__(self, worker, **kwargs):
        self.stopped = threading.Event()
        self.thread = None
        self.client = None

    def include_if(self, worker):
        return worker.hostname.startswith('ai_worker@')

    def start(self, worker):
        self.stopped.clear()
        self.client = redis.Redis.from_url(worker.app.conf.broker_url, decode_responses=True,
                                           socket_connect_timeout=3, socket_timeout=3)
        self.thread = threading.Thread(target=self._run, name='ai-worker-heartbeat', daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                self.client.set(AI_HEARTBEAT_KEY, str(time.time()), ex=AI_HEARTBEAT_TTL)
            except Exception:
                # Never log broker URLs or their credentials. A missed pulse
                # expires naturally and is visible as a health failure.
                pass
            self.stopped.wait(AI_HEARTBEAT_INTERVAL)

    def stop(self, worker):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=4)
        if self.client:
            self.client.close()

    def terminate(self, worker):
        self.stop(worker)


def heartbeat_fresh(value, *, now=None):
    try:
        return value is not None and 0 <= (time.time() if now is None else now) - float(value) <= AI_HEARTBEAT_TTL
    except (ValueError, TypeError, OverflowError):
        return False
