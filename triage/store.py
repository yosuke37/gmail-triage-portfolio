"""Durable per-email progress and review/audit records, stored only locally."""
import json
from pathlib import Path
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path, *, database_url=None):
        from triage.database import Database

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.path, database_url)

    def close(self):
        self.db.close()

    def proposal(self, row_id):
        row = self.db.execute("SELECT payload FROM proposals WHERE email_row=?", (row_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def gmail_row(self, row_id, gmail):
        row = self.get(row_id)
        if row["source"] != "gmail" or row["account"] != gmail.account:
            raise ValueError("Review item does not belong to this Gmail account.")
        return row

    def read_state(self, row_id, gmail, *, unread):
        with self.db:
            self.db.lock_email(row_id)
            row = self.gmail_row(row_id, gmail)
            before = "UNREAD" in gmail.labels(row["email_id"])
            gmail.set_unread(row["email_id"], unread)
            self.log(row_id, "marked_unread" if unread else "marked_read", {"previous_unread": before})
        return "marked unread" if unread else "marked read"

    def reply(self, row_id):
        row = self.db.execute("SELECT * FROM replies WHERE email_row=?", (row_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def prepare_reply(self, row_id, gmail, body):
        from email.utils import make_msgid

        if not body.strip() or len(body) > 20000:
            raise ValueError("Reply text must contain 1–20,000 characters.")
        row = self.gmail_row(row_id, gmail)
        payload = {**gmail.reply_context(row["email_id"]), "body": body,
                   "message_id": make_msgid(domain=gmail.account.split("@")[-1])}
        with self.db:
            self.db.lock_email(row_id)
            previous = self.reply(row_id)
            if previous and previous["state"] != "draft":
                raise ValueError("This reply is already sending, sent, or uncertain; it cannot be replaced.")
            self.db.execute("""INSERT INTO replies(email_row,payload,created) VALUES (?,?,?)
                ON CONFLICT(email_row) DO UPDATE SET payload=excluded.payload""", (row_id, json.dumps(payload), now()))
            self.log(row_id, "reply_prepared", {"recipient": payload["recipient"]})
        return self.reply(row_id)

    def send_reply(self, row_id, gmail, *, expected_message_id):
        with self.db:
            self.db.lock_email(row_id)
            self.gmail_row(row_id, gmail)
            reply = self.reply(row_id)
            if not reply or reply["state"] != "draft":
                raise ValueError("No unsent reply draft. A previous send must not be retried automatically.")
            if reply["payload"]["message_id"] != expected_message_id:
                raise ValueError("Draft changed. Review the latest recipient and message again before sending.")
            self.db.execute("UPDATE replies SET state='sending' WHERE email_row=?", (row_id,))
            self.log(row_id, "reply_send_started", {"recipient": reply["payload"]["recipient"]})
        try:
            message_id = gmail.send_reply(reply["payload"])
        except Exception:
            with self.db:
                self.db.execute("UPDATE replies SET state='uncertain' WHERE email_row=?", (row_id,))
                self.log(row_id, "reply_send_uncertain", {})
            raise ValueError("Send outcome is uncertain. Check Gmail Sent before taking any further action; automatic resending is blocked.") from None
        with self.db:
            self.db.execute("UPDATE replies SET state='sent',sent_message_id=? WHERE email_row=?", (message_id, row_id))
            self.log(row_id, "reply_sent", {"sent_message_id": message_id})
        return "reply sent"

    def rows(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM emails ORDER BY id DESC")]

    def get(self, row_id):
        row = self.db.execute("SELECT * FROM emails WHERE id=?", (row_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown review ID.")
        return dict(row)

    def seen(self, source, account, email_id, mode=None):
        row = self.db.execute("SELECT mode,status FROM emails WHERE source=? AND account=? AND email_id=?", (source, account, email_id)).fetchone()
        if row is None:
            return False
        return not (mode == "ai" and row["mode"] == "demo" and row["status"] == "pending")

    def add(self, source, account, email, suggestion, mode):
        from triage.core import preview_record

        preview_record(email, suggestion)  # Validate the model's label before persisting.
        with self.db:
            changed = self.db.execute("""INSERT INTO emails
                (source,account,email_id,subject,sender,label,reason,needs_review,mode,created)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source,account,email_id) DO UPDATE SET
                    label=excluded.label,reason=excluded.reason,needs_review=excluded.needs_review,mode=excluded.mode
                WHERE emails.mode='demo' AND excluded.mode='ai' AND emails.status='pending'
                """, (source, account, email.id, email.subject, email.sender, suggestion.label, suggestion.reason, suggestion.needs_review, mode, now()))
            if changed.rowcount and suggestion.proposal:
                row_id = self.db.execute("SELECT id FROM emails WHERE source=? AND account=? AND email_id=?", (source, account, email.id)).fetchone()["id"]
                self.db.execute("INSERT INTO proposals(email_row,payload,created) VALUES (?,?,?) ON CONFLICT(email_row) DO UPDATE SET payload=excluded.payload,created=excluded.created", (row_id, json.dumps(suggestion.proposal), now()))
            if changed.rowcount:
                from triage.attachments import attachment_context
                row_id = self.db.execute("SELECT id FROM emails WHERE source=? AND account=? AND email_id=?", (source, account, email.id)).fetchone()["id"]
                self.db.execute("DELETE FROM email_attachments WHERE email_row=?", (row_id,))
                for attachment in attachment_context(email):
                    self.db.execute("INSERT INTO email_attachments(email_row,attachment_id,payload,created) VALUES (?,?,?,?)", (row_id, attachment["id"], json.dumps(attachment), now()))

    def attachments(self, row_id):
        return [json.loads(row["payload"]) for row in self.db.execute("SELECT payload FROM email_attachments WHERE email_row=? ORDER BY attachment_id", (row_id,))]

    def log(self, row_id, event, details):
        from triage.request_context import request_id, job_id

        if request_id.get():
            details = {**details, "request_id": request_id.get()}
        if job_id.get():
            details = {**details, "job_id": job_id.get()}
        self.db.execute("INSERT INTO audit(email_row,event,details,created) VALUES (?,?,?,?)", (row_id, event, json.dumps(details), now()))

    def review(self, row_id, label, *, approve):
        from triage.core import LABELS

        if label not in LABELS:
            raise ValueError("Unknown label.")
        with self.db:
            self.db.lock_email(row_id)
            row = self.get(row_id)
            if row["status"] not in ("pending", "approved", "rejected") or self.db.execute("SELECT 1 FROM actions WHERE email_row=?", (row_id,)).fetchone():
                raise ValueError("This email already has an action; it cannot be reviewed again.")
            status = "approved" if approve else "rejected"
            self.db.execute("UPDATE emails SET label=?,status=? WHERE id=?", (label, status, row_id))
            self.log(row_id, status, {"label": label})

    def apply(self, row_id, gmail, *, expected_label=None):
        # Serialize all review/apply/undo operations across CLI/dashboard processes.
        # Persist intent before the network call, so a crashed operation can resume.
        with self.db:
            self.db.lock_email(row_id)
            row = self.get(row_id)
            if expected_label is not None and row["label"] != expected_label:
                raise ValueError("The reviewed label changed; automation will not apply this item.")
            if row["source"] != "gmail" or row["account"] != gmail.account:
                raise ValueError("Review item does not belong to this Gmail account.")
            action = self.db.execute("SELECT * FROM actions WHERE email_row=?", (row_id,)).fetchone()
            if row["status"] == "applied":
                return "already applied"
            if row["status"] != "approved":
                raise ValueError("Approve this item in the dashboard first.")
            if action and action["state"] != "prepared":
                raise ValueError("This action cannot be reapplied.")
            if not action:
                label_id = gmail.label_id(row["label"])
                existing = label_id in gmail.labels(row["email_id"])
                self.db.execute("INSERT INTO actions(email_row,label_id,label,preexisting,state,created) VALUES (?,?,?,?,?,?)", (row_id, label_id, row["label"], existing, "prepared", now()))
                self.log(row_id, "apply_prepared", {"label_id": label_id, "preexisting": existing})
        with self.db:
            self.db.lock_email(row_id)
            action = self.db.execute("SELECT * FROM actions WHERE email_row=?", (row_id,)).fetchone()
            if action["state"] == "applied":
                return "already applied"
            if action["state"] != "prepared":
                raise ValueError("Action is no longer pending.")
            if not action["preexisting"]:
                gmail.modify(row["email_id"], action["label_id"])
            self.db.execute("UPDATE actions SET state='applied' WHERE email_row=?", (row_id,))
            self.db.execute("UPDATE emails SET status='applied' WHERE id=?", (row_id,))
            self.log(row_id, "applied", {"label_id": action["label_id"]})
        return "applied"

    def undo(self, row_id, gmail):
        with self.db:
            self.db.lock_email(row_id)
            row = self.get(row_id)
            action = self.db.execute("SELECT * FROM actions WHERE email_row=?", (row_id,)).fetchone()
            if row["source"] != "gmail" or row["account"] != gmail.account:
                raise ValueError("Review item does not belong to this Gmail account.")
            if not action or action["state"] != "applied":
                raise ValueError("No applied action to undo.")
            if not action["preexisting"]:
                gmail.modify(row["email_id"], action["label_id"], remove=True)
            self.db.execute("UPDATE actions SET state='undone' WHERE email_row=?", (row_id,))
            self.db.execute("UPDATE emails SET status='undone' WHERE id=?", (row_id,))
            self.log(row_id, "undone", {"preserved_preexisting_label": bool(action["preexisting"])})
        return "undone"
