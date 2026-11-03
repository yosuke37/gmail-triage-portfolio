import ast
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from triage.api import create_app
from triage.automation import Config
from triage.core import Email, Suggestion
from triage.store import Store


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.db"
        self.store = Store(self.path, database_url=getattr(self, "pg_url", None))
        self.store.add("gmail", "me@example.com", Email("gmail-1", "sender@example.com", "Receipt", "Payment"), Suggestion("Receipts", "Confirmation", False), "ai")
        self.row_id = self.store.rows()[0]["id"]
        self.app = create_app(database_path=self.path, database_url=getattr(self, "pg_url", None), token="test-service-key")
        self.client = TestClient(self.app, base_url="http://127.0.0.1", headers={"Authorization":"Bearer test-service-key"}, raise_server_exceptions=False)
        self.gmail = MagicMock()
        self.gmail.account = "me@example.com"
        self.gmail.label_id.return_value = "Label_receipts"
        self.gmail.labels.return_value = []
        self.gmail.reply_context.return_value = {"recipient":"sender@example.com", "subject":"Receipt", "thread_id":"1", "in_reply_to":"<original@example.com>", "references":"<original@example.com>"}
        self.gmail.send_reply.return_value = "sent-1"

    def tearDown(self):
        self.client.close()
        self.store.close()
        self.temp.cleanup()

    def test_authentication_and_host_check(self):
        self.assertEqual(self.client.get("/health", headers={"Authorization":""}).status_code, 200)
        for value in ("", "Bearer wrong-key"):
            response = self.client.get("/v1/summary", headers={"Authorization":value})
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("test-service-key", response.text)
        self.assertEqual(self.client.get("/v1/summary", headers={"Host":"attacker.example"}).status_code, 400)

    def test_validation_does_not_echo_private_input_or_call_gmail(self):
        with patch("triage.cli.gmail_client") as connect:
            response = self.client.post(f"/v1/emails/{self.row_id}/reply/draft", json={"instruction":"private-text", "tone":"unknown", "account":"attacker@example.com"})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("private-text", response.text)
            self.assertNotIn("attacker@example.com", response.text)
            connect.assert_not_called()

    def test_unknown_and_sample_items_do_not_contact_gmail(self):
        self.store.add("sample", "local-samples", Email("sample", "x", "Sample", "Body"), Suggestion("Other", "Demo", False), "demo")
        sample = self.store.rows()[0]["id"]
        with patch("triage.cli.gmail_client") as connect:
            self.assertEqual(self.client.post("/v1/emails/999/label").status_code, 404)
            self.assertEqual(self.client.get(f"/v1/emails/{sample}/original").status_code, 409)
            connect.assert_not_called()

    def test_pagination_filters_and_literal_search(self):
        for i in range(12):
            self.store.add("sample", "local-samples", Email(str(i), "x", "100% paid" if i == 0 else f"Subject {i}", "Body"), Suggestion("Other", "Demo", False), "demo")
        response = self.client.get("/v1/emails", params={"source":"sample", "page_size":5,"page":2}).json()
        self.assertEqual(response["total"], 12)
        self.assertEqual(len(response["items"]), 5)
        self.assertEqual(response["pages"], 3)
        response = self.client.get("/v1/emails", params={"search":"%"}).json()
        self.assertEqual(response["total"], 1)
        self.assertEqual(self.client.get("/v1/emails", params={"page_size":101}).status_code, 422)

    def test_approval_apply_undo_and_correlated_audit(self):
        with patch("triage.cli.gmail_client", return_value=self.gmail):
            self.assertEqual(self.client.post(f"/v1/emails/{self.row_id}/label").status_code, 409)
            self.gmail.modify.assert_not_called()
            reviewed = self.client.post(f"/v1/emails/{self.row_id}/review", json={"label":"Receipts","approve":True})
            self.assertEqual(reviewed.status_code, 200)
            self.assertIn("X-Request-ID", reviewed.headers)
            self.assertEqual(self.client.post(f"/v1/emails/{self.row_id}/label").status_code, 200)
            self.assertEqual(self.client.post(f"/v1/emails/{self.row_id}/label").status_code, 200)
            self.gmail.modify.assert_called_once()
            self.assertEqual(self.client.post(f"/v1/emails/{self.row_id}/label/undo").status_code, 200)
        audit = self.client.get("/v1/audit").json()
        event = next(item for item in audit if item["event"] == "approved")
        self.assertEqual(json.loads(event["details"])["request_id"], reviewed.headers["X-Request-ID"])

    def test_reply_send_requires_confirmation_and_exact_prepared_version(self):
        with patch("triage.cli.gmail_client", return_value=self.gmail):
            prepared = self.client.post(f"/v1/emails/{self.row_id}/reply/prepare", json={"body":"Thank you."})
            self.assertEqual(prepared.status_code, 200)
            message_id = prepared.json()["payload"]["message_id"]
            path = f"/v1/emails/{self.row_id}/reply/send"
            self.assertEqual(self.client.post(path, json={"expected_message_id":message_id}).status_code, 422)
            for confirmation in (False, 1, "true"):
                self.assertEqual(self.client.post(path, json={"expected_message_id":message_id,"confirmed":confirmation}).status_code, 422)
            self.assertEqual(self.client.post(path, json={"expected_message_id":"stale","confirmed":True}).status_code, 409)
            self.gmail.send_reply.assert_not_called()
            self.assertEqual(self.client.post(path, json={"expected_message_id":message_id,"confirmed":True}).status_code, 200)
            self.assertEqual(self.client.post(path, json={"expected_message_id":message_id,"confirmed":True}).status_code, 409)
            self.gmail.send_reply.assert_called_once()

    def test_gmail_account_mismatch_prevents_read_changes(self):
        self.gmail.account = "different@example.com"
        with patch("triage.cli.gmail_client", return_value=self.gmail):
            self.assertEqual(self.client.post(f"/v1/emails/{self.row_id}/read-state", json={"unread":False}).status_code, 409)
            self.gmail.set_unread.assert_not_called()

    def test_queued_partial_failure_retains_completed_work_and_hides_provider_text(self):
        def fail(store, **kwargs):
            store.add("gmail", "me@example.com", Email("new", "x", "New", "Body"), Suggestion("Other", "Review", True), "ai")
            kwargs["on_progress"]({"stage":"saved", "processed":1, "limit":2})
            raise RuntimeError("private provider response")

        with patch("triage.ingestion.fetch_for_dashboard", side_effect=fail) as fetch:
            response = self.client.post("/v1/ingestions", json={"query":"in:inbox","limit":2,"mode":"ai"})
            self.assertEqual(response.status_code, 202)
            fetch.assert_not_called()
            from triage.job_worker import process_once
            process_once(self.path, database_url=getattr(self, "pg_url", None), heartbeat=False)
            job = self.client.get("/v1/jobs/" + response.json()["id"]).json()
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["progress"]["processed"], 1)
        self.assertNotIn("private provider response", json.dumps(job))
        self.assertEqual(self.client.get("/v1/summary").json()["total"], 2)

    def test_worker_does_not_start_on_read_and_settings_are_validated(self):
        with patch("triage.automation.start_worker") as start:
            self.assertEqual(self.client.get("/v1/automation").status_code, 200)
            start.assert_not_called()
            invalid = asdict(Config(mode="demo", auto_labels=["Receipts"]))
            self.assertEqual(self.client.put("/v1/automation", json=invalid).status_code, 409)
            self.assertEqual(self.client.post("/v1/automation/start").status_code, 200)
            start.assert_called_once()

    def test_provider_errors_are_sanitized(self):
        with patch("triage.cli.gmail_client", side_effect=RuntimeError("secret provider content")):
            response = self.client.get(f"/v1/emails/{self.row_id}/original")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("secret provider content", response.text)
        self.assertIn("request_id", response.json())
        self.assertEqual(response.headers["X-Request-ID"], response.json()["request_id"])

    def test_job_receipts_are_durable_idempotent_and_private(self):
        payload = {"query": "in:inbox", "limit": 2, "mode": "demo"}
        with patch("triage.ingestion.fetch_for_dashboard") as execute:
            first = self.client.post("/v1/ingestions", json=payload, headers={"Idempotency-Key": "same-request"})
            again = self.client.post("/v1/ingestions", json=payload, headers={"Idempotency-Key": "same-request"})
            self.assertEqual(first.status_code, 202)
            self.assertEqual(first.json()["id"], again.json()["id"])
            self.assertEqual(self.client.post("/v1/ingestions", json={**payload, "limit": 3}, headers={"Idempotency-Key": "same-request"}).status_code, 409)
            execute.assert_not_called()
        job_id = first.json()["id"]
        listed = self.client.get("/v1/jobs").json()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("payload", listed[0])
        self.assertNotIn("lease_token", listed[0])
        self.assertEqual(self.client.get("/v1/jobs/" + job_id).json()["state"], "queued")
        self.assertEqual(self.client.get("/v1/jobs/missing").status_code, 404)
        self.assertEqual(self.client.post(f"/v1/jobs/{job_id}/cancel").json()["state"], "cancelled")
        self.assertEqual(self.client.post(f"/v1/jobs/{job_id}/retry").status_code, 409)

    def test_draft_enqueue_does_not_fetch_original_or_invoke_model(self):
        with patch("triage.cli.gmail_client") as gmail, patch("triage.drafting.generate_draft") as draft:
            response = self.client.post(f"/v1/emails/{self.row_id}/reply/draft", json={"instruction": "Decline politely", "tone": "Professional"})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["target_row_id"], self.row_id)
            gmail.assert_not_called()
            draft.assert_not_called()

    def test_attachment_snapshots_are_authenticated_details_not_list_payloads(self):
        email = Email("attachment-message", "vendor@example.com", "Document", "See attached.", (
            {"id": "attachment-1", "filename": "invoice.txt", "mime_type": "text/plain", "status": "extracted", "text": "Private attachment total USD 125.00", "truncated": False},))
        self.store.add("gmail", "me@example.com", email, Suggestion("Action needed", "Review document", True), "ai")
        row_id = self.store.rows()[0]["id"]
        response = self.client.get(f"/v1/emails/{row_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["attachments"][0]["text"], "Private attachment total USD 125.00")
        denied = self.client.get(f"/v1/emails/{row_id}", headers={"Authorization": ""})
        self.assertEqual(denied.status_code, 401)
        self.assertNotIn("Private attachment", denied.text)
        self.assertNotIn("Private attachment", self.client.get("/v1/emails").text)


class ApiBoundaryTests(unittest.TestCase):
    def test_lost_queue_receipt_retains_idempotency_key_for_resubmission(self):
        import httpx
        from triage.api_client import ApiClient
        pending = {}
        http = MagicMock()
        with patch("triage.api_client.make_client", return_value=http):
            http.request.side_effect = httpx.ReadTimeout("Connection lost")
            first = ApiClient(pending)
            with self.assertRaises(ValueError):
                first.fetch(query="in:inbox", limit=2, mode="demo")
            key = http.request.call_args.kwargs["headers"]["Idempotency-Key"]
            self.assertEqual(len(pending), 1)
            http.request.side_effect = None
            http.request.return_value = httpx.Response(202, json={"id": "existing-job"})
            second = ApiClient(pending)
            self.assertEqual(second.fetch(query="in:inbox", limit=2, mode="demo")["id"], "existing-job")
            self.assertEqual(http.request.call_args.kwargs["headers"]["Idempotency-Key"], key)
            self.assertEqual(pending, {})

    def test_dashboard_modules_do_not_import_database_or_execution_services(self):
        root = Path(__file__).resolve().parent.parent
        forbidden = {"triage.store", "triage.cli", "triage.automation", "triage.summaries", "triage.ingestion", "triage.proposals"}
        for filename in ("dashboard.py", "triage/automation_ui.py", "triage/digest_ui.py", "triage/evaluation_ui.py", "triage/api_client.py"):
            tree = ast.parse((root / filename).read_text(encoding="utf-8"))
            imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
            self.assertFalse(imports & forbidden, filename)

    def test_client_will_not_send_local_service_key_to_remote_url(self):
        from triage.api_client import make_client

        with patch.dict("os.environ", {"TRIAGE_API_URL":"https://attacker.example"}), patch("triage.api_client.api_token") as token:
            with self.assertRaises(ValueError):
                make_client()
            token.assert_not_called()
