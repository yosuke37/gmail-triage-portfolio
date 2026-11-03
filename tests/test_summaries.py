from datetime import datetime
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from triage.automation import Automation, Config
from triage.core import Email, Suggestion
from triage.store import Store
from tests.ui_harness import api_session
from triage.summaries import Summaries, TZ, digest_source, generate_digest, scheduled_digest, suggest_reply, write_digest


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.db")
        self.auto = Automation(self.store)
        self.data = Summaries(self.store)
        with self.store.db:
            self.store.db.execute("UPDATE automation SET enabled=1,owner='test',account='me@example.com'")
        self.store.add("gmail", "me@example.com", Email("1", "sender@example.com", "Interview", "Body"), Suggestion("Recruiting", "Interview request", False), "ai")
        self.row_id = self.store.rows()[0]["id"]
        from triage.proposals import Action, Evidence, baseline_plan, validate_plan

        email = Email("1", "sender@example.com", "Interview", "Body")
        plan = baseline_plan(email, Suggestion("Recruiting", "Interview request", False))
        plan.actions.append(Action(kind="draft_reply", label=None, reply_instruction="Acknowledge the invitation.", evidence=Evidence(field="subject", quote="Interview")))
        with self.store.db:
            self.store.db.execute("INSERT INTO proposals(email_row,payload,created) VALUES (?,?,?)", (self.row_id, json.dumps(validate_plan(email, plan)), "2026-11-02T00:00:00+00:00"))
        self.client = MagicMock()
        self.client.account = "me@example.com"
        self.client.original.return_value = {"email": Email("1", "sender@example.com", "Interview", "Body")}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_suggestion_is_opt_in_idempotent_and_never_prepares_or_sends(self):
        with patch("triage.drafting.draft_reply", new=AsyncMock(return_value=SimpleNamespace(body="Thank you. [Availability]", review_notes="Fill placeholder."))) as draft, patch.dict(os.environ, {"OPENAI_MODEL": "test"}):
            self.assertFalse(await suggest_reply(self.store, self.auto, self.row_id, self.client, "test"))
            self.auto.save(Config(suggested_reply_labels=["Recruiting"]))
            self.assertTrue(await suggest_reply(self.store, self.auto, self.row_id, self.client, "test"))
            self.assertFalse(await suggest_reply(self.store, self.auto, self.row_id, self.client, "test"))
            draft.assert_awaited_once()
        self.assertEqual(self.data.suggestion(self.row_id)["state"], "ready")
        self.assertIsNone(self.store.reply(self.row_id))
        self.client.send_reply.assert_not_called()

    async def test_flagged_rejected_and_stopped_do_not_get_suggestions(self):
        self.auto.save(Config(suggested_reply_labels=["Recruiting"]))
        with patch("triage.drafting.draft_reply", new=AsyncMock()) as draft:
            with self.store.db:
                self.store.db.execute("UPDATE emails SET needs_review=1")
            self.assertFalse(await suggest_reply(self.store, self.auto, self.row_id, self.client, "test"))
            with self.store.db:
                self.store.db.execute("UPDATE emails SET needs_review=0,status='rejected'")
            self.assertFalse(await suggest_reply(self.store, self.auto, self.row_id, self.client, "test"))
            self.auto.stop()
            self.assertFalse(await suggest_reply(self.store, self.auto, self.row_id, self.client, "test"))
            draft.assert_not_called()

    async def test_digest_due_time_and_account_date_deduplication(self):
        self.auto.save(Config(digest_enabled=True, digest_hour=18))
        with self.store.db:
            self.store.db.execute("UPDATE emails SET created='2026-11-02T15:00:00+00:00'")
        result = {"overview": "One recruiting message.", "highlights": [{"row_id": self.row_id, "summary": "Interview request", "suggested_next_step": "Check availability"}]}
        with patch.dict(os.environ, {"OPENAI_MODEL": "test", "OPENAI_API_KEY": "test"}), patch("triage.summaries.write_digest", new=AsyncMock(return_value=result)) as draft:
            self.assertFalse(await scheduled_digest(self.store, self.auto, "test", datetime(2026, 11, 2, 17, 59, tzinfo=TZ)))
            self.assertTrue(await scheduled_digest(self.store, self.auto, "test", datetime(2026, 11, 2, 18, 0, tzinfo=TZ)))
            self.assertFalse(await scheduled_digest(self.store, self.auto, "test", datetime(2026, 11, 2, 19, 0, tzinfo=TZ)))
            draft.assert_awaited_once()
        saved = self.data.digests("me@example.com")[0]
        self.assertEqual(saved["state"], "ready")
        self.assertEqual(json.loads(saved["content"])["stats"]["ingested_count"], 1)

    async def test_digest_source_excludes_other_accounts_and_samples_at_dst_boundary(self):
        with self.store.db:
            self.store.db.execute("UPDATE emails SET created='2026-11-02T04:30:00+00:00'")  # Nov 1, 11:30pm EST
        self.store.add("gmail", "other@example.com", Email("2", "other", "Other account", "private"), Suggestion("Other", "x", False), "ai")
        self.store.add("sample", "me@example.com", Email("3", "sample", "Sample", "x"), Suggestion("Other", "x", False), "demo")
        source = digest_source(self.store, "me@example.com", "2026-11-01")
        self.assertEqual(source["ingested_count"], 1)
        self.assertEqual(len(source["items"]), 1)
        self.assertNotIn("body", source["items"][0])
        self.assertEqual(digest_source(self.store, "me@example.com", "2026-11-02")["ingested_count"], 0)

    async def test_unknown_ai_message_reference_is_rejected(self):
        async def fake_run(agent, **kwargs):
            output = agent.output_type(overview="Summary", highlights=[{"row_id": 999, "summary": "x", "suggested_next_step": "y"}])
            return SimpleNamespace(final_output_as=lambda schema: output)

        with patch("agents.Runner.run", side_effect=fake_run):
            with self.assertRaisesRegex(ValueError, "unknown message"):
                await write_digest({"items": [{"row_id": 1}]}, "test")

    async def test_failed_digest_is_saved_without_automatic_paid_retry(self):
        with self.store.db:
            self.store.db.execute("UPDATE emails SET created='2026-11-02T15:00:00+00:00'")
        with patch.dict(os.environ, {"OPENAI_MODEL": "test", "OPENAI_API_KEY": "test"}), patch("triage.summaries.write_digest", new=AsyncMock(side_effect=RuntimeError("private provider response"))) as generate:
            with self.assertRaises(RuntimeError):
                await generate_digest(self.store, "me@example.com", "2026-11-02")
            self.assertFalse(await generate_digest(self.store, "me@example.com", "2026-11-02"))
            generate.assert_awaited_once()
        self.assertEqual(self.data.digests("me@example.com")[0]["state"], "failed")
        self.assertNotIn("private provider", self.data.digests("me@example.com")[0]["error"])


