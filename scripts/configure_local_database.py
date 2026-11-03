"""Create the app database/role without changing the running app's configuration."""
import json
from pathlib import Path
import secrets

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ROOT / ".secrets"


def main():
    config = json.loads((PRIVATE / "local_postgres.json").read_text())
    credential_file = PRIVATE / "postgres_app_password"
    if not credential_file.exists():
        credential_file.write_text(secrets.token_hex(24), encoding="utf-8")
    password = credential_file.read_text().strip()
    with psycopg.connect(host="127.0.0.1", port=config["port"], user="triage_admin", password=config["password"], dbname="postgres", autocommit=True) as connection:
        if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", ("triage",)).fetchone():
            connection.execute(sql.SQL("CREATE ROLE triage LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {} ").format(sql.Literal(password)))
        if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", ("triage",)).fetchone():
            connection.execute("CREATE DATABASE triage OWNER triage")
    (PRIVATE / "postgres_app_url").write_text(f"postgresql+psycopg://triage:{password}@127.0.0.1:{config['port']}/triage", encoding="utf-8")
    print("Local application database/role ready. URL saved privately; the app has not been switched.")


if __name__ == "__main__":
    main()
