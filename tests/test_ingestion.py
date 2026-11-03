from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from triage.core import Email, Suggestion
from triage.store import Store
from triage.ingestion import ingestion_lock
from triage.review import filter_reviews
from tests.ui_harness import api_session


class IngestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_duplicate_skip_and_partial_failure_recovery(self):
        from triage.cli import ingest

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "test.db")
            args = SimpleNamespace(source="gmail", mode="demo", query="in:inbox", limit=5)
            emails = [Email(str(i), "sender@example.com", f"Email {i}", "Body") for i in range(3)]
            events = []
            calls = []

            class Gmail:
                account = "me@example.com"

                def fetch(self, query, limit, exclude):
                    return (email for email in emails if not exclude(email.id))

            async def classify(email):
                calls.append(email.id)
                if email.id == "1" and calls.count("1") == 1:
                    raise RuntimeError("interrupted model call")
                return Suggestion("Other", "Review", True)

            try:
                with patch("triage.settings.classifier", return_value=classify):
                    with self.assertRaises(RuntimeError):
                        await ingest(args, store, Gmail(), quiet=True, on_progress=events.append)
                    self.assertEqual(len(store.rows()), 1)
                    result = await ingest(args, store, Gmail(), quiet=True, on_progress=events.append)
                    self.assertEqual(result["processed"], 2)
                    self.assertEqual(len(store.rows()), 3)
                    self.assertEqual(calls.count("0"), 1)
                    self.assertEqual(events[-1]["stage"], "saved")
                    result = await ingest(args, store, Gmail(), quiet=True)
                    self.assertEqual(result["processed"], 0)
            finally:
                store.close()


class ReviewTests(unittest.TestCase):
    def test_filters_combine_and_attention_sort(self):
        rows = [
            {"id": 1, "source": "gmail", "status": "pending", "label": "Receipts", "needs_review": True, "subject": "Invoice", "sender": "shop@example.com"},
            {"id": 2, "source": "sample", "status": "applied", "label": "Other", "needs_review": False, "subject": "Hello"},
            {"id": 3, "source": "gmail", "status": "pending", "label": "Other", "needs_review": False, "subject": "Hello"},
        ]
        self.assertEqual([r["id"] for r in filter_reviews(rows, statuses=["pending"], source="gmail", label="Receipts", search="SHOP@", flagged=True)], [1])
        self.assertEqual([r["id"] for r in filter_reviews(rows, statuses=["pending", "applied"], order="Needs attention first")], [1, 3, 2])
        self.assertEqual(filter_reviews(rows, statuses=[]), [])

    def test_ingestion_lock_rejects_overlap_then_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            with ingestion_lock(path):
                with self.assertRaisesRegex(ValueError, "Another ingestion"):
                    with ingestion_lock(path):
                        pass
            with ingestion_lock(path):
                pass

    def test_dashboard_fetch_feedback_and_filters_without_network(self):
        from streamlit.testing.v1 import AppTest

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            store = Store(path)
            store.add("sample", "local-samples", Email("1", "sender@example.com", "Test receipt", "Body"), Suggestion("Receipts", "Payment", False), "demo")
            with api_session(path) as queue, patch("triage.ingestion.fetch_for_dashboard", return_value={"processed": 2, "skipped": 0}) as fetch:
                app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "dashboard.py")).run(timeout=20)
                fetch.assert_not_called()
                app.text_input(key="fetch-query").input("is:unread")
                app.number_input(key="fetch-limit").set_value(3)
                next(button for button in app.button if button.label == "Fetch and classify").click().run()
                self.assertEqual(len(app.exception), 0)
                fetch.assert_not_called()
                queue.run_jobs()
                app.run()
                self.assertEqual(fetch.call_args.kwargs["query"], "is:unread")
                self.assertEqual(fetch.call_args.kwargs["limit"], 3)
                self.assertTrue(any("saved 2" in message.value for message in app.success))
                app.text_input(key="review-search").input("nothing matches").run()
                self.assertEqual(len(app.exception), 0)
                self.assertTrue(any("No saved emails match" in message.value for message in app.info))
                fetch.side_effect = RuntimeError("private provider details")
                next(button for button in app.button if button.label == "Fetch and classify").click().run()
                self.assertEqual(len(app.exception), 0)
                queue.run_jobs()
                app.run()
                self.assertTrue(any("Job failed" in message.value for message in app.error))
                self.assertFalse(any("private provider details" in message.value for message in app.error))
            store.close()
