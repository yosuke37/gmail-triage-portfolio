"""Container/cloud commands with dependency readiness and generic errors."""
import os
import sys
import time


def wait_database(timeout=90):
    from sqlalchemy import text
    from triage import settings
    from triage.database import engine_for, normalized_url
    engine = engine_for(normalized_url(settings.database_path(), settings.database_url()))
    deadline = time.monotonic() + timeout
    while True:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise ValueError("Database unavailable; check its service and mounted connection secret.") from None
            time.sleep(2)


def main():
    role = sys.argv[1] if len(sys.argv) > 1 else "api"
    if role not in ("api", "worker", "scheduler", "dashboard", "migrate"):
        raise ValueError("Unknown service role.")
    if role == "dashboard":
        if os.getenv("TRIAGE_API_HOSTPORT"):
            origin = "http://" + os.environ["TRIAGE_API_HOSTPORT"]
            os.environ["TRIAGE_API_URL"] = origin
            os.environ["TRIAGE_INTERNAL_API_ORIGIN"] = origin
        os.execvp(sys.executable, [sys.executable, "-m", "streamlit", "run", "dashboard.py", "--server.address=0.0.0.0",
            "--server.port=" + os.getenv("PORT", "8501"), "--server.headless=true", "--server.fileWatcherType=none", "--browser.gatherUsageStats=false"])
    wait_database()
    # Compose uses a one-shot migration service. Managed platforms may opt into
    # serialized startup migration explicitly; workers always wait for schema.
    if role == "migrate" or os.getenv("TRIAGE_MIGRATE_ON_START") == "1":
        from triage.db_cli import main as migrate
        previous, sys.argv = sys.argv, ["db_cli", "upgrade"]
        try:
            if migrate():
                raise ValueError("Schema migration failed; inspect database configuration.")
        finally:
            sys.argv = previous
        if role == "migrate":
            return
    deadline = time.monotonic() + 120
    while True:
        try:
            from triage import settings
            store = settings.open_store()
            store.close()
            break
        except ValueError:
            if time.monotonic() > deadline:
                raise ValueError("Schema not ready; run migrations before starting services.") from None
            time.sleep(2)
    module = {"api": "triage.api", "worker": "triage.job_worker", "scheduler": "triage.scheduler"}.get(role)
    if not module:
        raise ValueError("Unknown service role.")
    os.execvp(sys.executable, [sys.executable, "-m", module])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc) if type(exc) is ValueError else "Service startup failed; check database and secret configuration.", file=sys.stderr)
        raise SystemExit(1)
