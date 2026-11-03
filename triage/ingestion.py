"""Dashboard adapter and a cross-process ingestion lock shared with the CLI."""
from contextlib import contextmanager
from types import SimpleNamespace


@contextmanager
def ingestion_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".ingestion.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        import os

        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Another ingestion is running for this database. Wait for it to finish before fetching again.") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def fetch_for_dashboard(store, *, query, limit, mode, on_progress):
    import asyncio
    from triage import settings
    from triage.cli import gmail_client, ingest

    if not query.strip():
        raise ValueError("Enter a Gmail search query.")
    if not 1 <= limit <= 100:
        raise ValueError("Batch size must be between 1 and 100.")
    if mode not in ("ai", "demo"):
        raise ValueError("Choose AI or demo mode.")
    settings.classifier(mode)  # Validate before contacting Gmail.
    args = SimpleNamespace(source="gmail", query=query, limit=limit, mode=mode)
    client = gmail_client()
    return asyncio.run(ingest(args, store, client, on_progress=on_progress, quiet=True))
