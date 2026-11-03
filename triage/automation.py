"""Persistent local automation settings, state, rules, and process controls."""
import json
import time
from uuid import uuid4
from dataclasses import dataclass, asdict, field

from triage.core import LABELS
from triage.store import now


@dataclass
class Config:
    interval_minutes: int = 5
    batch_size: int = 5
    daily_limit: int = 50
    query: str = "in:inbox newer_than:7d"
    mode: str = "ai"
    auto_labels: list[str] = field(default_factory=list)
    suggested_reply_labels: list[str] = field(default_factory=list)
    reply_tone: str = "Professional"
    reply_instruction: str = "Draft a brief acknowledgment. Ask for clarification if needed; do not commit to dates, purchases, or actions."
    digest_enabled: bool = False
    digest_hour: int = 18
    digest_minute: int = 0

    def validate(self):
        for name, low, high in (("interval_minutes", 1, 1440), ("batch_size", 1, 100), ("daily_limit", 1, 1000)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}.")
        if not self.query.strip() or len(self.query) > 500:
            raise ValueError("Enter a Gmail search query of 1–500 characters.")
        if self.mode not in ("ai", "demo") or any(label not in LABELS for label in self.auto_labels + self.suggested_reply_labels):
            raise ValueError("Invalid classification mode or automatic label rule.")
        if self.mode == "demo" and self.auto_labels:
            raise ValueError("Automatic label changes require AI mode; demo remains preview-only.")
        from triage.drafting import TONES

        if self.reply_tone not in TONES or not self.reply_instruction.strip() or len(self.reply_instruction) > 2000:
            raise ValueError("Choose a supported reply tone and an instruction of 1–2,000 characters.")
        if type(self.digest_enabled) is not bool or type(self.digest_hour) is not int or not 0 <= self.digest_hour <= 23 or type(self.digest_minute) is not int or not 0 <= self.digest_minute <= 59:
            raise ValueError("Choose a valid daily digest time.")
        if self.mode != "ai" and (self.suggested_reply_labels or self.digest_enabled):
            raise ValueError("Suggested replies and AI daily digests require AI mode.")
        return self


class Automation:
    def __init__(self, store):
        self.store = store
        self.db = store.db
        with self.db:
            self.db.execute("INSERT INTO automation(id,config,updated) VALUES (1,?,?) ON CONFLICT(id) DO NOTHING", (json.dumps(asdict(Config())), now()))

    def state(self):
        return dict(self.db.execute("SELECT * FROM automation WHERE id=1").fetchone())

    def config(self):
        return Config(**json.loads(self.state()["config"])).validate()

    def save(self, config):
        config.validate()
        with self.db:
            self.db.execute("UPDATE automation SET config=?,updated=? WHERE id=1", (json.dumps(asdict(config)), now()))

    def stop(self):
        with self.db:
            self.db.lock_automation()
            owner = self.state()["owner"]
            self.db.execute("UPDATE automation SET state='stopped',enabled=0,next_run=NULL,updated=? WHERE id=1", (now(),))
            for row in self.db.execute("SELECT id,payload FROM jobs WHERE state IN ('queued','running') AND kind IN ('scheduled_triage','daily_digest')"):
                if owner and json.loads(row["payload"]).get("schedule_owner") == owner:
                    self.db.execute("""UPDATE jobs SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,
                        finished=CASE WHEN state='queued' THEN ? ELSE finished END WHERE id=? AND state IN ('queued','running')""", (now(), row["id"]))

    def active(self, owner):
        state = self.state()
        return bool(state["enabled"] and state["owner"] == owner)

    def heartbeat(self, owner, state="idle", next_run=None):
        with self.db:
            self.db.execute("UPDATE automation SET heartbeat=?,state=?,next_run=? WHERE id=1 AND owner=? AND enabled=1", (time.time(), state, next_run, owner))

    def runs(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM automation_runs ORDER BY id DESC LIMIT 50")]

    def daily_processed(self):
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        midnight = datetime.now(ZoneInfo("America/New_York")).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        return self.db.execute("SELECT COUNT(*) FROM automation_processed WHERE created>=?", (midnight,)).fetchone()[0]


def start_worker(store):
    """Enable persistent scheduling; service processes are managed separately."""
    from triage import settings
    from triage.cli import gmail_client

    automation = Automation(store)
    config = automation.config()
    settings.classifier(config.mode)
    read = gmail_client()
    if config.auto_labels and gmail_client(writable=True).account != read.account:
        raise ValueError("Read and write authorizations must use the same Gmail account.")
    with store.db:
        store.db.lock_automation()
        if automation.state()["enabled"]:
            raise ValueError("Scheduled triage is already enabled.")
        owner = uuid4().hex
        store.db.execute("UPDATE automation SET enabled=1,owner=?,account=?,state='scheduled',heartbeat=NULL,pid=NULL,next_run=0,updated=? WHERE id=1", (owner, read.account, now()))


def apply_rules(store, automation, row_id, gmail, owner):
    """Rules are deterministic and checked again immediately before each action."""
    config = automation.config()
    from triage.proposals import can_automate

    if not can_automate(store, row_id, kind="set_label", config=config, account=gmail.account):
        return False
    row = store.get(row_id)
    if not automation.active(owner) or row["source"] != "gmail" or config.mode != "ai" or row["mode"] != "ai" or row["needs_review"] or row["label"] not in config.auto_labels:
        return False
    if row["account"] != automation.state()["account"] or row["account"] != gmail.account:
        raise ValueError("Automation account does not match this Gmail message.")
    if row["status"] == "pending":
        with store.db:
            # Never overwrite a decision that the user made in another process.
            changed = store.db.execute("UPDATE emails SET status='approved' WHERE id=? AND status='pending' AND label=? AND needs_review=0", (row_id, row["label"])).rowcount
            if not changed:
                return False
            store.log(row_id, "automation_approved", {"label": row["label"]})
    elif row["status"] == "approved":
        # Retry only approval created by this automation, not manual approvals.
        decision = store.db.execute("SELECT event FROM audit WHERE email_row=? AND event IN ('approved','rejected','automation_approved') ORDER BY id DESC LIMIT 1", (row_id,)).fetchone()
        if not decision or decision["event"] != "automation_approved":
            return False
    else:
        return False
    if not automation.active(owner) or row["label"] not in automation.config().auto_labels:
        return False
    store.apply(row_id, gmail, expected_label=row["label"])
    return True
