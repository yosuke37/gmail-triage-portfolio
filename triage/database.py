"""SQLAlchemy connection boundary for SQLite and PostgreSQL.

Domain queries keep bound positional values; schema/locking/dialect differences
are centralized here rather than spread through application services.
"""
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool


def normalized_url(path=None, database_url=None):
    if database_url:
        url = make_url(database_url)
        if url.get_backend_name() != "postgresql":
            raise ValueError("TRIAGE_DATABASE_URL must be a PostgreSQL URL; use TRIAGE_DB_PATH for SQLite.")
        return url.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
    return "sqlite+pysqlite:///" + Path(path).resolve().as_posix()


@lru_cache(maxsize=16)
def engine_for(url):
    if url.startswith("sqlite"):
        engine = create_engine(url, poolclass=NullPool, connect_args={"timeout": 30})

        @event.listens_for(engine, "connect")
        def sqlite_options(dbapi_connection, record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
            dbapi_connection.execute("PRAGMA busy_timeout=30000")
    else:
        engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5,
                               connect_args={"connect_timeout": 10}, hide_parameters=True)
    return engine


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Result:
    def __init__(self, result):
        self.result = result
        self.rowcount = result.rowcount

    def fetchone(self):
        row = self.result.mappings().fetchone()
        return Row(row) if row is not None else None

    def fetchall(self):
        return [Row(row) for row in self.result.mappings().fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


def bind_query(query, parameters):
    """Convert placeholders outside SQL literals to SQLAlchemy bound values."""
    if isinstance(parameters, Mapping):
        return text(query), parameters
    output, index, quote = [], 0, None
    position = 0
    while position < len(query):
        char = query[position]
        if quote:
            output.append(char)
            if char == quote:
                if position + 1 < len(query) and query[position + 1] == quote:
                    output.append(query[position + 1])
                    position += 1
                else:
                    quote = None
        elif char in ("'", '"'):
            quote = char
            output.append(char)
        elif char == "?":
            output.append(f":p{index}")
            index += 1
        else:
            output.append(char)
        position += 1
    if index != len(parameters):
        raise ValueError("Incorrect number of database query parameters.")
    return text("".join(output)), {f"p{i}": int(value) if isinstance(value, bool) else value for i, value in enumerate(parameters)}


class Database:
    def __init__(self, path, database_url=None):
        self.url = normalized_url(path, database_url)
        self.engine = engine_for(self.url)
        self.backend = self.engine.dialect.name
        from triage.db_migrate import ensure_ready

        ensure_ready(self.engine, path)
        self.connection = self.engine.connect()
        self.transaction = None

    def execute(self, query, parameters=()):
        if query.strip().upper() == "BEGIN IMMEDIATE":
            return None  # Transaction start belongs to this boundary now.
        statement, values = bind_query(query, parameters)
        result = self.connection.execute(statement, values)
        return Result(result)

    def __enter__(self):
        if self.transaction is not None:
            raise ValueError("Nested write transactions are not supported.")
        if self.connection.in_transaction():
            self.connection.rollback()  # End a previous read-only autobegin.
        self.transaction = self.connection.begin()
        if self.backend == "sqlite":
            self.connection.exec_driver_sql("BEGIN IMMEDIATE")
        return self

    def __exit__(self, kind, exc, traceback):
        transaction, self.transaction = self.transaction, None
        if kind:
            transaction.rollback()
        else:
            transaction.commit()

    def lock_email(self, row_id):
        if self.backend == "postgresql":
            self.execute("SELECT id FROM emails WHERE id=? FOR UPDATE", (row_id,)).fetchone()

    def lock_automation(self):
        if self.backend == "postgresql":
            self.execute("SELECT id FROM automation WHERE id=1 FOR UPDATE").fetchone()

    def close(self):
        self.connection.close()
