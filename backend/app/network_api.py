"""Administrator-only collection proxy configuration."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .db import get_db
from .models import SystemNetworkSettings
from .network_settings import network_dict, update_network_settings
from .security import admin_user, audit

router = APIRouter()


@router.get('/api/settings/network')
def get_settings(db: Session = Depends(get_db), user=Depends(admin_user)):
    return network_dict(db.get(SystemNetworkSettings, 1))


@router.put('/api/settings/network')
async def save_settings(request: Request, db: Session = Depends(get_db), user=Depends(admin_user)):
    # Parse explicitly to avoid framework validation responses echoing proxy
    # usernames, passwords or complete URLs in a malformed secret field.
    content = await request.body()
    if len(content) > 20000:
        raise HTTPException(422, '网络设置内容超过大小限制')
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeError):
        raise HTTPException(422, '网络设置必须为有效 JSON 对象') from None
    result = update_network_settings(db, payload)
    audit(db, user, 'update_network_settings', 'network')
    db.commit()
    return network_dict(result)