class SuggestedReplyUiTests(unittest.TestCase):
    def test_scheduler_checks_digest_even_when_classification_is_capped(self):
        from triage.scheduler import tick
        from triage.jobs import Jobs

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            store = Store(path)
            auto = Automation(store)
            auto.save(Config(digest_enabled=True, digest_hour=0, digest_minute=0))
            with store.db:
                store.db.execute("UPDATE automation SET enabled=1,owner='test',account='me@example.com'")
            with patch.object(Automation, "daily_processed", return_value=1000), patch("triage.cli.gmail_client") as gmail, patch("agents.Runner.run") as generate:
                tick(store)
                tick(store)
                self.assertEqual([job["kind"] for job in Jobs(store).list()].count("daily_digest"), 1)
                generate.assert_not_called()
                gmail.assert_not_called()
            store.close()

    def test_use_suggestion_does_not_send_or_prepare(self):
        from streamlit.testing.v1 import AppTest

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            store = Store(path)
            store.add("gmail", "me@example.com", Email("1", "sender@example.com", "Interview", "Body"), Suggestion("Recruiting", "Request", False), "ai")
            row_id = store.rows()[0]["id"]
            data = Summaries(store)
            data.claim_suggestion(row_id)
            with store.db:
                store.db.execute("UPDATE reply_suggestions SET state='ready',body='Thank you for reaching out.',notes='Review first.'")
            with api_session(path), patch("triage.cli.gmail_client") as client:
                app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "dashboard.py")).run(timeout=20)
                self.assertEqual(len(app.exception), 0)
                app.button(key=f"use-suggestion-{row_id}").click().run()
                self.assertEqual(app.text_area(key=f"body-{row_id}").value, "Thank you for reaching out.")
                self.assertEqual(len(app.exception), 0)
                self.assertIsNone(store.reply(row_id))
                client.assert_not_called()
            store.close()
