"""Backend-specific LangGraph persistence; provider tables own their migrations."""
from contextlib import asynccontextmanager

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy.engine import make_url


def native_url(database_url):
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


async def setup_postgres(database_url):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(native_url(database_url)) as saver:
        await saver.setup()


@asynccontextmanager
async def checkpoint_saver(store):
    if store.db.backend == "postgresql":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        context = AsyncPostgresSaver.from_conn_string(native_url(store.db.url))
    else:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        context = AsyncSqliteSaver.from_conn_string(str(store.path.with_suffix(".checkpoints.sqlite3")))
    async with context as saver:
        saver.serde = JsonPlusSerializer(allowed_msgpack_modules=[("triage.core", "Email"), ("triage.core", "Suggestion")])
        yield saver
