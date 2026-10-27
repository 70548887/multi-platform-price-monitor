"""Administrator-owned AI endpoint and encrypted key configuration."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException
from sqlalchemy import text

from .models import AISettings, utcnow
from .security import decrypt_session, encrypt_session


def endpoint_url(value):
    try:
        if not isinstance(value, str) or len(value) > 500 or any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value:
            raise ValueError
        parts = urlsplit(value)
        host = (parts.hostname or '').lower().rstrip('.').encode('idna').decode('ascii')
        if (parts.scheme != 'https' or not host or parts.username or parts.password or parts.query or parts.fragment
                or parts.port not in (None, 443) or host == 'api.zxcbug.com' or host.endswith('.api.zxcbug.com')):
            raise ValueError
        if not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)*/*', parts.path):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if ('.' not in host or host.endswith(('.local', '.localhost', '.internal', '.test', '.invalid', '.example'))
                    or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.'))):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
            if address.version == 6:
                host = '[' + host + ']'
        path = parts.path.rstrip('/')
        if path.endswith('/chat/completions'):
            path = path[:-len('/chat/completions')]
        return urlunsplit(('https', host, path, '', ''))
    except (ValueError, UnicodeError):
        raise HTTPException(422, 'AI 接口须为公开 HTTPS Base URL，不含认证信息、查询参数或内网地址') from None


def public_ai_settings(row):
    return {'enabled': bool(row.enabled) if row else False, 'base_url': row.base_url if row else '',
            'key_configured': bool(row and row.encrypted_key), 'model': row.model if row else '',
            'daily_time': row.daily_time if row else '09:00', 'timezone': 'Asia/Shanghai',
            'proxy_route': row.proxy_route if row else 'direct', 'sample_limit': row.sample_limit if row else 100,
            'daily_product_limit': row.daily_product_limit if row else 1000,
            'daily_request_limit': row.daily_request_limit if row else 20,
            'auto_apply': bool(row.auto_apply) if row else False, 'batch_size': 50,
            'revision': row.revision if row else 0,
            'json_mode': bool(row.json_mode) if row else True,
            'configured': bool(row and row.base_url and row.encrypted_key and row.model),
            'updated_at': row.updated_at.isoformat() if row else None}


def save_ai_settings(db, payload):
    if not isinstance(payload, dict) or set(payload) - {'enabled','base_url','key','clear_key','model','daily_time','proxy_route','sample_limit','json_mode','daily_product_limit','daily_request_limit','auto_apply'}:
        raise HTTPException(422, 'AI 配置字段无效')
    db.execute(text('SELECT pg_advisory_xact_lock(71960220)'))
    row = db.get(AISettings, 1)
    if row is None:
        row = AISettings(id=1, enabled=False, base_url='', encrypted_key=None, model='', daily_time='09:00',
                         proxy_route='direct', sample_limit=100, json_mode=True, revision=0,
                         daily_product_limit=1000,daily_request_limit=20,auto_apply=False)
        db.add(row)
    for name in ('enabled','clear_key','json_mode','auto_apply'):
        if name in payload and type(payload[name]) is not bool:
            raise HTTPException(422, 'AI 开关字段须为布尔值')
    if 'base_url' in payload:
        value = payload['base_url']
        row.base_url = endpoint_url(value) if value else ''
    if 'model' in payload:
        if not isinstance(payload['model'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}', payload['model']):
            raise HTTPException(422, '请填写有效的模型名称')
        row.model = payload['model']
    if 'daily_time' in payload:
        if not isinstance(payload['daily_time'], str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', payload['daily_time']):
            raise HTTPException(422, '每日分析时间须为 HH:MM（北京时间）')
        row.daily_time = payload['daily_time']
    if 'proxy_route' in payload:
        if payload['proxy_route'] not in ('direct','overseas'):
            raise HTTPException(422, 'AI 请求线路仅支持直连或境外代理')
        row.proxy_route = payload['proxy_route']
    if 'sample_limit' in payload:
        if type(payload['sample_limit']) is not int or not 10 <= payload['sample_limit'] <= 200:
            raise HTTPException(422, '每次 AI 商品样本数须为 10 到 200')
        row.sample_limit = payload['sample_limit']
    for name, maximum in (('daily_product_limit',1000),('daily_request_limit',20)):
        if name in payload:
            if type(payload[name]) is not int or not 1 <= payload[name] <= maximum:
                raise HTTPException(422, f'{name} 须为 1 到 {maximum} 的整数')
            setattr(row,name,payload[name])
    key = payload.get('key')
    if key is not None and (not isinstance(key, str) or len(key) > 4096 or any(ord(c) <= 32 or ord(c) >= 127 for c in key)):
        raise HTTPException(422, 'API Key 格式无效')
    if payload.get('clear_key') and key:
        raise HTTPException(422, '清除 Key 与设置新 Key 不能同时执行')
    if payload.get('clear_key'):
        row.encrypted_key = None
        row.enabled = False
    elif key:
        # Reject keys pasted into other public fields so the write-only
        # credential cannot accidentally become part of the visible config.
        if key in row.base_url or key in row.model:
            raise HTTPException(422, 'Key 只能填写在密钥字段')
        row.encrypted_key = encrypt_session(key)
    for name in ('enabled','json_mode','auto_apply'):
        if name in payload:
            setattr(row, name, payload[name])
    if row.encrypted_key:
        try:
            secret = decrypt_session(row.encrypted_key)
        except Exception:
            raise HTTPException(422, '已有 AI Key 无法解密，请重新填写') from None
        if secret and (secret in row.base_url or secret in row.model):
            raise HTTPException(422, '公开配置字段不能包含 API Key')
    if row.enabled and not (row.base_url and row.model and row.encrypted_key):
        raise HTTPException(422, '启用每日分析前，请填写接口网址、模型和 Key')
    row.revision += 1
    row.updated_at = utcnow()
    db.flush()
    return row
