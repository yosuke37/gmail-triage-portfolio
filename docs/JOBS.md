# Durable jobs and dedicated services

The application uses a PostgreSQL-backed queue, so Redis is not required. The API commits a job before returning HTTP **202**. A dedicated scheduler enqueues due work; independent worker processes claim and execute it. The dashboard polls saved status every two seconds. Closing the browser or restarting the API does not cancel jobs.

## Start on Windows

From the project folder, start PostgreSQL and ensure its schema is current:

```powershell
.\.venv\Scripts\python.exe scripts/local_postgres.py start
.\.venv\Scripts\python.exe -m triage.db_cli upgrade
```

Keep each of these commands running in its own PowerShell terminal:

```powershell
# Terminal 1: application API
.\.venv\Scripts\python.exe -m triage.api

# Terminal 2: background job consumer (required for manual fetch/draft/digest)
.\.venv\Scripts\python.exe -m triage.job_worker

# Terminal 3: scheduled job producer
.\.venv\Scripts\python.exe -m triage.scheduler

# Terminal 4: dashboard
.\.venv\Scripts\python.exe -m streamlit run dashboard.py
```

Open [the dashboard](http://127.0.0.1:8501/). **Background jobs** shows worker/scheduler availability, job IDs, progress, attempts, and errors. An offline service does not discard jobs: queued work waits until a worker returns. No new credentials are needed; all services use the existing local configuration.

**Start scheduled triage** validates the authorized account and enables a durable schedule; it does not launch a subprocess. **Stop scheduled triage** disables the schedule and requests cancellation of its queued/running jobs. Manual jobs are unaffected. Keep both service processes running for scheduled work. Restarting them resumes an enabled schedule. Stopping a worker with Ctrl+C lets its current job finish before exit; stop scheduled triage first if you want to disable future cycles. Stop workers before stopping PostgreSQL.

For controlled diagnostics, `python -m triage.job_worker --once` processes at most one due job and exits. It can incur charges or apply previously authorized automation rules if such a job is queued. Multiple worker processes are supported on PostgreSQL. Ingestion is serialized per database by an advisory lock; additional workers can handle other work while one ingests. Only one scheduler should run; its advisory lock rejects another instance. SQLite remains an offline/developer backend with local locks, not the deployment queue.

## What is queued

| Kind | Work | Attempts |
|---|---|---|
| `ingest` | Fetch/classify a bounded Gmail batch and save review items | Up to 3 for recognized transient errors |
| `draft_reply` | Fetch the original and generate an editable AI reply | Up to 3 for recognized transient errors |
| `daily_digest` | Generate a local digest snapshot | 1; explicit new attempt through digest panel |
| `scheduled_triage` | Classification, opted-in suggested replies, and authorized label rules | Up to 3 for recognized transient errors |

Sending replies, deleting messages, forwarding, arbitrary tools, and shell commands are not queue kinds. Sending retains the existing explicit recipient/body review, exact prepared message ID confirmation, and uncertain-outcome protection. Manual label/read actions remain synchronous. Automatic labels still require the saved opt-in rules and current deterministic policy checks; classification alone cannot authorize them.

## Delivery and recovery

- PostgreSQL `FOR UPDATE SKIP LOCKED` atomically claims due rows without making workers wait on the first claimed job. Bound queries and transactions protect enqueue/claim/state changes.
- Each claim receives a fresh lease token and a 60-second lease. Workers renew every five seconds using a separate connection. State/progress/result writes require the current token, so a recovered job rejects writes from its old worker.
- An expired lease is reclaimed on the next worker claim. Jobs retry after 10 then 20 seconds for recognized transient failures, up to their attempt limit. Terminal jobs remain visible as failed; they are not silently discarded. Quota/configuration/authorization errors do not loop automatically. Daily digests require deliberate recovery through their panel.
- Cancellation is cooperative. Checks occur at job boundaries, ingestion checkpoints, and before scheduled suggestion/label steps. A Gmail/model call already in flight may complete and save work. Cancelling cannot undo an external action already accepted. A worker that loses its lease must stop at its next checkpoint. The heartbeat stops renewing jobs after 30 minutes; provider timeout/process supervision is still needed for a permanently stuck process.
- Delivery is **at least once**, not exactly once. An interruption can repeat a billed AI request. Existing processed-email deduplication, graph checkpoints, digest claims, approval checks, and idempotent label application limit duplicate application effects. They do not guarantee zero repeated provider calls.
- The scheduler stores the next enqueue time. It keeps at most one active classification job for its current generation, skips overlapping cycles, and does not backfill every missed interval. The interval is measured from enqueue time; if a cycle runs longer, the next job can become due as soon as it finishes. After three terminal failed scheduled jobs, the schedule pauses. A stop/start creates a new generation so old jobs cannot resume that schedule.

## API contract

`POST /v1/ingestions`, `/v1/emails/{id}/reply/draft`, and `/v1/digests/generate` return a job receipt with `id`, `kind`, `state`, `attempts`, and timestamps. They no longer stream results or run the agent inside the HTTP request. API clients must poll:

- `GET /v1/jobs` — latest 30 jobs, excluding private payloads/results.
- `GET /v1/jobs/{id}` — saved progress, sanitized error, and result when available.
- `POST /v1/jobs/{id}/cancel` — request cancellation.
- `POST /v1/jobs/{id}/retry` — explicitly create a new job for a failed ingestion/draft/triage job; may use credits. Digest retries use the digest panel's clear/new-attempt flow.
- `GET /v1/services` — service heartbeats; healthy means seen within 45 seconds and not stopped.

All routes require the existing local service bearer token. Queue POSTs accept an `Idempotency-Key` header (1–200 ASCII characters). Reusing the key with the same validated payload returns the existing receipt; a different payload returns 409. The dashboard retains a pending key across reruns after a lost response, so resubmitting the same request recovers the receipt. This is separate from the worker's at-least-once execution. Inspect job history after a lost response, especially after restarting the browser session. There is no automatic send retry.

Request IDs and job IDs flow into audit records. Status/progress exclude source email text and raw provider exception messages. Draft results and input instructions are private application data stored in the queue database. The API returns results only through authenticated detail requests. This is still a **single-owner local application**, not per-user isolation. Compose packaging, a single-owner HTTPS login gateway, mounted deployment secrets, backup automation, health checks, and attempt metrics are now prepared; see [deployment setup](DEPLOYMENT.md). Local container smoke checks have passed. Hosted activation, database encryption, retention/purging, and per-user authentication remain work.

## Verification

```powershell
$env:TRIAGE_TEST_POSTGRES='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

PostgreSQL tests create/drop isolated `triage_test_*` databases. They cover concurrent claims, skipping a locked head row, expired lease recovery and stale-worker fencing, idempotency, retries, cancellation, scheduler deduplication, and persistence. Dashboard/API tests use mocked Gmail/OpenAI calls and drain workers explicitly; no test sends live email. Back up PostgreSQL before schema upgrades (`python -m triage.db_cli backup`). Migration `0003_jobs` is additive; destructive downgrade is intentionally disabled.
