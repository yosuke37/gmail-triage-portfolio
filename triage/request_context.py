"""Transport-independent correlation context for the audit trail."""
from contextvars import ContextVar

request_id = ContextVar("triage_request_id", default=None)
job_id = ContextVar("triage_job_id", default=None)
