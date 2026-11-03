"""Dedicated queue consumer. Run independently from API and scheduler."""
import argparse
import asyncio
from dataclasses import asdict
import json
import signal
from threading import Event, Thread
import time
from uuid import uuid4

from triage.jobs import Jobs, JobCancelled, PAYLOADS
from triage.store import Store


def transient(exc):
    # No send jobs exist. Retried work can still repeat a billed model request.
    if getattr(exc, "code", None) in ("insufficient_quota", "credit_balance_exhausted", "credit_balance_exhaustion"):
        return False
    if type(exc) is ValueError:
        return str(exc).startswith("Another ingestion")
    return isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in {
        "APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError", "ConnectError", "ReadTimeout",
        "TransportError", "OperationalError", "InterfaceError", "ServerNotFoundError", "HttpLib2Error"}


class Context:
    def __init__(self, store, job, lost=None):
        self.store, self.job, self.lost = store, job, lost or Event()

    def check(self):
        if self.lost.is_set() or not Jobs(self.store).alive(self.job):
            raise JobCancelled()

    def progress(self, event):
        self.check()
        Jobs(self.store).progress(self.job, event)


def execute(store, job, context):
    from triage.cli import gmail_client
    from triage.automation import Automation

    context.check()
    payload = PAYLOADS[job["kind"]].model_validate(json.loads(job["payload"]))
    if job["kind"] == "ingest":
        from triage.ingestion import fetch_for_dashboard

        return fetch_for_dashboard(store, query=payload.query, limit=payload.limit, mode=payload.mode, on_progress=context.progress)
    if job["kind"] == "draft_reply":
        from triage.drafting import generate_draft

        row = store.get(payload.row_id)
        if row["source"] != "gmail":
            raise ValueError("Sample records cannot draft Gmail replies.")
        previous = store.reply(payload.row_id)
        if previous and previous["state"] != "draft":
            raise ValueError("This reply is already sent, sending, or uncertain.")
        client = gmail_client()
        store.gmail_row(payload.row_id, client)
        original = client.original(row["email_id"])
        context.check()
        draft = generate_draft(original["email"], payload.instruction, payload.tone)
        context.check()
        with store.db:
            store.log(payload.row_id, "ai_reply_drafted", {"tone": payload.tone})
        return {"original": {**original, "email": asdict(original["email"])}, "body": draft.body, "review_notes": draft.review_notes,
            "evidence": [e.model_dump() for e in getattr(draft, "evidence", [])]}
    if job["kind"] == "daily_digest":
        from triage.summaries import Summaries, generate_digest

        if payload.schedule_owner and not Automation(store).active(payload.schedule_owner):
            raise JobCancelled()
        if payload.regenerate:
            with store.db:
                store.db.execute("DELETE FROM daily_digests WHERE id=? AND created=? AND account=? AND state='ready'", (payload.expected_digest_id, payload.expected_digest_created, payload.account))
        context.check()
        generated = asyncio.run(generate_digest(store, payload.account, payload.day.isoformat()))
        digest = next((row for row in Summaries(store).digests(payload.account) if row["day"] == payload.day.isoformat()), None)
        if digest and digest["state"] == "failed":
            raise ValueError("Digest failed; check configuration/credits and clear the failed attempt before requesting another.")
        return {"generated": generated}
    if job["kind"] == "scheduled_triage":
        from triage.worker import run_cycle
        from triage.db_locks import store_lock

        if not Automation(store).active(payload.schedule_owner):
            raise JobCancelled()
        with store_lock(store, "scheduled-cycle"):
            outcome = asyncio.run(run_cycle(store, payload.schedule_owner, on_progress=context.progress, raise_errors=True, check_cancelled=context.check))
        if outcome == "stopped":
            raise JobCancelled()
        return {"outcome": outcome}
    raise ValueError("Unsupported job kind.")


def process_once(path, *, database_url=None, worker_id="worker", heartbeat=True):
    store = Store(path, database_url=database_url)
    try:
        return _process_store(store, path, database_url=database_url, worker_id=worker_id, heartbeat=heartbeat)
    finally:
        store.close()


def _process_store(store, path, *, database_url, worker_id, heartbeat):
    from triage.request_context import request_id, job_id

    queue = Jobs(store)
    job = queue.claim(worker_id)
    if not job:
        queue.heartbeat(worker_id, "worker")
        return False
    stop, lost = Event(), Event()

    def renew():
        connection = None
        started = time.monotonic()
        try:
            connection = Store(path, database_url=database_url)
            while not stop.wait(5):
                if time.monotonic() - started > 1800 or not Jobs(connection).renew(job):
                    lost.set()
                    break
                Jobs(connection).heartbeat(worker_id, "worker", "busy", job["id"])
        except Exception:
            lost.set()
        finally:
            if connection:
                connection.close()

    thread = Thread(target=renew, daemon=True) if heartbeat else None
    request_token = request_id.set(job["request_id"])
    job_token = job_id.set(job["id"])
    try:
        queue.heartbeat(worker_id, "worker", "busy", job["id"])
        if thread:
            thread.start()
        context = Context(store, job, lost)
        from triage.usage import capture_usage
        with capture_usage(lambda event: queue.usage(job, event)):
            result = execute(store, job, context)
        context.check()
        queue.finish(job, result)
    except JobCancelled:
        queue.finish(job, cancelled=True, error="Cancellation or lost lease observed. In-flight work may have completed and remains saved.")
    except Exception as exc:
        queue.finish(job, error=f"{type(exc).__name__}: Job failed. Check configuration, authorization, credits, or connectivity. Completed work remains saved.", retry=transient(exc))
    finally:
        stop.set()
        if thread and thread.is_alive():
            thread.join(timeout=15)
        request_id.reset(request_token)
        job_id.reset(job_token)
        queue.heartbeat(worker_id, "worker")
    return True


def main():
    from triage import settings

    parser = argparse.ArgumentParser(description="Consume durable Gmail triage jobs (never sends replies).")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--name", default="worker-" + uuid4().hex[:12])
    args = parser.parse_args()
    shutdown = Event()
    for name in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, name), lambda *_: shutdown.set())
    print(f"Queue worker {args.name} ready. Ctrl+C stops after the current job.", flush=True)
    try:
        while not shutdown.is_set():
            try:
                worked = process_once(settings.database_path(), database_url=settings.database_url(), worker_id=args.name)
            except Exception:
                if args.once:
                    raise
                print("Worker database operation failed; retrying. Claimed jobs recover after their lease expires.", flush=True)
                shutdown.wait(5)
                continue
            if args.once:
                break
            if not worked:
                shutdown.wait(1)
    finally:
        try:
            with_store = settings.open_store()
            try:
                Jobs(with_store).heartbeat(args.name, "worker", "stopped")
            finally:
                with_store.close()
        except Exception:
            print("Worker stopped; final heartbeat could not be saved.", flush=True)


if __name__ == "__main__":
    main()
