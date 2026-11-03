"""One-way, transactional import from a SQLite snapshot into an empty PostgreSQL DB."""
import importlib
from pathlib import Path

from sqlalchemy import MetaData, Table, func, inspect, select, text

from triage.database import engine_for, normalized_url
from triage.db_locks import engine_lock
from triage.db_migrate import backup_sqlite, heads, revisions


def import_sqlite(source_path, target_engine):
    source_path = Path(source_path)
    if not source_path.is_file() or target_engine.dialect.name != "postgresql":
        raise ValueError("Import requires an existing SQLite source and a PostgreSQL target.")
    if revisions(target_engine) != heads():
        raise ValueError("Upgrade the PostgreSQL schema before importing.")
    snapshot = backup_sqlite(source_path)
    source_engine = engine_for(normalized_url(snapshot))
    metadata = importlib.import_module("triage.migrations.versions.0001_initial").schema()
    counts = {}
    with engine_lock(target_engine, "import"):
        with source_engine.connect() as source, target_engine.begin() as target:
            for connection in (source, target):
                if "jobs" in inspect(connection).get_table_names() and connection.execute(text("SELECT 1 FROM jobs LIMIT 1")).first():
                    raise ValueError("Import with existing jobs is unsupported; archive or migrate the queue explicitly.")
            for table in metadata.sorted_tables:
                if target.execute(select(table).limit(1)).first() is not None:
                    raise ValueError("Target application tables are not empty; import was refused without overwriting data.")
            existing = set(inspect(source).get_table_names())
            for table in metadata.sorted_tables:
                if table.name not in existing:
                    counts[table.name] = 0
                    continue
                old_table = Table(table.name, MetaData(), autoload_with=source)
                if set(old_table.columns.keys()) - set(table.columns.keys()):
                    raise ValueError("Source schema contains unsupported columns; import was rolled back without dropping data.")
                count = 0
                for row in source.execute(select(old_table)).mappings():
                    record = {key: value for key, value in row.items() if key in table.c}
                    if table.name == "automation":
                        record.update(enabled=0, owner=None, pid=None, heartbeat=None, state="stopped", next_run=None)
                    target.execute(table.insert().values(**record))
                    count += 1
                counts[table.name] = count
            # Explicit imported IDs must not collide with the next generated ID.
            for table in metadata.sorted_tables:
                if "id" in table.c:
                    maximum = target.execute(select(func.max(table.c.id))).scalar()
                    target.execute(text("SELECT setval(pg_get_serial_sequence(:table, 'id'), :value, :called)"), {"table": table.name, "value": maximum or 1, "called": maximum is not None})
    return {"snapshot": str(snapshot), "counts": counts, "automation_enabled": False,
            "checkpoint_note": "SQLite checkpoint files are retained unchanged; PostgreSQL starts its own checkpoints. Completed-email deduplication is imported."}
