"""Renewable platform lease with a persisted upstream retry deadline."""
import asyncio
import contextlib
import secrets
import time

from .collectors import CollectorError


class LeaseBusy(Exception):
    pass


class PlatformLease:
    def __init__(self, client, platform_id, interval, *, owner=None, heartbeat=None):
        self.client, self.platform_id, self.interval = client, platform_id, float(interval)
        self.owner = owner or secrets.token_hex(24)
        self.preacquired = owner is not None
        self.key = f'pm:lock:{platform_id}'
        self.cooldown_key = f'pm:cooldown:{platform_id}'
        self.heartbeat = heartbeat
        self._last_heartbeat = 0
        self._task = None
        self._lost = False

    def renew(self):
        if self._lost:
            raise CollectorError('采集锁已失效，停止本批次')
        renewed = self.client.eval("if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('expire',KEYS[1],180) end return 0", 1, self.key, self.owner)
        if not renewed:
            self._lost = True
            raise CollectorError('采集锁已失效，停止本批次')
        if self.heartbeat and time.monotonic()-self._last_heartbeat >= 15:
            self.heartbeat()
            self._last_heartbeat = time.monotonic()

    async def _maintain(self):
        try:
            while True:
                await asyncio.sleep(30)
                self.renew()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lost = True

    async def start(self):
        if not self.preacquired and not self.client.set(self.key, self.owner, nx=True, ex=180):
            raise LeaseBusy()
        self.renew()
        self._task = asyncio.create_task(self._maintain())
        return self

    async def close(self):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self.client.eval("if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) end return 0", 1, self.key, self.owner)

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *_):
        await self.close()

    async def _wait(self, until_ms):
        while True:
            self.renew()
            remaining = (until_ms-int(time.time()*1000))/1000
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 30))

    async def sleep(self, seconds):
        """Retry-After survives a process crash and is shared with previews."""
        seconds = max(0, float(seconds))
        until = int(time.time()*1000+seconds*1000)
        self.client.eval("local old=tonumber(redis.call('get',KEYS[1]) or '0'); local n=tonumber(ARGV[1]); if n>old then redis.call('set',KEYS[1],n,'PX',ARGV[2]) end return 1", 1,
                         self.cooldown_key, until, max(1000, int(seconds*1000)+1000))
        await self._wait(until)

    async def gate(self):
        while True:
            self.renew()
            cooldown = int(self.client.get(self.cooldown_key) or 0)
            if cooldown > int(time.time()*1000):
                await self._wait(cooldown)
                continue
            wait = self.client.eval("local n=tonumber(redis.call('get',KEYS[1]) or '0'); local t=tonumber(ARGV[1]); if n>t then return n-t end; redis.call('set',KEYS[1],t+tonumber(ARGV[2]),'PX',ARGV[2]); return 0", 1,
                f'pm:gate:{self.platform_id}', int(time.time()*1000), max(1, int(self.interval*1000)))
            if not wait:
                return
            await self._wait(int(time.time()*1000)+wait)
