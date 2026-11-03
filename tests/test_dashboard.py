from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from triage.core import Email, Suggestion
from triage.store import Store
from tests.ui_harness import api_session


class DashboardTests(unittest.TestCase):
    def test_review_changes_are_saved_without_mailbox_access(self):
        from streamlit.testing.v1 import AppTest

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            store = Store(path)
            store.add("sample", "local-samples", Email("1", "sender@example.com", "Example receipt", "body"), Suggestion("Receipts", "Payment confirmed", False), "demo")
            row_id = store.rows()[0]["id"]
            with api_session(path):
                app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "dashboard.py")).run(timeout=20)
                self.assertEqual(len(app.exception), 0)
                app.selectbox(key=f"label-{row_id}").select("Other")
                app.button(key=f"approve-{row_id}").click().run()
                self.assertEqual(len(app.exception), 0)
                self.assertEqual(store.get(row_id)["status"], "approved")
                self.assertEqual(store.get(row_id)["label"], "Other")
                app.button(key=f"reject-{row_id}").click().run()
                self.assertEqual(store.get(row_id)["status"], "rejected")
            store.close()
