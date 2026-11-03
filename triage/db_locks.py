"""Session advisory locks for distributed PostgreSQL workers."""
from contextlib import contextmanager
import hashlib

from sqlalchemy import text


@contextmanager
def engine_lock(engine, purpose, *, wait=True):
    if engine.dialect.name != "postgresql":
        yield
        return
    key = int.from_bytes(hashlib.sha256(f"gmail-triage:{purpose}".encode()).digest()[:8], "big", signed=True)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        if wait:
            connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        elif not connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar_one():
            raise ValueError(f"Another {purpose} operation is running for this database.")
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


@contextmanager
def store_lock(store, purpose):
    if store.db.backend == "postgresql":
        with engine_lock(store.db.engine, purpose, wait=False):
            yield
    else:
        from triage.ingestion import ingestion_lock

        path = store.path if purpose == "ingestion" else store.path.with_name(store.path.stem + "." + purpose + ".sqlite3")
        with ingestion_lock(path):
            yield
