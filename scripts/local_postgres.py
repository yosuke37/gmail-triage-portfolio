"""Workspace-local PostgreSQL for development. No system service or public bind.

Requires the official EDB Windows binary archive extracted under .tools.
Passwords and DSNs are written only under .secrets; never printed or put in argv.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / ".tools" / "postgresql-18.6-5" / "pgsql" / "bin"
DATA = ROOT / ".local" / "postgres"
PRIVATE = ROOT / ".secrets"
PORT = 55432


def run(command, *, env=None):
    options = {"text": True, "env": env, "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0}
    if Path(command[0]).name == "pg_ctl.exe":
        # A database child must not inherit captured pipe handles and hold the
        # launching terminal's completion open for the lifetime of the server.
        with (ROOT / ".local" / "postgres-control.log").open("a", encoding="utf-8") as log:
            result = subprocess.run([str(x) for x in command], stdout=log, stderr=log, **options)
    else:
        result = subprocess.run([str(x) for x in command], capture_output=True, **options)
    if result.returncode:
        detail = (result.stderr or result.stdout or "See .local/postgres-control.log.").strip()
        private_config = PRIVATE / "local_postgres.json"
        if private_config.exists():
            detail = detail.replace(json.loads(private_config.read_text())["password"], "[redacted]")
        raise RuntimeError(f"{Path(command[0]).name} failed: {detail[-1500:]}")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "start", "stop", "status"))
    args = parser.parse_args()
    if os.name != "nt" or not (BIN / "pg_ctl.exe").exists():
        raise ValueError("Extract the official PostgreSQL 18.6-5 Windows binaries first; see docs/DATABASE.md.")
    PRIVATE.mkdir(exist_ok=True)
    DATA.parent.mkdir(exist_ok=True)
    config_path = PRIVATE / "local_postgres.json"
    if args.command == "init":
        if (DATA / "PG_VERSION").exists():
            raise ValueError("Local cluster already exists. Use start; initialization never overwrites an existing cluster.")
        config = {"password": secrets.token_hex(24), "port": PORT}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        password_file = PRIVATE / "postgres_init_password"
        password_file.write_text(config["password"], encoding="utf-8")
        run([BIN / "initdb.exe", "-D", DATA, "-U", "triage_admin", "--auth-host=scram-sha-256", "--auth-local=scram-sha-256", "--encoding=UTF8", "--locale=C", "--pwfile", password_file])
        with (DATA / "postgresql.conf").open("a", encoding="utf-8") as handle:
            handle.write(f"\nlisten_addresses = '127.0.0.1'\nport = {PORT}\nmax_connections = 60\n")
        print("Local PostgreSQL cluster initialized. Credentials saved privately under .secrets.")
    elif args.command == "start":
        status = subprocess.run([str(BIN / "pg_ctl.exe"), "-D", str(DATA), "status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        if status.returncode == 0:
            print(f"PostgreSQL already running on 127.0.0.1:{PORT}.")
            return
        run([BIN / "pg_ctl.exe", "-D", DATA, "-l", ROOT / ".local" / "postgres.log", "-w", "start"])
        print(f"PostgreSQL is listening on 127.0.0.1:{PORT}.")
    elif args.command == "stop":
        run([BIN / "pg_ctl.exe", "-D", DATA, "-m", "fast", "-w", "stop"])
        print("Local PostgreSQL stopped.")
    else:
        result = subprocess.run([str(BIN / "pg_ctl.exe"), "-D", str(DATA), "status"], capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        print("Local PostgreSQL running." if result.returncode == 0 else "Local PostgreSQL not running.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc) if type(exc) in (ValueError, RuntimeError) else "Local PostgreSQL setup failed; secrets omitted.", file=sys.stderr)
        raise SystemExit(1)
