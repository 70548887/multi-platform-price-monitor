"""Platform families and their independently validated collection versions."""
from copy import deepcopy

from fastapi import HTTPException

from .collectors import DEFAULT_RULE
from .collectors.fans8 import FANS8_DEFAULT_RULE, validate_fans8_rule
from .rule_config import validate_rule_config


PLATFORM_TYPES = [
    {'id': 'yile', 'name': '亿乐', 'adapters': [
        {'id': 'xiaohong66', 'name': '亿乐 · 当前站点版本', 'implemented': True,
         'default_base_url': 'https://www.xiaohong66.com', 'login_path': '/',
         'token_storage_key': 'token', 'note': ''},
    ]},
    {'id': 'yike', 'name': '易客', 'adapters': [
        {'id': 'fans8', 'name': '易客 · Fans8 PC版本', 'implemented': True,
         'default_base_url': 'https://fans8.top', 'login_path': '/indexPc.html#/login',
         'token_storage_key': 'ACCESS_TOKEN', 'note': '应援宝站点版本'},
        {'id': 'yike', 'name': '易客 · 标准版本（待验证）', 'implemented': False,
         'default_base_url': '', 'login_path': '/', 'token_storage_key': '',
         'note': '需要对应站点或接口资料验证'},
    ]},
]


def adapter_info(adapter):
    for family in PLATFORM_TYPES:
        for version in family['adapters']:
            if version['id'] == adapter:
                return family, version
    return None, None


def adapter_supported(platform):
    family, version = adapter_info(platform.adapter)
    return bool(version and version['implemented'] and
                family['id'] == platform.platform_type)


def require_supported(platform):
    if not adapter_supported(platform):
        raise HTTPException(422, '此平台版本尚待接口验证，请选择已支持的采集版本')


def adapter_default_rule(adapter):
    return deepcopy({'xiaohong66': DEFAULT_RULE, 'fans8': FANS8_DEFAULT_RULE}.get(adapter, {}))


def validate_adapter_rule(adapter, value):
    if adapter == 'xiaohong66':
        return validate_rule_config(value).model_dump()
    if adapter == 'fans8':
        return validate_fans8_rule(value)
    # Unverified versions may store connection details and an empty placeholder.
    if value:
        raise HTTPException(422, '待验证版本暂不支持配置采集规则')
    return {}
