"""Scheduled validated dumps; database credentials never appear in argv/logs."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event
import time
from urllib.parse import parse_qs, unquote, urlsplit


def connection_env():
    value = Path(os.environ["TRIAGE_DATABASE_URL_FILE"]).read_text().strip()
    url = urlsplit(value.replace("postgresql+psycopg://", "postgresql://", 1))
    if url.scheme not in ("postgres", "postgresql") or not url.hostname or not url.path.strip("/"):
        raise ValueError("Invalid backup database secret")
    env = {**os.environ, "PGHOST": url.hostname, "PGPORT": str(url.port or 5432),
        "PGDATABASE": unquote(url.path[1:]), "PGUSER": unquote(url.username or ""), "PGPASSWORD": unquote(url.password or "")}
    supported = {"sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT", "sslkey": "PGSSLKEY", "channel_binding": "PGCHANNELBINDING"}
    for key, values in parse_qs(url.query).items():
        if key not in supported:
            raise ValueError("Unsupported backup connection option")
        env[supported[key]] = values[-1]
    env["PGCONNECT_TIMEOUT"] = "15"
    return env


def backup_once(folder=None):
    folder = Path(folder or os.getenv("BACKUP_DIR", "/backups"))
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = folder / ("postgres-" + stamp + ".dump")
    partial = output.with_suffix(".partial")
    try:
        result = subprocess.run(["pg_dump", "--format=custom", "--file", str(partial)], env=connection_env(), capture_output=True, timeout=3600)
        if result.returncode:
            raise ValueError("Backup failed")
        result = subprocess.run(["pg_restore", "--list", str(partial)], capture_output=True, timeout=60)
        if result.returncode:
            raise ValueError("Backup archive validation failed")
        partial.replace(output)
        marker = folder / ".last-success.json"
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(json.dumps({"time": time.time(), "archive": output.name}), encoding="utf-8")
        temporary.replace(marker)
        retain = max(1, int(os.getenv("BACKUP_RETAIN_COUNT", "7")))
        for old in sorted(folder.glob("postgres-*.dump"), reverse=True)[retain:]:
            old.unlink()
        return output
    finally:
        partial.unlink(missing_ok=True)


def healthy():
    folder = Path(os.getenv("BACKUP_DIR", "/backups"))
    value = json.loads((folder / ".last-success.json").read_text())
    interval = max(60, int(os.getenv("BACKUP_INTERVAL_SECONDS", "86400")))
    return (folder / value["archive"]).is_file() and time.time() - value["time"] < interval + 3600


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "health":
        try:
            return 0 if healthy() else 1
        except Exception:
            return 1
    shutdown = Event()
    for name in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, name), lambda *_: shutdown.set())
    while not shutdown.is_set():
        try:
            backup_once()
            print("Database backup completed and archive verified.", flush=True)
            if len(sys.argv) > 1 and sys.argv[1] == "once":
                return 0
            shutdown.wait(max(60, int(os.getenv("BACKUP_INTERVAL_SECONDS", "86400"))))
        except Exception:
            print("Database backup failed; previous archives retained. Check connectivity, permissions, and client/server compatibility.", flush=True)
            if len(sys.argv) > 1 and sys.argv[1] == "once":
                return 1
            shutdown.wait(60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
