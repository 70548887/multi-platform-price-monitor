from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(os.getenv('APP_ROOT', str(Path(__file__).resolve().parents[2]))).resolve()
load_dotenv(ROOT / '.env')

DATABASE_URL = os.environ['DATABASE_URL']
REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:56379/0')
SESSION_SECRET = os.getenv('SESSION_SECRET', '')
ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY', '')
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin888')
DEFAULT_USER_USERNAME = os.getenv('DEFAULT_USER_USERNAME', 'user')
DEFAULT_USER_PASSWORD = os.getenv('DEFAULT_USER_PASSWORD', 'user123')
COOKIE_SECURE = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
DATA_DIR = Path(os.getenv('DATA_DIR', str(ROOT / '.runtime')))
DETAIL_RETENTION_DAYS = int(os.getenv('DETAIL_RETENTION_DAYS', '180'))
AGGREGATE_RETENTION_DAYS = int(os.getenv('AGGREGATE_RETENTION_DAYS', '730'))
ERROR_RETENTION_DAYS = int(os.getenv('ERROR_RETENTION_DAYS', '7'))


def validate_secrets() -> None:
    if len(SESSION_SECRET) < 32:
        raise RuntimeError('SESSION_SECRET must contain at least 32 characters')
    if not ENCRYPTION_KEY:
        raise RuntimeError('ENCRYPTION_KEY is required')
    from cryptography.fernet import Fernet
    Fernet(ENCRYPTION_KEY.encode())
