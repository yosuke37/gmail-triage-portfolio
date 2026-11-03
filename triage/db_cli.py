"""Database lifecycle commands; connection credentials come only from local configuration."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

from triage import settings
from triage.database import engine_for, normalized_url
from triage.db_migrate import backup_sqlite, heads, revisions, upgrade


def main():
    parser = argparse.ArgumentParser(description="Versioned database operations (credentials are never printed).")
    parser.add_argument("command", choices=("status", "upgrade", "import-sqlite", "backup"))
    parser.add_argument("--source", type=Path, help="SQLite source for import/backup; defaults to TRIAGE_DB_PATH.")
    args = parser.parse_args()
    engine = engine_for(normalized_url(settings.database_path(), settings.database_url()))
    try:
        if args.command == "status":
            print(json.dumps({"backend": engine.dialect.name, "current": sorted(revisions(engine)), "head": sorted(heads()), "current_schema": revisions(engine) == heads()}, indent=2))
        elif args.command == "upgrade":
            if engine.dialect.name == "sqlite":
                backup_sqlite(settings.database_path())
            upgrade(engine)
            if engine.dialect.name == "postgresql":
                from triage.checkpoints import setup_postgres

                asyncio.run(setup_postgres(settings.database_url()))
            print(json.dumps({"backend": engine.dialect.name, "current": sorted(revisions(engine)), "checkpoints": "PostgreSQL ready" if engine.dialect.name == "postgresql" else "SQLite remains file-backed"}))
        elif args.command == "backup":
            if engine.dialect.name == "postgresql":
                from triage.db_backup import backup_postgres

                output = backup_postgres(settings.database_url(), settings.database_path().parent / "backups")
            else:
                output = backup_sqlite(args.source or settings.database_path())
            print(json.dumps({"backup": str(output)}))
        else:
            from triage.db_import import import_sqlite

            print(json.dumps(import_sqlite(args.source or settings.database_path(), engine), indent=2))
    except Exception as exc:
        message = str(exc) if type(exc) is ValueError else "Database operation failed; verify service availability, configuration, and migration compatibility. Credentials and records are omitted."
        print(f"{type(exc).__name__}: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
