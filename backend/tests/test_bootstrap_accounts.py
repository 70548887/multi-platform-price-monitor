"""Installation account defaults are tested only against price_monitor_test."""
from types import SimpleNamespace

from sqlalchemy import func, select

from test_pipeline import isolated_database, clean_test_resources, db, admin_client
from app.bootstrap_accounts import seed_initial_accounts
from app.models import User, UserPlatformAccess
from app.security import password_hash, password_matches


def settings(**changes):
    return SimpleNamespace(**{
        'ADMIN_USERNAME': 'admin', 'ADMIN_PASSWORD': 'admin888',
        'DEFAULT_USER_USERNAME': 'user', 'DEFAULT_USER_PASSWORD': 'user123', **changes,
    })


def test_initial_install_creates_requested_accounts_with_no_viewer_platform_grants(db):
    assert seed_initial_accounts(db, settings()) is True
    db.commit()
    users = {row.username: row for row in db.scalars(select(User))}
    assert set(users) == {'admin', 'user'}
    assert users['admin'].role == 'admin' and password_matches('admin888', users['admin'].password_hash)
    assert users['user'].role == 'viewer' and password_matches('user123', users['user'].password_hash)
    assert db.scalar(select(func.count()).select_from(UserPlatformAccess)) == 0


def test_explicit_installation_values_override_default_accounts(db):
    values = settings(ADMIN_USERNAME='owner', ADMIN_PASSWORD='owner-password',
                      DEFAULT_USER_USERNAME='reader', DEFAULT_USER_PASSWORD='reader-password')
    seed_initial_accounts(db, values)
    db.commit()
    users = {row.username: row for row in db.scalars(select(User))}
    assert set(users) == {'owner', 'reader'}
    assert password_matches(values.ADMIN_PASSWORD, users['owner'].password_hash)
    assert password_matches(values.DEFAULT_USER_PASSWORD, users['reader'].password_hash)


def test_restart_or_upgrade_never_resets_or_adds_accounts_to_existing_database(db):
    existing = User(username='existing-owner', password_hash=password_hash('original-password'), role='admin')
    db.add(existing)
    db.commit()
    original_hash = existing.password_hash
    assert seed_initial_accounts(db, settings()) is False
    db.commit()
    assert db.scalar(select(func.count()).select_from(User)) == 1
    assert existing.password_hash == original_hash


def test_user_api_accepts_requested_seven_character_passwords(admin_client):
    created = admin_client.post('/api/users', json={'username': 'user', 'password': 'user123', 'role': 'viewer'})
    assert created.status_code == 200
    assert created.json()['platform_ids'] == []
    changed = admin_client.patch('/api/users/' + str(created.json()['id']), json={'password': 'new1234'})
    assert changed.status_code == 200
    rejected = admin_client.patch('/api/users/' + str(created.json()['id']), json={'password': 'short'})
    assert rejected.status_code == 422
