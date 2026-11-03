from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from triage.automation import Automation, Config, apply_rules, start_worker
from triage.core import Email, Suggestion
from triage.store import Store
from tests.ui_harness import api_session


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.db"
        self.store = Store(self.path)
        self.auto = Automation(self.store)
        self.gmail = MagicMock()
        self.gmail.account = "me@example.com"
        self.gmail.label_id.return_value = "Label_receipts"
        self.gmail.labels.return_value = []
        with self.store.db:
            self.store.db.execute("UPDATE automation SET enabled=1,owner='test',account='me@example.com'")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, label="Receipts", flagged=False, source="gmail", mode="ai"):
        from triage.proposals import baseline_plan, validate_plan

        email = Email(str(len(self.store.rows())), "x", "Subject", "Body")
        suggestion = Suggestion(label, "Evidence", flagged)
        bundle = validate_plan(email, baseline_plan(email, suggestion))
        self.store.add(source, self.gmail.account, email, Suggestion(label, "Evidence", flagged, bundle), mode)
        return self.store.rows()[0]["id"]

    def test_settings_validation_and_disabled_default(self):
        other = Store(Path(self.temp.name) / "other.db")
        try:
            self.assertFalse(Automation(other).state()["enabled"])
            self.assertEqual(Automation(other).config().auto_labels, [])
        finally:
            other.close()
        for config in (Config(interval_minutes=0), Config(batch_size=101), Config(query=""), Config(mode="demo", auto_labels=["Receipts"]), Config(auto_labels=["Invalid"])):
            with self.assertRaises(ValueError):
                self.auto.save(config)

    def test_only_enabled_unflagged_ai_label_is_applied(self):
        allowed = self.add()
        flagged = self.add(flagged=True)
        wrong_label = self.add(label="Other")
        demo = self.add(mode="demo")
        sample = self.add(source="sample")
        self.assertFalse(apply_rules(self.store, self.auto, allowed, self.gmail, "test"))
        self.auto.save(Config(auto_labels=["Receipts"]))
        for row_id in (flagged, wrong_label, demo, sample):
            self.assertFalse(apply_rules(self.store, self.auto, row_id, self.gmail, "test"))
        self.assertTrue(apply_rules(self.store, self.auto, allowed, self.gmail, "test"))
        self.assertEqual(self.store.get(allowed)["status"], "applied")
        self.assertFalse(apply_rules(self.store, self.auto, allowed, self.gmail, "test"))
        self.gmail.modify.assert_called_once()
        self.gmail.send_reply.assert_not_called()
        self.gmail.set_unread.assert_not_called()

    def test_manual_decisions_and_stop_are_preserved(self):
        self.auto.save(Config(auto_labels=["Receipts"]))
        rejected = self.add()
        self.store.review(rejected, "Receipts", approve=False)
        approved = self.add()
        self.store.review(approved, "Receipts", approve=True)
        for row_id in (rejected, approved):
            self.assertFalse(apply_rules(self.store, self.auto, row_id, self.gmail, "test"))
        pending = self.add()
        self.auto.stop()
        self.assertFalse(apply_rules(self.store, self.auto, pending, self.gmail, "test"))
        self.gmail.modify.assert_not_called()

    def test_label_changed_after_automation_approval_is_not_applied(self):
        row_id = self.add()
        self.store.review(row_id, "Other", approve=True)
        with self.assertRaisesRegex(ValueError, "label changed"):
            self.store.apply(row_id, self.gmail, expected_label="Receipts")
        self.gmail.modify.assert_not_called()

    def test_start_enables_schedule_without_launching_process(self):
        with self.store.db:
            self.store.db.execute("UPDATE automation SET enabled=0")
        with patch("triage.cli.gmail_client", return_value=self.gmail), patch("triage.settings.classifier"), patch("subprocess.Popen") as launch:
            start_worker(self.store)
            self.assertIsNone(self.auto.state()["pid"])
            self.assertEqual(self.auto.state()["account"], self.gmail.account)
            self.assertTrue(self.auto.state()["enabled"])
            with self.assertRaises(ValueError):
                start_worker(self.store)
            launch.assert_not_called()

    def test_worker_once_with_empty_queue_calls_no_providers(self):
        from triage.job_worker import process_once
        with patch("triage.cli.gmail_client") as gmail, patch("agents.Runner.run") as ai:
            self.assertFalse(process_once(self.path, heartbeat=False))
            gmail.assert_not_called()
            ai.assert_not_called()

    def test_three_consecutive_failed_jobs_pause_scheduler(self):
        from triage.jobs import Jobs
        from triage.scheduler import tick
        queue = Jobs(self.store)
        for index in range(3):
            queue.enqueue("scheduled_triage", {"schedule_owner": "test"}, key=str(index))
            job = queue.claim("test-worker")
            queue.finish(job, error="Failed")
        tick(self.store)
        self.assertFalse(self.auto.state()["enabled"])
        self.assertEqual(self.auto.state()["state"], "paused after errors")

    def test_panel_does_not_start_until_clicked_and_saves_settings(self):
        from streamlit.testing.v1 import AppTest

        with self.store.db:
            self.store.db.execute("UPDATE automation SET enabled=0")
        with api_session(self.path), patch("triage.automation.start_worker") as start:
            app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "dashboard.py")).run(timeout=20)
            self.assertEqual(len(app.exception), 0)
            start.assert_not_called()
            app.number_input(key="auto-interval").set_value(15)
            app.multiselect(key="auto-labels").select("Receipts")
            next(button for button in app.button if button.label == "Save automation settings").click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(self.auto.config().interval_minutes, 15)
            self.assertEqual(self.auto.config().auto_labels, ["Receipts"])
            start.assert_not_called()
            app.button(key="auto-start").click().run()
            start.assert_called_once()


class CycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_during_classification_saves_result_and_starts_no_next_email(self):
        from triage.worker import run_cycle

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "test.db")
            auto = Automation(store)
            with store.db:
                store.db.execute("UPDATE automation SET enabled=1,owner='test',account='me@example.com'")

            class Gmail:
                account = "me@example.com"

                def fetch(self, query, limit, exclude):
                    return iter([Email("1", "x", "One", "Body"), Email("2", "x", "Two", "Body")])

            calls = []

            async def classify(email):
                calls.append(email.id)
                auto.stop()
                return Suggestion("Receipts", "Receipt", False)

            try:
                with patch("triage.cli.gmail_client", return_value=Gmail()), patch("triage.settings.classifier", return_value=classify):
                    self.assertEqual(await run_cycle(store, "test"), "stopped")
                self.assertEqual(calls, ["1"])
                self.assertEqual(len(store.rows()), 1)
                self.assertEqual(auto.runs()[0]["processed"], 1)
            finally:
                store.close()

    async def test_daily_limit_progress_and_usage_are_persisted(self):
        from triage.worker import run_cycle
        from triage.usage import report_usage

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "test.db")
            auto = Automation(store)
            auto.save(Config(batch_size=5, daily_limit=1))
            with store.db:
                store.db.execute("UPDATE automation SET enabled=1,owner='test',account='me@example.com'")

            class Gmail:
                account = "me@example.com"

                def fetch(self, query, limit, exclude):
                    self.limit = limit
                    return iter([Email("1", "x", "Subject", "Body")])

            gmail = Gmail()

            async def classify(email):
                report_usage(SimpleNamespace(context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=1, input_tokens=10, output_tokens=5, total_tokens=15))))
                return Suggestion("Receipts", "Receipt", False)

            try:
                with patch("triage.cli.gmail_client", return_value=gmail) as connect, patch("triage.settings.classifier", return_value=classify):
                    self.assertEqual(await run_cycle(store, "test"), "completed")
                    self.assertEqual(gmail.limit, 1)
                    self.assertEqual(auto.daily_processed(), 1)
                    self.assertEqual(auto.runs()[0]["requests"], 1)
                    self.assertEqual(auto.runs()[0]["total_tokens"], 15)
                    self.assertEqual(await run_cycle(store, "test"), "capped")
                    connect.assert_called_once()
            finally:
                store.close()

    async def test_account_mismatch_is_recorded_without_provider_details(self):
        from triage.worker import run_cycle

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "test.db")
            auto = Automation(store)
            with store.db:
                store.db.execute("UPDATE automation SET enabled=1,owner='test',account='me@example.com'")
            try:
                with patch("triage.cli.gmail_client", return_value=SimpleNamespace(account="someoneelse@example.com")):
                    self.assertEqual(await run_cycle(store, "test"), "failed")
                self.assertEqual(auto.runs()[0]["state"], "failed")
                self.assertEqual(auto.runs()[0]["processed"], 0)
            finally:
                store.close()
