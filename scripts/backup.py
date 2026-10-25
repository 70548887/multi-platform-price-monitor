"""Create and validate an atomic PostgreSQL custom-format backup."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import ipaddress
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit

from acceptance_watch import read_env


ROOT = Path(__file__).resolve().parents[1]


class BackupError(Exception):
    """A failure safe to display without database connection credentials."""


@dataclass(frozen=True)
class NativeDatabase:
    host: str
    port: int
    database: str
    username: str
    password: str = field(repr=False)


def parse_database_url(value: str, fallback_password: str = "") -> NativeDatabase:
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"postgres", "postgresql", "postgresql+psycopg", "postgresql+psycopg2"}:
            raise ValueError
        host = parts.hostname or ""
        local = host.lower() == "localhost"
        if not local:
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = False
        if not local or parts.query or parts.fragment:
            raise ValueError
        port = parts.port or 5432
        database, username = unquote(parts.path[1:]), unquote(parts.username or "")
        password = unquote(parts.password) if parts.password is not None else fallback_password
        if not database or not username or not 1 <= port <= 65535:
            raise ValueError
        if any("\x00" in item or "\n" in item or "\r" in item for item in (database, username, password)):
            raise ValueError
        return NativeDatabase(host, port, database, username, password)
    except (ValueError, TypeError):
        raise BackupError("Native backup requires a valid local PostgreSQL DATABASE_URL in .env.") from None


def postgres_tools(directory: Path | None) -> tuple[str, str]:
    suffix = ".exe" if os.name == "nt" else ""
    if directory is not None:
        dump, restore = directory / ("pg_dump" + suffix), directory / ("pg_restore" + suffix)
        if dump.is_file() and restore.is_file():
            return str(dump), str(restore)
        raise BackupError("The selected --pg-bin directory does not contain pg_dump and pg_restore.")
    installed = Path("C:/Program Files/PostgreSQL/16/bin")
    if os.name == "nt" and (installed / "pg_dump.exe").is_file() and (installed / "pg_restore.exe").is_file():
        return str(installed / "pg_dump.exe"), str(installed / "pg_restore.exe")
    dump, restore = shutil.which("pg_dump"), shutil.which("pg_restore")
    if dump and restore:
        return dump, restore
    raise BackupError("PostgreSQL client tools were not found; set --pg-bin to the installed bin directory.")


def create_backup(*, mode: str, directory: Path, pg_bin: Path | None = None) -> Path:
    settings = read_env(ROOT / ".env")
    child_env = os.environ.copy()
    if mode == "native":
        database = parse_database_url(os.getenv("DATABASE_URL", settings.get("DATABASE_URL", "")),
                                      os.getenv("POSTGRES_PASSWORD", settings.get("POSTGRES_PASSWORD", "")))
        dump, restore = postgres_tools(pg_bin)
        dump_command = [dump, "--format=custom", "--no-owner", "--no-password",
                        f"--host={database.host}", f"--port={database.port}",
                        f"--username={database.username}", f"--dbname={database.database}"]
        restore_command = [restore, "--list"]
        child_env["PGPASSWORD"] = database.password
        child_env["PGCONNECT_TIMEOUT"] = "15"
    else:
        if not shutil.which("docker"):
            raise BackupError("Docker CLI is unavailable; backup was not run.")
        username = os.getenv("POSTGRES_USER", settings.get("POSTGRES_USER", "price_monitor"))
        name = os.getenv("POSTGRES_DB", settings.get("POSTGRES_DB", "price_monitor"))
        compose = ["docker", "compose", "--project-directory", str(ROOT), "exec", "-T", "db"]
        dump_command = [*compose, "pg_dump", "--format=custom", "--no-owner", "--no-password",
                        f"--username={username}", f"--dbname={name}"]
        restore_command = [*compose, "pg_restore", "--list"]
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = directory / f"price-monitor-{timestamp}.dump"
    descriptor, filename = tempfile.mkstemp(prefix=".backup-", dir=directory)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            result = subprocess.run(dump_command, cwd=ROOT, env=child_env, stdout=stream,
                                    stderr=subprocess.PIPE, check=False, timeout=600)
            stream.flush()
            os.fsync(stream.fileno())
        if result.returncode:
            raise BackupError("pg_dump failed; no completed backup was published.")
        with temporary.open("rb") as probe:
            if probe.read(5) != b"PGDMP":
                raise BackupError("Backup header validation failed; no completed backup was published.")
        # --list reads archive metadata without connecting to or restoring a DB.
        with temporary.open("rb") as archive:
            result = subprocess.run(restore_command, cwd=ROOT, env=child_env, stdin=archive,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False, timeout=120)
        if result.returncode:
            raise BackupError("pg_restore --list validation failed; no completed backup was published.")
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
        return output.resolve()
    except subprocess.TimeoutExpired:
        raise BackupError("PostgreSQL backup or archive validation timed out; no completed backup was published.") from None
    except OSError:
        raise BackupError("Backup file or PostgreSQL client access failed; no completed backup was published.") from None
    finally:
        temporary.unlink(missing_ok=True)
        child_env.pop("PGPASSWORD", None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("native", "docker"), default="docker")
    parser.add_argument("--directory", type=Path, default=ROOT / "backups")
    parser.add_argument("--pg-bin", type=Path, help="Native PostgreSQL client bin directory; defaults to installed PostgreSQL 16 or PATH")
    args = parser.parse_args(argv)
    try:
        output = create_backup(mode=args.mode, directory=args.directory, pg_bin=args.pg_bin)
    except BackupError as exc:
        parser.exit(1, str(exc) + "\n")
    print(f"Backup: {output}")
    print("Validation: pg_restore --list passed; no database restore was performed.")
    print("Back up .env separately and securely; its ENCRYPTION_KEY decrypts stored platform sessions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
