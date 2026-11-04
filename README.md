# Gmail Triage Assistant

A Python portfolio project for explainable email labeling using the OpenAI Agents SDK and LangGraph.

## Current capabilities

**Attachment-aware review** includes bounded PDF, DOCX, `.txt`, PNG and JPEG extraction with local English OCR in classification and reply drafting, with stored previews, typed source citations, extraction statuses, and mandatory human review for attachment-bearing messages. An offline synthetic demo is included. See [attachment setup, limits, and verification](docs/ATTACHMENTS.md).

**Docker and private hosting packaging** now include Compose services, a login-protected HTTPS gateway, mounted secret files, health/readiness checks, restart policies, scheduled backup/restore tooling, and a prepared Render Blueprint. Per-attempt durations, token usage, failures, and lease recovery appear in Background jobs. See [deployment setup and remaining cloud activation steps](docs/DEPLOYMENT.md). Docker build and local HTTPS/login, backup restore, and job durability smoke tests have passed. Cloud resources have not been provisioned.

**Durable jobs and dedicated workers** now separate slow AI/Gmail processing from HTTP requests. PostgreSQL stores queued work, leases, retry history, and progress. The scheduler enqueues due jobs; workers execute them independently. The dashboard monitors jobs and service health. See [service startup and job recovery](docs/JOBS.md).

**PostgreSQL and Alembic migrations** now back the running app. SQLite remains available for lightweight development. The database boundary includes pooled connections, row/advisory locks, version checks, a backup-first SQLite importer, and PostgreSQL graph checkpoints. See [database setup, migration, backup, and recovery instructions](docs/DATABASE.md).

The **FastAPI application service** now separates the Streamlit UI from database, Gmail, agent, and worker operations. It provides authenticated local endpoints, typed request/response contracts, server-side review pagination, durable job status, and request IDs correlated with audit events. See [the architecture guide](docs/ARCHITECTURE.md) for the API boundary and remaining production milestones.

The Agents SDK now produces a **typed intent/action proposal**, followed by LangGraph evidence and deterministic policy validation. A 30-case synthetic evaluation suite measures classification, review flags, action sets, grounding, policy safety, latency, and token usage against a keyword baseline. See [the evaluation guide](docs/EVALUATION.md) and the dashboard's **Agent evaluation results** panel for reproducible artifacts and failure inspection.

Scheduled triage can optionally generate **suggested replies** for selected categories, using your instruction and tone. Suggestions stay local until explicitly loaded, edited, reviewed, and sent. **Daily digests** appear in the dashboard at a configurable America/New_York time, summarize saved message metadata and potential outstanding requests, and support manual generation and JSON download. Both features default to off and use additional API credits when enabled.

**Scheduled triage and automation** uses dedicated scheduler and worker processes, with schedule controls in the dashboard. Save a schedule, search, batch size, and daily message limit; optionally enable automatic labels for unflagged AI suggestions. Start/stop controls, run/error history, token counts, account checks, and preserved manual decisions are included. It starts stopped, never sends replies automatically, and does not install an OS startup task.

The dashboard can fetch and classify new Gmail messages with a configurable search and batch size, show progress/errors, and apply or undo approved labels. The local review list supports search, combined filters, attention-first sorting, and pagination. CLI and dashboard ingestion share checkpoints, duplicate protection, and a process lock.

Original Gmail messages can be loaded as bounded, plain-text previews with read status and an Open in Gmail link. An Agents SDK drafting assistant uses your instruction and chosen tone to generate an editable reply. Generating a draft uses API credits; sending remains a separate review-and-confirm action.

The application reads synthetic emails or Gmail, runs an OpenAI Agents SDK classifier inside a LangGraph workflow, and saves suggestions for human review. Demo mode uses keyword rules without API calls. A local Streamlit dashboard lets you approve, correct, or reject labels, mark Gmail messages read/unread, and compose replies for explicit review and sending. Separate commands apply approved custom Gmail labels and undo them with an audit trail. SQLite checkpoints let interrupted graph runs resume; processed messages are deduplicated. Polling periodically ingests new suggestions without applying them automatically.

