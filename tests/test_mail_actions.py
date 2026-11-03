import base64
from email import policy
from email.parser import BytesParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from triage.core import Email, Suggestion
from triage.gmail import Gmail
from triage.store import Store
from tests.ui_harness import api_session


class MailAdapterTests(unittest.TestCase):
    def setUp(self):
        self.service = MagicMock()
        self.service.users().getProfile().execute.return_value = {"emailAddress": "me@example.com"}
        self.gmail = Gmail(self.service, writable=True)

    def context(self, **headers):
        self.service.users().messages().get().execute.return_value = {
            "threadId": "thread-1", "payload": {"headers": [
                {"name": key, "value": value} for key, value in {
                    "From": "Sender <sender@example.com>", "Reply-To": "reply@example.com",
                    "Message-ID": "<original@example.com>", "Subject": "Project update", **headers,
                }.items()
            ]},
        }
        return self.gmail.reply_context("original")

    def test_read_unread_changes_only_unread_label(self):
        self.gmail.set_unread("original", False)
        self.service.users().messages().modify.assert_called_with(userId="me", id="original", body={"removeLabelIds": ["UNREAD"]})
        self.gmail.set_unread("original", True)
        self.service.users().messages().modify.assert_called_with(userId="me", id="original", body={"addLabelIds": ["UNREAD"]})
        self.gmail.writable = False
        with self.assertRaises(ValueError):
            self.gmail.set_unread("original", True)

    def test_threaded_reply_uses_reply_to_and_never_cc(self):
        context = self.context()
        self.service.users().messages().send().execute.return_value = {"id": "sent-1"}
        reply = {**context, "body": "Thanks for the update.\nこんにちは", "message_id": "<reply@example.com>"}
        self.assertEqual(self.gmail.send_reply(reply), "sent-1")
        request = self.service.users().messages().send.call_args.kwargs["body"]
        message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(request["raw"]))
        self.assertEqual(message["To"], "reply@example.com")
        self.assertIsNone(message["Cc"])
        self.assertIsNone(message["Bcc"])
        self.assertEqual(message["In-Reply-To"], "<original@example.com>")
        self.assertEqual(message["References"], "<original@example.com>")
        self.assertEqual(message["Subject"], "Project update")
        self.assertEqual(request["threadId"], "thread-1")
        self.assertIn("こんにちは", message.get_content())
        self.service.users().messages().send().execute.assert_called_with(num_retries=0)

    def test_header_injection_and_multiple_recipients_rejected(self):
        with self.assertRaises(ValueError):
            self.context(**{"Reply-To": "x@example.com\r\nBcc: attacker@example.com"})
        with self.assertRaises(ValueError):
            self.context(**{"Reply-To": "x@example.com, y@example.com"})
        with self.assertRaises(ValueError):
            self.context(**{"Message-ID": ""})


class MailStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.db")
        self.gmail = MagicMock()
        self.gmail.account = "me@example.com"
        self.gmail.labels.return_value = ["INBOX", "UNREAD"]
        self.gmail.reply_context.return_value = {
            "recipient": "reply@example.com", "subject": "Question", "thread_id": "thread-1",
            "in_reply_to": "<original@example.com>", "references": "<original@example.com>",
        }
        self.gmail.send_reply.return_value = "sent-1"
        self.store.add("gmail", self.gmail.account, Email("original", "sender@example.com", "Question", "body"), Suggestion("Other", "Review", True), "ai")
        self.row_id = self.store.rows()[0]["id"]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_read_status_audited_and_reversible_via_inverse(self):
        self.store.read_state(self.row_id, self.gmail, unread=False)
        self.gmail.set_unread.assert_called_with("original", False)
        self.store.read_state(self.row_id, self.gmail, unread=True)
        self.gmail.set_unread.assert_called_with("original", True)
        events = [r[0] for r in self.store.db.execute("SELECT event FROM audit")]
        self.assertEqual(events, ["marked_read", "marked_unread"])

    def test_draft_is_local_and_requires_exact_reviewed_version(self):
        draft = self.store.prepare_reply(self.row_id, self.gmail, "Thanks!")
        self.gmail.send_reply.assert_not_called()
        with self.assertRaises(ValueError):
            self.store.send_reply(self.row_id, self.gmail, expected_message_id="stale")
        self.store.send_reply(self.row_id, self.gmail, expected_message_id=draft["payload"]["message_id"])
        self.assertEqual(self.store.reply(self.row_id)["state"], "sent")
        with self.assertRaises(ValueError):
            self.store.send_reply(self.row_id, self.gmail, expected_message_id=draft["payload"]["message_id"])
        self.gmail.send_reply.assert_called_once()

    def test_failed_send_is_not_retried(self):
        draft = self.store.prepare_reply(self.row_id, self.gmail, "Thanks!")
        self.gmail.send_reply.side_effect = ConnectionError("Response lost")
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.store.send_reply(self.row_id, self.gmail, expected_message_id=draft["payload"]["message_id"])
        self.assertEqual(self.store.reply(self.row_id)["state"], "uncertain")
        with self.assertRaises(ValueError):
            self.store.send_reply(self.row_id, self.gmail, expected_message_id=draft["payload"]["message_id"])
        self.gmail.send_reply.assert_called_once()

    def test_account_mismatch_and_empty_reply_do_not_send(self):
        with self.assertRaises(ValueError):
            self.store.prepare_reply(self.row_id, self.gmail, "  ")
        self.gmail.account = "someoneelse@example.com"
        with self.assertRaises(ValueError):
            self.store.prepare_reply(self.row_id, self.gmail, "Thanks")
        with self.assertRaises(ValueError):
            self.store.read_state(self.row_id, self.gmail, unread=True)
        self.gmail.send_reply.assert_not_called()
        self.gmail.set_unread.assert_not_called()

    def test_dashboard_requires_preview_and_confirmation(self):
        from streamlit.testing.v1 import AppTest

        with api_session(Path(self.temp.name) / "test.db"), patch("triage.cli.gmail_client", return_value=self.gmail):
            app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "dashboard.py")).run(timeout=20)
            self.assertEqual(len(app.exception), 0)
            app.button(key=f"read-{self.row_id}").click().run()
            self.gmail.set_unread.assert_called_with("original", False)
            app.text_area(key=f"body-{self.row_id}").input("Thanks for your message.")
            app.button(key=f"prepare-{self.row_id}").click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(app.button(key=f"send-{self.row_id}").disabled)
            self.gmail.send_reply.assert_not_called()
            app.checkbox(key=f"confirm-{self.row_id}").check().run()
            self.assertFalse(app.button(key=f"send-{self.row_id}").disabled)
            app.button(key=f"send-{self.row_id}").click().run()
            self.assertEqual(len(app.exception), 0)
            self.gmail.send_reply.assert_called_once()
            self.assertEqual(self.store.reply(self.row_id)["state"], "sent")
