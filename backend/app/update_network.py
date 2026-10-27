"""Resolve the GitHub egress route just before network access, keeping secrets in memory."""
from dataclasses import replace
from types import SimpleNamespace

from .collectors.base import CollectorError
from .models import SystemNetworkSettings
from .network_settings import get_platform_proxy
from .update_common import UpdateError


def github_network_config(settings, db):
    if settings.proxy_route == 'direct':
        return replace(settings, proxy='')
    if settings.proxy_route == 'custom':
        return settings
    try:
        proxy = get_platform_proxy(db, SimpleNamespace(proxy_route='overseas'))
    except CollectorError:
        raise UpdateError('UPDATE_PROXY_UNAVAILABLE',
                          '系统更新需要境外代理，请在系统设置中配置并启用境外代理。', 409) from None
    except Exception:
        raise UpdateError('UPDATE_PROXY_UNAVAILABLE', '更新代理配置读取失败，请检查系统设置。', 503) from None
    return replace(settings, proxy=proxy)


def public_network_config(settings, db):
    result = settings.public_dict()
    if settings.proxy_route == 'overseas':
        row = db.get(SystemNetworkSettings, 1)
        result.update(proxy_configured=bool(row and row.overseas_encrypted_url),
                      proxy_enabled=bool(row and row.overseas_enabled),
                      proxy_source='系统设置中的境外代理')
        try:
            github_network_config(settings, db)
            result['proxy_ready'] = True
        except UpdateError:
            result['proxy_ready'] = False
    else:
        if settings.proxy_route == 'direct':
            result['proxy_configured'] = False
        result.update(proxy_enabled=bool(settings.proxy) and settings.proxy_route != 'direct',
                      proxy_ready=True,
                      proxy_source='独立更新代理（UPDATE_PROXY）' if result['proxy_route'] == 'custom' else '直连')
    return result
