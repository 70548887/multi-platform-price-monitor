"""Query-level platform authorization, evaluated afresh for every request."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select, true

from .models import UserPlatformAccess


def platform_scope(user, platform_column):
    if user.role == 'admin':
        return true()
    return platform_column.in_(select(UserPlatformAccess.platform_id).where(UserPlatformAccess.user_id == user.id))


def assigned_platform_ids(db, user):
    if user.role == 'admin':
        return []
    return list(db.scalars(select(UserPlatformAccess.platform_id).where(
        UserPlatformAccess.user_id == user.id).order_by(UserPlatformAccess.platform_id)))


def access_dict(db, user):
    return {'all_platforms': user.role == 'admin', 'platform_ids': assigned_platform_ids(db, user)}


def require_platform_record(db, user, model, identifier):
    record = db.scalar(select(model).where(model.id == identifier, platform_scope(user, model.platform_id)))
    if record is None:
        raise HTTPException(404, '记录不存在')
    return record
