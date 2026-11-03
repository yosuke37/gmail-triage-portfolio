"""A durable PostgreSQL queue with SQLite support for local/offline tests.

Delivery is at least once. Queue kinds never include send, delete, or arbitrary
tool execution. Claims are leased and fenced by a new token on every attempt.
"""
import hashlib
import json
import time
from uuid import uuid4

from pydantic import Field

from triage.api_models import RequestModel, IngestRequest, DraftRequest, DigestRequest
from triage.store import now

OWNER = "local-owner"
TERMINAL = {"succeeded", "failed", "cancelled"}


class DraftJob(DraftRequest):
    row_id: int = Field(gt=0)


class DigestJob(DigestRequest):
    schedule_owner: str | None = None
    expected_digest_id: int | None = None
    expected_digest_created: str | None = None


class ScheduledJob(RequestModel):
    schedule_owner: str = Field(min_length=1)


PAYLOADS = {"ingest": IngestRequest, "draft_reply": DraftJob, "daily_digest": DigestJob, "scheduled_triage": ScheduledJob}
ATTEMPTS = {"ingest": 3, "draft_reply": 3, "daily_digest": 1, "scheduled_triage": 3}


class JobCancelled(Exception):
    pass


class Jobs:
    def __init__(self, store, *, clock=None):
        self.store, self.db, self.clock = store, store.db, clock

    def seconds(self):
        if self.clock:
            return self.clock()
        if self.db.backend == "postgresql":
            return float(self.db.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())").fetchone()[0])
        return time.time()

    def enqueue(self, kind, payload, *, key, owner=OWNER, request_id=None):
        if kind not in PAYLOADS:
            raise ValueError("Unsupported job kind.")
        payload = PAYLOADS[kind].model_validate(payload).model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True)
        # Regeneration snapshots are captured on first enqueue, not part of the
        # caller's logical request identity if a receipt is lost and resubmitted.
        identity = {key: value for key, value in payload.items() if key not in ("expected_digest_id", "expected_digest_created")}
        fingerprint = hashlib.sha256((kind + json.dumps(identity, sort_keys=True)).encode()).hexdigest()
        job_id = uuid4().hex
        with self.db:
            self.db.execute("""INSERT INTO jobs(id,owner_id,kind,payload,payload_hash,idempotency_key,state,max_attempts,available_at,request_id,created)
                VALUES (?,?,?,?,?,?,'queued',?,?,?,?) ON CONFLICT(owner_id,idempotency_key) DO NOTHING""",
                (job_id, owner, kind, encoded, fingerprint, key, ATTEMPTS[kind], self.seconds(), request_id, now()))
            job = self.db.execute("SELECT * FROM jobs WHERE owner_id=? AND idempotency_key=?", (owner, key)).fetchone()
            if job["payload_hash"] != fingerprint:
                raise ValueError("Idempotency key was already used for a different operation.")
        return self.public(job)

    def public(self, row, *, result=True):
        fields = ("id", "kind", "state", "attempts", "max_attempts", "available_at", "worker_id", "cancel_requested", "error", "request_id", "created", "started", "finished")
        output = {key: row[key] for key in fields}
        output["target_row_id"] = json.loads(row["payload"]).get("row_id") if row["kind"] == "draft_reply" else None
        output["progress"] = json.loads(row["progress"]) if row["progress"] else None
        if result:
            output["result"] = json.loads(row["result"]) if row["result"] else None
        return output

    def get(self, job_id, *, owner=OWNER):
        row = self.db.execute("SELECT * FROM jobs WHERE id=? AND owner_id=?", (job_id, owner)).fetchone()
        if not row:
            raise ValueError("Job not found.")
        return self.public(row)

    def list(self, *, owner=OWNER):
        return [self.public(row, result=False) for row in self.db.execute("SELECT * FROM jobs WHERE owner_id=? ORDER BY created DESC,id DESC LIMIT 30", (owner,))]

    def cancel(self, job_id, *, owner=OWNER):
        with self.db:
            self.db.execute("""UPDATE jobs SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,
                finished=CASE WHEN state='queued' THEN ? ELSE finished END WHERE id=? AND owner_id=? AND state IN ('queued','running')""", (now(), job_id, owner))
        return self.get(job_id, owner=owner)

    def recover(self):
        with self.db:
            timestamp = self.seconds()
            self.db.execute("""UPDATE job_attempts SET state='lease_expired',finished_at=?,duration_seconds=?-started_at
                WHERE state='running' AND EXISTS (SELECT 1 FROM jobs WHERE jobs.id=job_attempts.job_id
                AND jobs.attempts=job_attempts.attempt AND jobs.state='running' AND jobs.lease_until<?)""", (timestamp, timestamp, timestamp))
            self.db.execute("""UPDATE jobs SET state=CASE WHEN cancel_requested=1 THEN 'cancelled' WHEN attempts>=max_attempts THEN 'failed' ELSE 'queued' END,
                error='Worker lease expired; work may resume from saved checkpoints.',lease_token=NULL,lease_until=NULL,
                finished=CASE WHEN cancel_requested=1 OR attempts>=max_attempts THEN ? ELSE NULL END
                WHERE state='running' AND lease_until<?""", (now(), self.seconds()))

    def claim(self, worker_id, *, lease_seconds=60):
        self.recover()
        with self.db:
            query = "SELECT * FROM jobs WHERE state='queued' AND cancel_requested=0 AND available_at<=? ORDER BY available_at,created,id LIMIT 1"
            if self.db.backend == "postgresql":
                query += " FOR UPDATE SKIP LOCKED"
            job = self.db.execute(query, (self.seconds(),)).fetchone()
            if not job:
                return None
            token = uuid4().hex
            self.db.execute("UPDATE jobs SET state='running',attempts=attempts+1,lease_token=?,lease_until=?,worker_id=?,started=?,finished=NULL,error=NULL WHERE id=?", (token, self.seconds() + lease_seconds, worker_id, now(), job["id"]))
            self.db.execute("INSERT INTO job_attempts(job_id,attempt,worker_id,state,started_at) VALUES (?,?,?,'running',?)", (job["id"], job["attempts"] + 1, worker_id, self.seconds()))
            return dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())

    def alive(self, job):
        row = self.db.execute("SELECT state,lease_token,cancel_requested,lease_until FROM jobs WHERE id=?", (job["id"],)).fetchone()
        return bool(row and row["state"] == "running" and row["lease_token"] == job["lease_token"] and not row["cancel_requested"] and row["lease_until"] > self.seconds())

    def renew(self, job, *, lease_seconds=60):
        with self.db:
            return bool(self.db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND state='running' AND lease_token=? AND lease_until>?", (self.seconds() + lease_seconds, job["id"], job["lease_token"], self.seconds())).rowcount)

    def progress(self, job, event):
        if not self.alive(job):
            raise JobCancelled()
        # Only operational fields; never put source email text in progress.
        event = {key: value for key, value in event.items() if key in ("stage", "processed", "skipped", "limit")}
        with self.db:
            self.db.execute("UPDATE jobs SET progress=? WHERE id=? AND state='running' AND lease_token=?", (json.dumps(event), job["id"], job["lease_token"]))

    def finish(self, job, result=None, *, error=None, retry=False, cancelled=False):
        state = "cancelled" if cancelled else "queued" if retry and job["attempts"] < job["max_attempts"] else "failed" if error else "succeeded"
        with self.db:
            changed = bool(self.db.execute("""UPDATE jobs SET state=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE ? END,result=?,error=?,available_at=?,finished=CASE WHEN cancel_requested=1 THEN ? ELSE ? END,lease_until=NULL,lease_token=NULL
                WHERE id=? AND state='running' AND lease_token=? AND lease_until>?""", (state, json.dumps(result) if result is not None else None, error,
                self.seconds() + min(300, 10 * 2 ** (job["attempts"] - 1)) if state == "queued" else self.seconds(),
                now(), None if state == "queued" else now(), job["id"], job["lease_token"], self.seconds())).rowcount)
            if changed:
                actual = self.db.execute("SELECT state FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
                timestamp = self.seconds()
                self.db.execute("UPDATE job_attempts SET state=?,finished_at=?,duration_seconds=?-started_at WHERE job_id=? AND attempt=? AND state='running'", ("retry_scheduled" if actual == "queued" else actual, timestamp, timestamp, job["id"], job["attempts"]))
            return changed

    def usage(self, job, event):
        with self.db:
            self.db.execute("""UPDATE job_attempts SET requests=requests+?,input_tokens=input_tokens+?,
                output_tokens=output_tokens+?,total_tokens=total_tokens+? WHERE job_id=? AND attempt=? AND state='running'""",
                tuple(int(event.get(name, 0)) for name in ("requests", "input_tokens", "output_tokens", "total_tokens")) + (job["id"], job["attempts"]))

    def operations(self):
        counts = {row["state"]: row["count"] for row in self.db.execute("SELECT state,COUNT(*) AS count FROM jobs WHERE owner_id=? GROUP BY state", (OWNER,))}
        totals = dict(self.db.execute("""SELECT COUNT(*) AS attempts,COALESCE(AVG(a.duration_seconds),0) AS average_seconds,
            COALESCE(SUM(a.requests),0) AS requests,COALESCE(SUM(a.total_tokens),0) AS total_tokens,
            COALESCE(SUM(CASE WHEN a.state='lease_expired' THEN 1 ELSE 0 END),0) AS expired_leases
            FROM job_attempts a JOIN jobs j ON j.id=a.job_id WHERE j.owner_id=?""", (OWNER,)).fetchone())
        attempts = [dict(row) for row in self.db.execute("""SELECT a.*,j.kind,j.request_id FROM job_attempts a JOIN jobs j ON j.id=a.job_id
            WHERE j.owner_id=? ORDER BY a.started_at DESC LIMIT 50""", (OWNER,))]
        return {"jobs": counts, "totals": totals, "recent_attempts": attempts}

    def heartbeat(self, service_id, kind, state="idle", job_id=None):
        with self.db:
            self.db.execute("""INSERT INTO job_services(id,kind,heartbeat,state,job_id,started) VALUES (?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat,state=excluded.state,job_id=excluded.job_id""", (service_id, kind, self.seconds(), state, job_id, now()))
        from triage.service_health import record
        record(kind, state)

    def services(self):
        return [{**dict(row), "healthy": self.seconds() - row["heartbeat"] < 45 and row["state"] != "stopped"} for row in self.db.execute("SELECT * FROM job_services ORDER BY started DESC LIMIT 20")]
