import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from triage.core import Email, Suggestion
from triage.gmail import body_text
from triage.store import Store


class FakeGmail:
    account = "me@example.com"

    def __init__(self, existing=False):
        self.current = {"Label_triage"} if existing else set()
        self.calls = []
        self.fail_after_write = False

    def label_id(self, label):
        return "Label_triage"

    def labels(self, email_id):
        return list(self.current)

    def modify(self, email_id, label_id, *, remove=False):
        self.calls.append((email_id, label_id, remove))
        if remove:
            self.current.discard(label_id)
        else:
            self.current.add(label_id)
        if self.fail_after_write:
            self.fail_after_write = False
            raise ConnectionError("Response lost after Gmail accepted request")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "triage.db"
        self.store = Store(self.path)
        self.email = Email("gmail-1", "sender@example.com", "Subject", "Body")
        self.suggestion = Suggestion("Receipts", "Payment confirmation", False)
        self.store.add("gmail", "me@example.com", self.email, self.suggestion, "ai")
        self.id = self.store.rows()[0]["id"]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_duplicates_and_progress_survive_reopen(self):
        self.store.add("gmail", "me@example.com", self.email, self.suggestion, "ai")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(len(self.store.rows()), 1)
        self.assertTrue(self.store.seen("gmail", "me@example.com", self.email.id))
        self.assertFalse(self.store.seen("gmail", "different@example.com", self.email.id))

    def test_pending_demo_can_upgrade_to_ai_without_overwriting_review(self):
        sample = Email("sample-2", "x", "Unclear", "Body")
        self.store.add("sample", "local-samples", sample, self.suggestion, "demo")
        self.assertFalse(self.store.seen("sample", "local-samples", sample.id, "ai"))
        self.store.add("sample", "local-samples", sample, Suggestion("Other", "AI result", True), "ai")
        row = self.store.rows()[0]
        self.assertEqual(row["mode"], "ai")
        self.store.review(row["id"], "Action needed", approve=True)
        self.store.add("sample", "local-samples", sample, self.suggestion, "ai")
        self.assertEqual(self.store.get(row["id"])["label"], "Action needed")

    def test_apply_requires_review_and_correct_account(self):
        gmail = FakeGmail()
        with self.assertRaisesRegex(ValueError, "Approve"):
            self.store.apply(self.id, gmail)
        self.store.review(self.id, "Receipts", approve=True)
        gmail.account = "different@example.com"
        with self.assertRaisesRegex(ValueError, "account"):
            self.store.apply(self.id, gmail)
        self.assertEqual(gmail.calls, [])

    def test_apply_is_idempotent_and_undo_preserves_other_labels(self):
        gmail = FakeGmail()
        gmail.current.add("INBOX")
        self.store.review(self.id, "Receipts", approve=True)
        self.store.apply(self.id, gmail)
        self.store.apply(self.id, gmail)
        self.assertEqual(len(gmail.calls), 1)
        self.store.undo(self.id, gmail)
        self.assertEqual(gmail.current, {"INBOX"})
        self.assertEqual(self.store.get(self.id)["status"], "undone")

    def test_undo_preserves_a_preexisting_label(self):
        gmail = FakeGmail(existing=True)
        self.store.review(self.id, "Receipts", approve=True)
        self.store.apply(self.id, gmail)
        self.store.undo(self.id, gmail)
        self.assertIn("Label_triage", gmail.current)
        self.assertEqual(gmail.calls, [])

    def test_interrupted_apply_can_resume_without_losing_undo(self):
        gmail = FakeGmail()
        self.store.review(self.id, "Receipts", approve=True)
        gmail.fail_after_write = True
        with self.assertRaises(ConnectionError):
            self.store.apply(self.id, gmail)
        with self.assertRaises(ValueError):
            self.store.review(self.id, "Other", approve=True)
        self.store.close()
        self.store = Store(self.path)
        self.store.apply(self.id, gmail)
        self.store.undo(self.id, gmail)
        self.assertNotIn("Label_triage", gmail.current)

    def test_sample_items_cannot_modify_gmail(self):
        self.store.add("sample", "local-samples", self.email, self.suggestion, "demo")
        row = self.store.rows()[0]
        self.store.review(row["id"], "Receipts", approve=True)
        with self.assertRaises(ValueError):
            self.store.apply(row["id"], FakeGmail())


