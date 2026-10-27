"""Seed requested installation accounts once; upgrades never reset existing users."""
import re

from sqlalchemy import select, text

from .models import User
from .security import password_hash


def seed_initial_accounts(db, settings):
    db.execute(text('SELECT pg_advisory_xact_lock(71960222)'))
    if db.scalar(select(User.id).limit(1)) is not None:
        return False
    definitions = (
        (settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD, 'admin'),
        (settings.DEFAULT_USER_USERNAME, settings.DEFAULT_USER_PASSWORD, 'viewer'),
    )
    if definitions[0][0] == definitions[1][0]:
        raise RuntimeError('Initial admin and viewer usernames must be different')
    for username, password, _ in definitions:
        if not isinstance(username, str) or not re.fullmatch(r'[a-zA-Z0-9_.@-]{2,80}', username):
            raise RuntimeError('Initial account username is invalid')
        if not isinstance(password, str) or not 7 <= len(password) <= 256:
            raise RuntimeError('Initial account password must contain 7 to 256 characters')
    for username, password, role in definitions:
        db.add(User(username=username, password_hash=password_hash(password), role=role, enabled=True))
    db.flush()
    return True
