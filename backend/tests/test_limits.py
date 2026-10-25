"""Fast-clock lease checks: simulate long upstream waits without real delays."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import limits
from app.limits import PlatformLease


class FastClock:
    def __init__(self):
        self.now = 1_800_000_000.0
        self.started = self.now
        self.sleeps = []

    def time(self):
        return self.now

    def monotonic(self):
        return self.now-self.started

    def advance(self, seconds):
        self.now += seconds

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


class ExpiringRedis:
    """Minimal Redis semantics including TTL expiry and the production Lua contracts."""
    def __init__(self, clock):
        self.clock = clock
        self.values = {}
        self.expires = {}
        self.renewed_at = []

    def get(self, key):
        if self.expires.get(key, float("inf")) <= self.clock.time():
            self.delete(key)
        return self.values.get(key)

    def set(self, key, value, nx=False, ex=None, px=None):
        if nx and self.get(key) is not None:
            return False
        self.values[key] = str(value)
        if ex is not None or px is not None:
            self.expires[key] = self.clock.time() + (float(ex) if ex is not None else float(px)/1000)
        return True

    def delete(self, key):
        found = key in self.values
        self.values.pop(key, None)
        self.expires.pop(key, None)
        return int(found)

    def eval(self, script, count, key, *arguments):
        assert count == 1
        if "redis.call('expire'" in script:
            if self.get(key) != arguments[0]:
                return 0
            self.expires[key] = self.clock.time()+180
            self.renewed_at.append(self.clock.time())
            return 1
        if "redis.call('del'" in script:
            return self.delete(key) if self.get(key) == arguments[0] else 0
        if "local old=tonumber" in script:
            if int(arguments[0]) > int(self.get(key) or 0):
                self.set(key, arguments[0], px=arguments[1])
            return 1
        if "local n=tonumber" in script:
            old = int(self.get(key) or 0)
            now, interval = map(int, arguments)
            if old > now:
                return old-now
            self.set(key, now+interval, px=interval)
            return 0
        raise AssertionError("Unexpected production Redis script")


@pytest.fixture
def fast_lease_environment(monkeypatch):
    clock = FastClock()
    client = ExpiringRedis(clock)
    monkeypatch.setattr(limits, "time", SimpleNamespace(time=clock.time, monotonic=clock.monotonic))
    monkeypatch.setattr(limits, "asyncio", SimpleNamespace(sleep=clock.sleep))
    return clock, client


@pytest.mark.asyncio
async def test_retry_after_600_seconds_keeps_lease_and_heartbeat_alive(fast_lease_environment):
    clock, client = fast_lease_environment
    heartbeats = []
    client.set("pm:lock:7", "worker-one", ex=180)
    lease = PlatformLease(client, 7, 1, owner="worker-one", heartbeat=lambda: heartbeats.append(clock.time()))
    lease.renew()
    await lease.sleep(600)
    assert clock.monotonic() == 600
    assert client.get("pm:lock:7") == "worker-one"
    assert len(heartbeats) >= 20
    assert max(clock.sleeps) <= 30
    assert all(second-first <= 30 for first, second in zip(client.renewed_at, client.renewed_at[1:]))
    assert client.expires["pm:lock:7"] > clock.time()


@pytest.mark.asyncio
async def test_persisted_retry_after_is_honored_after_crash_and_new_lease(fast_lease_environment):
    clock, client = fast_lease_environment
    initial = clock.time()
    client.set("pm:lock:8", "crashed-worker", ex=180)
    first = PlatformLease(client, 8, 1, owner="crashed-worker")

    class ProcessCrashed(Exception):
        pass

    async def crash_during_wait(until):
        clock.advance(30)
        raise ProcessCrashed

    first._wait = crash_during_wait
    with pytest.raises(ProcessCrashed):
        await first.sleep(600)
    assert int(client.get("pm:cooldown:8")) == int((initial+600)*1000)
    clock.advance(151)
    assert client.get("pm:lock:8") is None
    assert client.set("pm:lock:8", "replacement-worker", nx=True, ex=180)
    replacement = PlatformLease(client, 8, 1, owner="replacement-worker")
    await replacement.gate()
    assert clock.time() >= initial+600
    assert client.get("pm:lock:8") == "replacement-worker"
    assert max(clock.sleeps) <= 30
