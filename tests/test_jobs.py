from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from triage.automation import Automation, Config
from triage.jobs import Jobs, JobCancelled
from triage.job_worker import Context, process_once
from triage.scheduler import tick
from triage.store import Store
from tests.test_database import PostgresFixture


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "jobs.db"
        self.store = Store(self.path, database_url=getattr(self, "pg_url", None))
        self.timestamp = 1000.0
        self.queue = Jobs(self.store, clock=lambda: self.timestamp)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def enqueue(self, key="one", **kwargs):
        return self.queue.enqueue("ingest", {"query": "in:inbox", "mode": "demo", "limit": 2}, key=key, **kwargs)

    def test_persistence_idempotency_validation_and_owner_boundary(self):
        receipt = self.enqueue()
        self.assertEqual(receipt["id"], self.enqueue()["id"])
        with self.assertRaises(ValueError):
            self.queue.enqueue("ingest", {"query": "different", "mode": "demo", "limit": 2}, key="one")
        for kind in ("send_reply", "delete", "shell"):
            with self.assertRaises(ValueError):
                self.queue.enqueue(kind, {}, key=kind)
        with self.assertRaises(ValueError):
            self.queue.get(receipt["id"], owner="someone-else")
        self.assertEqual(self.queue.list(owner="someone-else"), [])
        other = Store(self.path, database_url=getattr(self, "pg_url", None))
        try:
            self.assertEqual(Jobs(other).get(receipt["id"])["state"], "queued")
        finally:
            other.close()
        self.assertNotIn("payload", receipt)
        self.assertNotIn("lease_token", receipt)

    def test_expired_lease_is_recovered_and_old_worker_is_fenced(self):
        receipt = self.enqueue()
        first = self.queue.claim("first", lease_seconds=10)
        self.timestamp += 11
        self.assertFalse(self.queue.renew(first))
        self.assertFalse(self.queue.finish(first, {"expired": True}))
        second = self.queue.claim("second")
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        self.assertEqual(second["attempts"], 2)
        self.assertFalse(self.queue.finish(first, {"stale": True}))
        self.assertFalse(self.queue.renew(first))
        with self.assertRaises(JobCancelled):
            Context(self.store, first).check()
        self.assertTrue(self.queue.finish(second, {"processed": 1}))
        self.assertEqual(self.queue.get(receipt["id"])["result"], {"processed": 1})

    def test_backoff_attempt_limit_and_cancel(self):
        receipt = self.enqueue()
        for attempt in range(1, 4):
            job = self.queue.claim("worker")
            self.assertEqual(job["attempts"], attempt)
            self.queue.finish(job, error="Network failure", retry=True)
            if attempt < 3:
                self.assertIsNone(self.queue.claim("worker"))
                self.timestamp += 10 * 2 ** (attempt - 1)
        self.assertEqual(self.queue.get(receipt["id"])["state"], "failed")
        queued = self.enqueue("cancel-queued")
        self.assertEqual(self.queue.cancel(queued["id"])["state"], "cancelled")
        running = self.enqueue("cancel-running")
        claimed = self.queue.claim("worker")
        self.queue.cancel(running["id"])
        self.assertFalse(self.queue.alive(claimed))
        self.queue.finish(claimed, {"done": True})
        self.assertEqual(self.queue.get(running["id"])["state"], "cancelled")

    def test_worker_persists_progress_result_and_correlation(self):
        # Use the real DB clock here; fake-clock jobs are also due now.
        receipt = self.enqueue(request_id="origin-request")
        def fetch(store, **kwargs):
            from triage.request_context import request_id, job_id
            self.assertEqual(request_id.get(), "origin-request")
            self.assertEqual(job_id.get(), receipt["id"])
            kwargs["on_progress"]({"stage": "saved", "processed": 1, "limit": 2, "body": "private"})
            return {"processed": 1, "skipped": 0}
        with patch("triage.ingestion.fetch_for_dashboard", side_effect=fetch):
            self.assertTrue(process_once(self.path, database_url=getattr(self, "pg_url", None), heartbeat=False))
        job = self.queue.get(receipt["id"])
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["result"]["processed"], 1)
        self.assertNotIn("body", job["progress"])
        self.assertTrue(any(row["healthy"] for row in self.queue.services()))

    def test_transient_failure_requeues_but_quota_failure_does_not(self):
        receipt = self.enqueue()
        with patch("triage.ingestion.fetch_for_dashboard", side_effect=TimeoutError("private")):
            process_once(self.path, database_url=getattr(self, "pg_url", None), heartbeat=False)
        job = self.queue.get(receipt["id"])
        self.assertEqual(job["state"], "queued")
        self.assertGreater(job["available_at"], 1000)
        self.assertNotIn("private", job["error"])
        self.queue.cancel(receipt["id"])
        receipt = self.enqueue("quota")
        class QuotaError(Exception):
            code = "insufficient_quota"
        with patch("triage.ingestion.fetch_for_dashboard", side_effect=QuotaError()):
            process_once(self.path, database_url=getattr(self, "pg_url", None), heartbeat=False)
        self.assertEqual(self.queue.get(receipt["id"])["state"], "failed")

    def test_scheduler_deduplicates_without_provider_calls_and_stop_cancels(self):
        auto = Automation(self.store)
        auto.save(Config(mode="demo"))
        with self.store.db:
            self.store.db.execute("UPDATE automation SET enabled=1,owner='generation',account='me@example.com',next_run=0")
        with patch("triage.cli.gmail_client") as gmail, patch("agents.Runner.run") as ai:
            self.assertEqual(tick(self.store), 1)
            self.assertEqual(tick(self.store), 0)
            self.assertEqual(len(self.queue.list()), 1)
            gmail.assert_not_called()
            ai.assert_not_called()
        auto.stop()
        self.assertEqual(self.queue.list()[0]["state"], "cancelled")
        self.assertEqual(tick(self.store), 0)

    def test_digest_snapshot_changes_do_not_break_lost_receipt_idempotency(self):
        payload = {"account": "me@example.com", "day": "2026-11-02", "regenerate": True,
                   "expected_digest_id": 1, "expected_digest_created": "old"}
        first = self.queue.enqueue("daily_digest", payload, key="regenerate")
        second = self.queue.enqueue("daily_digest", {**payload, "expected_digest_id": 2, "expected_digest_created": "new"}, key="regenerate")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["max_attempts"], 1)

    def test_attempt_metrics_record_usage_latency_retry_and_crash_recovery(self):
        from triage.usage import capture_usage, report_usage
        receipt = self.enqueue()
        first = self.queue.claim("worker")
        nested = []
        with capture_usage(lambda event: self.queue.usage(first, event)):
            with capture_usage(nested.append):
                report_usage(SimpleNamespace(context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=1, input_tokens=10, output_tokens=5, total_tokens=15))))
        self.timestamp += 4
        self.queue.finish(first, error="Network failure", retry=True)
        self.timestamp += 10
        second = self.queue.claim("worker", lease_seconds=5)
        self.timestamp += 6
        third = self.queue.claim("recovery-worker")
        self.queue.finish(third, {"processed": 1})
        evidence = self.queue.operations()
        self.assertEqual(evidence["jobs"]["succeeded"], 1)
        self.assertEqual(evidence["totals"]["attempts"], 3)
        self.assertEqual(evidence["totals"]["total_tokens"], 15)
        self.assertEqual(evidence["totals"]["expired_leases"], 1)
        self.assertEqual(len(nested), 1)
        self.assertEqual({a["state"] for a in evidence["recent_attempts"]}, {"retry_scheduled", "lease_expired", "succeeded"})
        self.assertEqual(next(a for a in evidence["recent_attempts"] if a["attempt"] == 1)["duration_seconds"], 4)
        self.assertNotIn("payload", json.dumps(evidence))


@unittest.skipUnless(os.getenv("TRIAGE_TEST_POSTGRES") == "1", "Real PostgreSQL queue tests are opt-in")
class PostgresQueueTests(PostgresFixture, QueueTests):
    def setUp(self):
        self.pg_setup()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self.pg_cleanup()

    def test_concurrent_workers_claim_distinct_jobs(self):
        for i in range(8):
            self.enqueue(str(i))
        def claim(i):
            store = Store(self.path, database_url=self.pg_url)
            try:
                return Jobs(store).claim(str(i))["id"]
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            claimed = list(pool.map(claim, range(8)))
        self.assertEqual(len(set(claimed)), 8)

    def test_locked_head_job_does_not_block_another_worker(self):
        first, second = self.enqueue(), self.enqueue("two")
        with self.store.db:
            self.store.db.execute("SELECT id FROM jobs WHERE id=? FOR UPDATE", (first["id"],)).fetchone()
            other = Store(self.path, database_url=self.pg_url)
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(Jobs(other).claim, "other-worker")
                    self.assertEqual(future.result(timeout=5)["id"], second["id"])
            finally:
                other.close()
