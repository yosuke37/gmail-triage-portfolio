import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)
from triage.secrets_config import load_provider_secrets, secret_value

load_provider_secrets()
# Do not export email content through framework observability integrations.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"


def local_path(variable: str, default: str) -> Path:
    path = Path(os.environ.get(variable, default)).expanduser()
    return path if path.is_absolute() else ROOT / path


def database_path() -> Path:
    return local_path("TRIAGE_DB_PATH", "output/triage.sqlite3")


def database_url():
    return secret_value("TRIAGE_DATABASE_URL") or None


def open_store():
    from triage.store import Store

    return Store(database_path(), database_url=database_url())


# Psycopg's async connections require a selector event loop on Windows.
if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def classifier(mode: str):
    if mode == "ai":
        if not os.environ.get("OPENAI_API_KEY") or not os.environ.get("OPENAI_MODEL"):
            raise ValueError("Set OPENAI_API_KEY and OPENAI_MODEL in your local .env first.")
        from triage.classifier import make_ai_classifier

        return make_ai_classifier(os.environ["OPENAI_MODEL"])
    from triage.core import demo_classify

    async def classify(email):
        return demo_classify(email)

    return classify
