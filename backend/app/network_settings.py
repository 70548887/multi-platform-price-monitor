"""Encrypted, administrator-managed collection proxy routes.

Credentials are returned only to collectors in memory, never public serializers.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import text

from .collectors.base import CollectorError
from .models import SystemNetworkSettings, utcnow
from .security import decrypt_session, encrypt_session

ROUTES = ('direct', 'tunnel', 'overseas')
PROXY_GROUPS = ('tunnel', 'overseas')


def validate_proxy_url(value: str) -> str:
    """HTTP(S) proxy endpoint only; error text must never contain the input."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096
            or value != value.strip() or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        raise HTTPException(422, '代理地址格式错误，请使用完整的 HTTP 或 HTTPS 代理地址')
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
        if (parts.scheme not in ('http', 'https') or not host or port is None
                or not 1 <= port <= 65535 or parts.path not in ('', '/')
                or parts.query or parts.fragment or '\\' in value):
            raise ValueError
        host = host.lower().rstrip('.')
        if host == 'api.zxcbug.com' or host.endswith('.api.zxcbug.com'):
            raise ValueError
        # DNS names and public numeric addresses are allowed. Internal/loopback
        # proxies are useful for an operator-managed tunnel agent on the host.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or any(not label or len(label) > 63 for label in host.rstrip('.').split('.')):
                raise ValueError
        if parts.username is not None and not parts.username:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(422, '代理地址格式错误，请使用完整的 HTTP 或 HTTPS 代理地址') from None
    return value


def network_dict(settings: SystemNetworkSettings | None) -> dict:
    return {**{group: {'enabled': bool(getattr(settings, f'{group}_enabled', False)),
                       'configured': bool(getattr(settings, f'{group}_encrypted_url', None))}
               for group in PROXY_GROUPS},
            'updated_at': settings.updated_at.isoformat() if settings else None}


def update_network_settings(db, payload) -> SystemNetworkSettings:
    if not isinstance(payload, dict) or set(payload) - set(PROXY_GROUPS):
        raise HTTPException(422, '网络设置字段无效')
    db.execute(text('SELECT pg_advisory_xact_lock(71960203)'))
    settings = db.get(SystemNetworkSettings, 1)
    if settings is None:
        settings = SystemNetworkSettings(id=1, tunnel_enabled=False, overseas_enabled=False)
        db.add(settings)
    for group, update in payload.items():
        if not isinstance(update, dict) or set(update) - {'enabled', 'url', 'clear'}:
            raise HTTPException(422, '代理设置字段无效')
        for field in ('enabled', 'clear'):
            if field in update and type(update[field]) is not bool:
                raise HTTPException(422, '代理启用和清除字段必须为布尔值')
        value = update.get('url')
        if value is not None and not isinstance(value, str):
            raise HTTPException(422, '代理地址必须为字符串')
        if update.get('clear') and value:
            raise HTTPException(422, '清除代理与设置新地址不能同时执行')
        encrypted_field = f'{group}_encrypted_url'
        enabled_field = f'{group}_enabled'
        if update.get('clear'):
            setattr(settings, encrypted_field, None)
            setattr(settings, enabled_field, False)
        elif value:
            setattr(settings, encrypted_field, encrypt_session(validate_proxy_url(value)))
        if 'enabled' in update:
            if update['enabled'] and not getattr(settings, encrypted_field):
                raise HTTPException(422, '请先配置代理地址再启用此代理')
            setattr(settings, enabled_field, update['enabled'])
    settings.updated_at = utcnow()
    db.flush()
    return settings


def get_platform_proxy(db, platform) -> str | None:
    route = getattr(platform, 'proxy_route', 'direct') or 'direct'
    if route == 'direct':
        return None
    if route not in PROXY_GROUPS:
        raise CollectorError('采集代理路由无效，请检查平台代理设置')
    settings = db.get(SystemNetworkSettings, 1)
    if (settings is None or not getattr(settings, f'{route}_enabled')
            or not getattr(settings, f'{route}_encrypted_url')):
        raise CollectorError('平台指定的采集代理尚未配置或未启用')
    try:
        return validate_proxy_url(decrypt_session(getattr(settings, f'{route}_encrypted_url')))
    except Exception:
        raise CollectorError('采集代理配置无法读取，请在系统设置中重新保存') from None
