"""Shared request limits for previews, account checks, and scheduled collectors."""
from contextlib import asynccontextmanager

from fastapi import HTTPException

from .tasks import redis_client
from .limits import LeaseBusy, PlatformLease


@asynccontextmanager
async def platform_operation(platform):
    rc = redis_client()
    try:
        async with PlatformLease(rc, platform.id, platform.request_interval) as lease:
            async def gate():
                await lease.gate()
            gate.sleep = lease.sleep
            yield gate
    except LeaseBusy:
        raise HTTPException(409, '该平台正在采集或测试，请稍后重试') from None
