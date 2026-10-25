"""Generate local deployment secrets once; never overwrite an existing .env."""
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import secrets
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    parser.add_argument("--admin-username", default="admin")
    parser.add_argument("--quiet-secret", action="store_true", help="Do not print the generated admin password")
    parser.add_argument("--native", action="store_true", help="Use project-local PostgreSQL/Redis ports")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.exit(1, f"Refusing to overwrite existing secrets: {output}\n")
    if not args.admin_username or any(c in args.admin_username for c in "\r\n=# "):
        parser.error("Admin username must not contain whitespace, #, or =")
    password = secrets.token_urlsafe(24)
    values = {
        "POSTGRES_USER": "price_monitor",
        "POSTGRES_DB": "price_monitor",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "ADMIN_USERNAME": args.admin_username,
        "ADMIN_PASSWORD": password,
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
        "COOKIE_SECURE": "false",
        "WEB_BIND": "127.0.0.1",
        "WEB_PORT": "28671",
    }
    if args.native:
        values["DATABASE_URL"] = f"postgresql+psycopg://price_monitor:{values['POSTGRES_PASSWORD']}@127.0.0.1:55432/price_monitor"
        values["REDIS_URL"] = "redis://127.0.0.1:56379/0"
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".env-", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        if output.exists():
            raise FileExistsError(f"Secrets already exist: {output}")
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"Created {output}")
    print(f"Admin username: {args.admin_username}")
    if not args.quiet_secret:
        print(f"Admin password: {password}")
    else:
        print("Admin password was saved to .env and was not printed.")
    print("Keep .env securely; ENCRYPTION_KEY is required to recover platform sessions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
