"""Dedicated scheduler: enqueue due work, never run Gmail or model operations."""
import signal
from datetime import datetime
import json
from threading import Event
from uuid import uuid4

from triage.automation import Automation
from triage.jobs import Jobs
from triage.summaries import TZ


def tick(store, *, service_id="scheduler"):
    auto, queue = Automation(store), Jobs(store)
    queue.heartbeat(service_id, "scheduler")
    state, config = auto.state(), auto.config()
    if not state["enabled"]:
        return 0
    owner, account = state["owner"], state["account"]
    if not owner or not account:
        return 0
    timestamp = queue.seconds()
    recent = [dict(row) for row in store.db.execute("SELECT * FROM jobs WHERE kind='scheduled_triage' ORDER BY created DESC LIMIT 100") if json.loads(row["payload"]).get("schedule_owner") == owner]
    terminal = [row for row in recent if row["state"] in ("succeeded", "failed")]
    if len(terminal) >= 3 and all(row["state"] == "failed" for row in terminal[:3]):
        auto.stop()
        with store.db:
            store.db.execute("UPDATE automation SET state='paused after errors' WHERE owner=?", (owner,))
        return 0
    count = 0
    active = any(row["state"] in ("queued", "running") for row in recent)
    if not active and timestamp >= (state["next_run"] or 0):
        queue.enqueue("scheduled_triage", {"schedule_owner": owner}, key=f"schedule:{owner}:{state['next_run'] or 0}")
        count += 1
        state["next_run"] = timestamp + config.interval_minutes * 60
    current = datetime.fromtimestamp(timestamp, TZ)
    if config.digest_enabled and config.mode == "ai" and (current.hour, current.minute) >= (config.digest_hour, config.digest_minute):
        day = current.date().isoformat()
        existing = store.db.execute("SELECT 1 FROM daily_digests WHERE account=? AND day=?", (account, day)).fetchone()
        if not existing:
            queue.enqueue("daily_digest", {"account": account, "day": day, "schedule_owner": owner}, key=f"digest:{owner}:{account}:{day}")
    auto.heartbeat(owner, "waiting for worker" if active or count else "scheduled", state["next_run"])
    return count


def main():
    from triage import settings
    from triage.db_locks import store_lock

    store = settings.open_store()
    service_id = "scheduler-" + uuid4().hex[:12]
    shutdown = Event()
    for name in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, name), lambda *_: shutdown.set())
    print("Scheduler ready. It enqueues work only while scheduled triage is enabled.", flush=True)
    try:
        with store_lock(store, "scheduler"):
            while not shutdown.is_set():
                try:
                    tick(store, service_id=service_id)
                except Exception:
                    print("Scheduler tick failed; check database availability. Retrying without provider calls.", flush=True)
                shutdown.wait(3)
    finally:
        try:
            Jobs(store).heartbeat(service_id, "scheduler", "stopped")
        except Exception:
            print("Scheduler stopped; final heartbeat could not be saved.", flush=True)
        finally:
            store.close()


if __name__ == "__main__":
    main()
