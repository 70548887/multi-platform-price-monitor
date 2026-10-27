"""Generate local deployment secrets once; never overwrite an existing .env."""
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import secrets
import tempfile


def prepare_update_config(root: Path) -> bool:
    """Install the public template once, including in extracted Release installs."""
    directory = root / "config"
    source, target = directory / "update.example.yaml", directory / "update.yaml"
    if directory.is_symlink() or source.is_symlink() or target.is_symlink():
        raise ValueError("Update configuration paths cannot be symbolic links")
    if target.exists():
        if not target.is_file():
            raise ValueError("config/update.yaml must be a regular file")
        return False
    if not source.is_file():
        raise ValueError("config/update.example.yaml is missing from this installation")
    descriptor, temporary = tempfile.mkstemp(prefix=".update-config-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(source.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        # This file contains public options and environment-variable references;
        # containers must be able to read the mounted file as their own UID.
        os.chmod(temporary, 0o644)
        try:
            # Link publishes complete bytes without overwriting a concurrent
            # administrator's configuration; unlike replace it is exclusive.
            os.link(temporary, target)
        except FileExistsError:
            if target.is_symlink() or not target.is_file():
                raise ValueError("config/update.yaml must be a regular file") from None
            return False
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    parser.add_argument("--admin-username", default=os.getenv("ADMIN_USERNAME", "admin"))
    parser.add_argument("--quiet-secret", action="store_true", help="Do not print installation account passwords")
    parser.add_argument("--native", action="store_true", help="Use project-local PostgreSQL/Redis ports")
    parser.add_argument("--prepare-config-only", action="store_true", help="Create missing public update config without changing existing secrets")
    args = parser.parse_args()
    output = args.output.resolve()
    if args.prepare_config_only:
        try:
            created = prepare_update_config(output.parent)
        except (OSError, ValueError) as error:
            parser.exit(1, f"Update configuration initialization failed: {error}\n")
        print("Created config/update.yaml" if created else "Kept existing config/update.yaml")
        return 0
    if output.exists():
        parser.exit(1, f"Refusing to overwrite existing secrets: {output}\n")
    if not args.admin_username or any(c in args.admin_username for c in "\r\n=# "):
        parser.error("Admin username must not contain whitespace, #, or =")
    password = os.getenv("ADMIN_PASSWORD", "admin888")
    viewer_username = os.getenv("DEFAULT_USER_USERNAME", "user")
    viewer_password = os.getenv("DEFAULT_USER_PASSWORD", "user123")
    if (not viewer_username or any(c in viewer_username for c in "\r\n=# ")
            or viewer_username == args.admin_username):
        parser.error("Default user username must be valid and different from admin")
    if any(not 7 <= len(value) <= 256 or any(c in value for c in "\r\n")
           for value in (password, viewer_password)):
        parser.error("Installation account passwords must contain 7 to 256 characters and no newlines")
    values = {
        "POSTGRES_USER": "price_monitor",
        "POSTGRES_DB": "price_monitor",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "ADMIN_USERNAME": args.admin_username,
        "ADMIN_PASSWORD": password,
        "DEFAULT_USER_USERNAME": viewer_username,
        "DEFAULT_USER_PASSWORD": viewer_password,
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
        print(f"Default user username: {viewer_username}")
        print(f"Default user password: {viewer_password}")
    else:
        print("Admin password was saved to .env and was not printed.")
    print("Keep .env securely; ENCRYPTION_KEY is required to recover platform sessions.")
    if (output.parent / "config/update.example.yaml").is_file():
        prepare_update_config(output.parent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
