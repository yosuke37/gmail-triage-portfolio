"""Versioned schema operations. PostgreSQL runtime never creates schema implicitly."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from contextlib import closing

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory


def config(connection=None):
    result = Config()
    result.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    if connection is not None:
        result.attributes["connection"] = connection
    return result


def heads():
    return set(ScriptDirectory.from_config(config()).get_heads())


def revisions(engine):
    with engine.connect() as connection:
        return set(MigrationContext.configure(connection).get_current_heads())


def backup_sqlite(path):
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return None
    destination = path.parent / "backups" / f"{path.stem}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)
    return destination


def upgrade(engine):
    # Serialize migration runners, including across PostgreSQL clients/hosts.
    from triage.db_locks import engine_lock

    with engine_lock(engine, "schema"):
        with engine.begin() as connection:
            if engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            command.upgrade(config(connection), "head")


def ensure_ready(engine, path):
    if revisions(engine) == heads():
        return
    if engine.dialect.name == "postgresql":
        raise ValueError("Database schema is not current. Run: python -m triage.db_cli upgrade")
    # SQLite keeps zero-setup development and safely adopts legacy local data.
    from triage.ingestion import ingestion_lock

    with ingestion_lock(Path(path).with_suffix(".schema.sqlite3")):
        if revisions(engine) != heads():
            backup_sqlite(path)
            upgrade(engine)
