from __future__ import annotations

import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .db import get_db
from .models import AuditLog, User


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600_000).hex()
    return f'pbkdf2_sha256$600000${salt}${digest}'


def password_matches(password: str, encoded: str) -> bool:
    try:
        _, iterations, salt, expected = encoded.split('$')
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def encrypt_session(token: str) -> str:
    return Fernet(config.ENCRYPTION_KEY.encode()).encrypt(token.encode()).decode()


def decrypt_session(token: str) -> str:
    return Fernet(config.ENCRYPTION_KEY.encode()).decrypt(token.encode()).decode()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get('user_id')
    user = db.get(User, uid) if isinstance(uid, int) else None
    if not user or not user.enabled:
        raise HTTPException(401, '请登录')
    return user


def csrf_matches(supplied, expected) -> bool:
    # compare_digest(str, str) rejects non-ASCII input with TypeError. Invalid
    # untrusted header bytes must produce a 403, never an application crash.
    return (isinstance(supplied, str) and isinstance(expected, str) and bool(expected)
            and hmac.compare_digest(supplied.encode('utf-8'), expected.encode('utf-8')))


def admin_user(request: Request, user: User = Depends(current_user)) -> User:
    if user.role != 'admin':
        raise HTTPException(403, '需要管理员权限')
    supplied = request.headers.get('X-CSRF-Token', '')
    expected = request.session.get('csrf', '')
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and not csrf_matches(supplied, expected):
        raise HTTPException(403, '请求校验失败，请刷新后重试')
    return user


def validate_base_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('平台地址必须是无账号、查询参数或片段的 HTTPS 网址')
    if parts.path not in ('', '/') or parts.port not in (None, 443):
        raise ValueError('平台地址仅填写 HTTPS 根域名')
    if parts.hostname == 'api.zxcbug.com' or parts.hostname.endswith('.api.zxcbug.com'):
        raise ValueError('此域名不能配置为采集目标')
    return value.rstrip('/')


def audit(db: Session, user: User | None, action: str, target: str):
    db.add(AuditLog(user_id=user.id if user else None, action=action, target=target))
