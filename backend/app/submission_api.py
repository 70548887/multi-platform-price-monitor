"""Member platform suggestions are reviewed locally; submitted URLs are never fetched."""
from __future__ import annotations

import ipaddress
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .db import get_db
from .models import PlatformSubmission, User, utcnow
from .security import admin_user, audit, current_user, csrf_matches
from .services import iso

router = APIRouter(prefix='/api/platform-submissions')


def submission_user(request: Request, user=Depends(current_user)):
    supplied = request.headers.get('X-CSRF-Token', '')
    expected = request.session.get('csrf', '')
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and not csrf_matches(supplied, expected):
        raise HTTPException(403, '请求校验失败，请刷新后重试')
    return user


def public_site_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
        host = (parts.hostname or '').lower().rstrip('.').encode('idna').decode('ascii')
        if parts.scheme not in ('https', 'http') or not host or parts.username or parts.password:
            raise ValueError
        if parts.port not in (None, 443 if parts.scheme == 'https' else 80):
            raise ValueError
        if host == 'api.zxcbug.com' or host.endswith('.api.zxcbug.com'):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if ('.' not in host or host.endswith(('.localhost', '.local', '.internal', '.test', '.invalid', '.example'))
                    or host == 'localhost' or len(host) > 253
                    or any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-')
                           or any(not (char.isalnum() or char == '-') for char in label)
                           for label in host.split('.'))):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
            if address.version == 6:
                host = '[' + host + ']'
        # Keep only the public origin, never login tokens in paths/query/hash.
        return urlunsplit((parts.scheme, host, '', '', ''))
    except (ValueError, UnicodeError):
        raise ValueError('请填写公开平台的 HTTP/HTTPS 网址，不含账号密码或内网地址') from None


class SubmissionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=8, max_length=2000)
    notes: str = Field(default='', max_length=2000)

    @field_validator('name')
    @classmethod
    def name_not_blank(cls, value):
        if not value.strip():
            raise ValueError('请填写平台名称')
        return value.strip()

    _url = field_validator('url')(public_site_url)


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['accepted', 'rejected']
    admin_note: str = Field(default='', max_length=1000)


def submission_dict(row, *, username=None):
    value = {'id': row.id, 'name': row.name, 'url': row.url, 'notes': row.notes,
             'status': row.status, 'admin_note': row.admin_note,
             'created_at': iso(row.created_at), 'reviewed_at': iso(row.reviewed_at)}
    if username is not None:
        value['submitted_by'] = username
    return value


@router.get('')
def submissions(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                status: Literal['pending', 'accepted', 'rejected'] | None = None,
                db: Session = Depends(get_db), user=Depends(current_user)):
    query = select(PlatformSubmission)
    if user.role != 'admin':
        query = query.where(PlatformSubmission.user_id == user.id)
    if status is not None:
        query = query.where(PlatformSubmission.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(PlatformSubmission.id.desc()).offset((page-1)*page_size).limit(page_size)).all()
    names = {u.id: u.username for u in db.scalars(select(User).where(
        User.id.in_({r.user_id for r in rows})))} if user.role == 'admin' else {}
    return {'items': [submission_dict(row, username=names.get(row.user_id)) for row in rows],
            'total': total, 'page': page, 'page_size': page_size}


@router.post('')
def submit_platform(body: SubmissionInput, db: Session = Depends(get_db), user=Depends(submission_user)):
    db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': 19000000 + user.id})
    pending = list(db.scalars(select(PlatformSubmission).where(
        PlatformSubmission.user_id == user.id, PlatformSubmission.status == 'pending')))
    if any(row.url == body.url for row in pending):
        raise HTTPException(409, '你已提交此平台，请等待审核')
    if len(pending) >= 5:
        raise HTTPException(429, '最多保留 5 条待审核申请，请等待处理后再提交')
    row = PlatformSubmission(user_id=user.id, name=body.name, url=body.url, notes=body.notes)
    db.add(row)
    db.flush()
    audit(db, user, 'submit_platform', str(row.id))
    db.commit()
    return submission_dict(row)


@router.patch('/{submission_id}')
def review_platform(submission_id: int, body: ReviewInput, db: Session = Depends(get_db), user=Depends(admin_user)):
    row = db.scalar(select(PlatformSubmission).where(PlatformSubmission.id == submission_id).with_for_update())
    if row is None:
        raise HTTPException(404, '申请不存在')
    if row.status != 'pending':
        raise HTTPException(409, '此申请已审核，请刷新查看结果')
    row.status, row.admin_note = body.status, body.admin_note.strip()
    row.reviewed_at, row.reviewed_by = utcnow(), user.id
    audit(db, user, 'review_platform_submission', str(row.id))
    db.commit()
    return submission_dict(row)
