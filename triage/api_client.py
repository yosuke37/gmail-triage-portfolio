"""Dashboard HTTP adapter. No database, Gmail, agent, or worker operations."""
import hashlib
import json
from uuid import uuid4
import os
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx

from triage.api_security import api_token


def make_client():
    base = os.getenv("TRIAGE_API_URL", "http://127.0.0.1:8000")
    parsed = urlparse(base)
    # An operator may explicitly trust one private service origin. Never accept
    # arbitrary browser input, redirects, wildcard hosts, or an origin prefix.
    internal = os.getenv("TRIAGE_INTERNAL_API_ORIGIN", "").rstrip("/")
    trusted = base.rstrip("/") == internal and bool(internal)
    if parsed.scheme not in ("http", "https") or (parsed.hostname not in ("127.0.0.1", "localhost", "::1") and not trusted) or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("TRIAGE_API_URL must be a local origin or exactly match the configured TRIAGE_INTERNAL_API_ORIGIN.")
    return httpx.Client(base_url=base, headers={"Authorization": f"Bearer {api_token()}"}, timeout=120, trust_env=False, follow_redirects=False)


class ApiClient:
    def __init__(self, pending_keys=None):
        self.pending_keys = pending_keys if pending_keys is not None else {}
        self.http = make_client()
        self.cache = {}

    def close(self):
        self.http.close()

    def checked(self, response):
        if response.is_error:
            try:
                detail = response.json().get("detail", "Application API request failed.")
            except (ValueError, AttributeError):
                detail = "Application API request failed."
            request_id = response.headers.get("X-Request-ID", "")
            raise ValueError(f"{detail}" + (f" (request {request_id})" if request_id else ""))
        return response

    def call(self, method, path, **kwargs):
        try:
            return self.checked(self.http.request(method, path, **kwargs)).json()
        except httpx.RequestError:
            raise ValueError("Application API unavailable. Start it with: python -m triage.api. A timed-out send must be checked in Gmail Sent before retrying.") from None

    def queue_call(self, path, payload):
        fingerprint = hashlib.sha256((path + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
        key = self.pending_keys.setdefault(fingerprint, uuid4().hex)
        result = self.call("POST", path, json=payload, headers={"Idempotency-Key": key})
        self.pending_keys.pop(fingerprint, None)
        return result

    def summary(self):
        return self.call("GET", "/v1/summary")

    def list_emails(self, **filters):
        if not filters.get("status"):
            filters["status"] = ["__none__"]
        return self.call("GET", "/v1/emails", params=filters)

    def detail(self, row_id):
        if row_id not in self.cache:
            self.cache[row_id] = self.call("GET", f"/v1/emails/{row_id}")
        return self.cache[row_id]

    def proposal(self, row_id):
        return self.detail(row_id)["proposal"]

    def reply(self, row_id):
        return self.detail(row_id)["reply"]

    def suggestion(self, row_id):
        return self.detail(row_id)["suggestion"]

    def verdicts(self, row_id):
        return self.detail(row_id)["decisions"]

    def review(self, row_id, label, *, approve):
        return self.call("POST", f"/v1/emails/{row_id}/review", json={"label": label, "approve": approve})

    def apply(self, row_id):
        return self.call("POST", f"/v1/emails/{row_id}/label")["status"]

    def undo(self, row_id):
        return self.call("POST", f"/v1/emails/{row_id}/label/undo")["status"]

    def read_state(self, row_id, *, unread):
        return self.call("POST", f"/v1/emails/{row_id}/read-state", json={"unread": unread})["status"]

    def original(self, row_id):
        result = self.call("GET", f"/v1/emails/{row_id}/original")
        result["email"] = SimpleNamespace(**result["email"])
        return result

    def draft(self, row_id, instruction, tone):
        return self.queue_call(f"/v1/emails/{row_id}/reply/draft", {"instruction": instruction, "tone": tone})

    def prepare_reply(self, row_id, body):
        return self.call("POST", f"/v1/emails/{row_id}/reply/prepare", json={"body": body})

    def send_reply(self, row_id, *, expected_message_id):
        return self.call("POST", f"/v1/emails/{row_id}/reply/send", json={"expected_message_id": expected_message_id, "confirmed": True})["status"]

    def fetch(self, *, query, limit, mode, on_progress=None):
        return self.queue_call("/v1/ingestions", {"query": query, "limit": limit, "mode": mode})

    def jobs(self):
        return self.call("GET", "/v1/jobs")

    def job(self, job_id):
        return self.call("GET", f"/v1/jobs/{job_id}")

    def services(self):
        return self.call("GET", "/v1/services")

    def operations(self):
        return self.call("GET", "/v1/operations")

    def cancel_job(self, job_id):
        return self.call("POST", f"/v1/jobs/{job_id}/cancel")

    def retry_job(self, job_id):
        return self.queue_call(f"/v1/jobs/{job_id}/retry", {})

    def automation_info(self):
        return self.call("GET", "/v1/automation")

    def save_automation(self, config):
        return self.call("PUT", "/v1/automation", json=config)

    def start_automation(self):
        return self.call("POST", "/v1/automation/start")

    def stop_automation(self):
        return self.call("POST", "/v1/automation/stop")

    def accounts(self):
        return self.call("GET", "/v1/accounts")

    def digests(self, account):
        return self.call("GET", "/v1/digests", params={"account": account})

    def generate_digest(self, account, day, *, regenerate=False):
        return self.queue_call("/v1/digests/generate", {"account": account, "day": day, "regenerate": regenerate})

    def clear_digest(self, digest_id):
        return self.call("POST", f"/v1/digests/{digest_id}/clear-attempt")["cleared"]

    def audit(self):
        return self.call("GET", "/v1/audit")

    def evaluations(self):
        return self.call("GET", "/v1/evaluations")