**Live Gmail and OpenAI usage requires your own credentials.** See [the setup guide](docs/SETUP.md) for steps from a new account to the first live batch. All automated tests use synthetic data and fake network calls.

## Run on Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[deploy]"
.\.venv\Scripts\python.exe -m triage
.\.venv\Scripts\python.exe -m triage.db_cli upgrade
.\.venv\Scripts\python.exe -m triage.api
```

Keep the API terminal running. Start a worker and scheduler in separate terminals (see [the startup guide](docs/JOBS.md)), then the dashboard:

```powershell
.\.venv\Scripts\python.exe -m streamlit run dashboard.py
```

No activation or external credentials are needed for demo mode. The API automatically generates its local service token; the dashboard reads it automatically. Visit `http://127.0.0.1:8501` for the dashboard and `http://127.0.0.1:8000/docs` for endpoint documentation. Open this folder in VS Code and select `.venv\Scripts\python.exe` as the Python interpreter to use the same environment.

For AI mode, set your own API key and a model available to your OpenAI API project:

```powershell
$env:OPENAI_API_KEY = "your-api-key"
$env:OPENAI_MODEL = "your-model-id"
.\.venv\Scripts\python.exe -m triage --mode ai
```

Alternatively, copy `.env.example` to `.env` and fill in the values locally; `.env` is loaded automatically. Use an API key from your OpenAI API project. Do not paste credentials into source files or commit them. Framework tracing is disabled. AI mode sends email content to OpenAI and can incur API usage charges.

You can supply a JSON array with `id`, `sender`, `subject`, and `body` fields:

```powershell
.\.venv\Scripts\python.exe -m triage --input .\data\sample_emails.json
```

## Design

`Streamlit -> authenticated FastAPI -> PostgreSQL jobs <- scheduler; workers -> domain services -> Gmail + OpenAI`

- `triage/core.py`: domain types and offline baseline.
- `triage/classifier.py`: Agents SDK classifier with a structured label allowlist.
- `triage/workflow.py`: graph orchestration.
- `triage/cli.py`: command-line entry point, ingestion, polling, and checkpoint recovery.
- `triage/gmail.py`: separate read-only/write OAuth tokens and Gmail adapter.
- `triage/store.py`: persistent reviews, duplicate protection, and recoverable label actions.
- `triage/api.py`, `triage/api_models.py`: local application API and typed contracts.
- `triage/api_client.py`: dashboard HTTP adapter, with no execution/database dependencies.
- `triage/jobs.py`, `triage/job_worker.py`, `triage/scheduler.py`: durable queue, leased consumers, and schedule producer.
- `dashboard.py`: local review interface and audit history.
- `triage/evaluation.py`: labeled synthetic evaluation with per-case and aggregate results.

The classifier has no mailbox-modification tools. Email text is untrusted content. Every classification preview has `applied: false`; review decisions and actual actions are tracked separately in SQLite. Review flags are model judgments, not calibrated confidence scores. Only approved items belonging to the authorized Gmail account can be applied. Undo preserves preexisting labels. See the setup guide for storage/privacy details, concurrency limits, and recovery behavior.

## Evaluate and test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m triage evaluate
.\.venv\Scripts\python.exe -m triage evaluate --mode ai
```

The ten-email synthetic development set includes semantic cases that the keyword baseline misses, plus a prompt injection example. It is not a production benchmark. Tests cover approval, account checks, duplicate protection, apply/undo recovery, MIME parsing, SDK output handling, graph checkpoint recovery, and dashboard review interactions.

## Remaining production work

Live verification with your credentials, broader evaluation data, encrypted storage/retention controls, deployment authentication, hosted scheduling and monitoring, and Google's requirements for distributing an app to other users. The current dashboard is for local use on your computer.

## References

- [OpenAI Agents SDK](https://developers.openai.com/api/docs/guides/agents/sdk)
- [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview)
- [Gmail labels](https://developers.google.com/workspace/gmail/api/guides/labels)