class ParsingTests(unittest.TestCase):
    def part(self, mime, text, **extra):
        return {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")}, **extra}

    def test_prefers_plain_text_and_skips_attachments(self):
        payload = {"parts": [self.part("text/html", "<b>HTML duplicate</b>"), self.part("text/plain", "Plain content"), self.part("text/plain", "Secret attachment", filename="notes.txt")]}
        self.assertEqual(body_text(payload), "Plain content")

    def test_html_drops_scripts_and_body_is_bounded(self):
        self.assertEqual(body_text(self.part("text/html", "<p>Hello</p><script>evil()</script>")), "Hello")
        self.assertEqual(len(body_text(self.part("text/plain", "x" * 20000))), 12000)

    def test_gmail_paginates_past_seen_messages_and_limits_new_downloads(self):
        from triage.gmail import Gmail

        service = MagicMock()
        service.users().getProfile().execute.return_value = {"emailAddress": "ME@example.com"}
        service.users().messages().list().execute.side_effect = [
            {"messages": [{"id": "seen"}], "nextPageToken": "page-2"},
            {"messages": [{"id": "new"}, {"id": "later"}]},
        ]
        service.users().messages().get().execute.return_value = {"id": "new", "payload": self.part("text/plain", "Hello")}
        gmail = Gmail(service)
        service.users().messages().get.reset_mock()
        emails = list(gmail.fetch("in:inbox", 1, exclude=lambda value: value == "seen"))
        self.assertEqual([email.id for email in emails], ["new"])
        service.users().messages().get.assert_called_once_with(userId="me", id="new", format="full")
        self.assertEqual(gmail.account, "me@example.com")


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_checkpoint_resumes_after_preview_failure(self):
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from triage.workflow import build_workflow

        classify = AsyncMock(return_value=Suggestion("Other", "Ambiguous", True))
        config = {"configurable": {"thread_id": "resume-test"}}
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "checkpoint.db")
            async with AsyncSqliteSaver.from_conn_string(path) as saver:
                saver.serde = JsonPlusSerializer(allowed_msgpack_modules=[("triage.core", "Email"), ("triage.core", "Suggestion")])
                graph = build_workflow(classify, checkpointer=saver)
                with patch("triage.workflow.preview_record", side_effect=RuntimeError("simulated interruption")):
                    with self.assertRaises(RuntimeError):
                        await graph.ainvoke({"email": Email("1", "x", "Hi", "Body")}, config, durability="sync")
            async with AsyncSqliteSaver.from_conn_string(path) as saver:
                saver.serde = JsonPlusSerializer(allowed_msgpack_modules=[("triage.core", "Email"), ("triage.core", "Suggestion")])
                graph = build_workflow(classify, checkpointer=saver)
                state = await graph.aget_state(config)
                self.assertEqual(state.next, ("preview",))
                result = await graph.ainvoke(None, config, durability="sync")
                self.assertEqual(result["preview"]["label"], "Other")
                self.assertEqual(classify.await_count, 1)

    async def test_sdk_returns_structured_output_and_disables_tracing(self):
        from triage.classifier import make_ai_classifier

        captured = {}

        async def fake_run(agent, **kwargs):
            captured.update(kwargs)
            captured["agent"] = agent
            output = agent.output_type(schema_version="1", intent="recruiting", label="Recruiting", reason="Interview request", needs_review=False, risk_flags=[],
                actions=[{"kind":"set_label","label":"Recruiting","reply_instruction":None,"evidence":{"field":"subject","quote":"Interview"}}])

            class Result:
                def final_output_as(self, schema):
                    return output

            return Result()

        with patch("agents.Runner.run", side_effect=fake_run):
            result = await make_ai_classifier("test-model")(Email("1", "x", "Interview", "Can you meet?"))
        self.assertEqual(result.label, "Recruiting")
        self.assertTrue(captured["run_config"].tracing_disabled)
        self.assertEqual(captured["agent"].tools, [])
        self.assertEqual(json.loads(captured["input"])["subject"], "Interview")

    async def test_graph_runs_classifier_and_builds_preview(self):
        from triage.workflow import build_workflow

        classify = AsyncMock(return_value=Suggestion("Other", "Ambiguous", True))
        result = await build_workflow(classify).ainvoke({"email": Email("1", "x", "Hello", "Hi")})
        self.assertFalse(result["preview"]["applied"])
        self.assertEqual(result["preview"]["status"], "review_required")


if __name__ == "__main__":
    unittest.main()
