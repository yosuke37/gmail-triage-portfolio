"""Scheduled triage domain operation, executed by the dedicated queue worker."""
from types import SimpleNamespace

from triage.automation import Automation, apply_rules
from triage.store import now
from triage.usage import capture_usage


class StopRequested(Exception):
    pass


async def run_cycle(store, owner, *, on_progress=None, raise_errors=False, check_cancelled=None):
    from triage.cli import gmail_client, ingest

    auto = Automation(store)
    config = auto.config()
    if not auto.active(owner):
        return "stopped"
    remaining = config.daily_limit - auto.daily_processed()
    if remaining <= 0:
        auto.heartbeat(owner, "daily limit reached")
        return "capped"
    state = auto.state()
    with store.db:
        run_id = store.db.execute("INSERT INTO automation_runs(started,state,account,mode) VALUES (?,'running',?,?) RETURNING id", (now(), state["account"], config.mode)).fetchone()["id"]
    auto.heartbeat(owner, "running")
    row_ids = []

    def progress(event):
        if event["stage"] == "saved":
            with store.db:
                store.db.execute("UPDATE automation_runs SET processed=? WHERE id=?", (event["processed"], run_id))
                store.db.execute("INSERT INTO automation_processed(run_id,email_id,account,created) VALUES (?,?,?,?) ON CONFLICT(run_id,email_id,account) DO NOTHING", (run_id, event["email_id"], event["account"], now()))
            row = store.db.execute("SELECT id FROM emails WHERE source='gmail' AND account=? AND email_id=?", (event["account"], event["email_id"])).fetchone()
            row_ids.append(row["id"])
        if on_progress:
            on_progress(event)
        if not auto.active(owner):
            raise StopRequested()
        auto.heartbeat(owner, "running")
        if event["stage"] != "saved" and auto.daily_processed() >= auto.config().daily_limit:
            raise StopRequested()

    def usage(event):
        with store.db:
            store.db.execute("""UPDATE automation_runs SET requests=requests+?,input_tokens=input_tokens+?,
                output_tokens=output_tokens+?,total_tokens=total_tokens+? WHERE id=?""", (event["requests"], event["input_tokens"], event["output_tokens"], event["total_tokens"], run_id))

    outcome = "completed"
    error = None
    try:
        client = gmail_client()
        if client.account != state["account"]:
            raise ValueError("The Gmail authorization account changed. Stop and restart with the intended account.")
        args = SimpleNamespace(source="gmail", query=config.query, limit=min(config.batch_size, remaining), mode=config.mode)
        with capture_usage(usage):
            await ingest(args, store, client, on_progress=progress, quiet=True)
            from triage.summaries import suggest_reply

            for row_id in row_ids:
                if check_cancelled:
                    check_cancelled()
                if not auto.active(owner):
                    raise StopRequested()
                if auto.config().suggested_reply_labels:
                    auto.heartbeat(owner, "drafting suggested reply")
                    await suggest_reply(store, auto, row_id, client, owner)
        # Only this cycle's newly saved items and interrupted automation applies
        # are candidates. Old pending/manual approvals are never bulk-applied.
        if auto.active(owner) and auto.config().auto_labels:
            write = gmail_client(writable=True)
            retry_ids = [r[0] for r in store.db.execute("""SELECT e.id FROM emails e JOIN audit a ON a.email_row=e.id
                WHERE e.account=? AND e.status='approved' AND a.event='automation_approved'""", (state["account"],))]
            for row_id in dict.fromkeys(row_ids + retry_ids):
                if check_cancelled:
                    check_cancelled()
                if not auto.active(owner):
                    raise StopRequested()
                if apply_rules(store, auto, row_id, write, owner):
                    with store.db:
                        store.db.execute("UPDATE automation_runs SET applied=applied+1 WHERE id=?", (run_id,))
    except StopRequested:
        outcome = "stopped"
    except Exception as exc:
        outcome = "failed"
        error = f"{type(exc).__name__}: Check Gmail authorization, API credits/model access, and network connection. Completed suggestions remain saved."
        if raise_errors:
            raise
    finally:
        with store.db:
            store.db.execute("UPDATE automation_runs SET state=?,finished=?,error=? WHERE id=?", (outcome, now(), error, run_id))
    return outcome


if __name__ == "__main__":
    raise SystemExit("Use python -m triage.job_worker and python -m triage.scheduler; the legacy combined service is retired.")
