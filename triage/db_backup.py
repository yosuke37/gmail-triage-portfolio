"""Credential-safe PostgreSQL custom-format backups."""
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess

from sqlalchemy.engine import make_url


def backup_postgres(database_url, destination_dir):
    from triage import settings

    executable = shutil.which("pg_dump")
    if not executable:
        local = settings.ROOT / ".tools" / "postgresql-18.6-5" / "pgsql" / "bin" / "pg_dump.exe"
        executable = str(local) if local.exists() else None
    if not executable:
        raise ValueError("pg_dump is not installed. Use a PostgreSQL client version compatible with the server.")
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    output = destination_dir / f"postgres-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.dump"
    url = make_url(database_url)
    safe_dsn = url.set(drivername="postgresql")._replace(password=None).render_as_string(hide_password=False)
    env = {**os.environ, "PGPASSWORD": url.password or ""}
    result = subprocess.run([executable, "--dbname", safe_dsn, "--format=custom", "--file", str(output)],
                            env=env, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise ValueError("PostgreSQL backup failed. Check pg_dump/server versions and connectivity; credentials are omitted.")
    return output
