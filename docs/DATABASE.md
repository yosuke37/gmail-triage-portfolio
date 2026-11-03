# PostgreSQL and versioned migrations

The application supports SQLite for lightweight development and PostgreSQL for the running service. SQLAlchemy manages connections and a bounded PostgreSQL pool. Alembic is the authority for application schema changes. The UI continues using FastAPI and has no database credential access.

## Configuration

```dotenv
# SQLite development file, and local metadata/backup location when using PostgreSQL:
TRIAGE_DB_PATH=output/triage.sqlite3

# Blank uses SQLite. Set locally for PostgreSQL; never commit the real value:
TRIAGE_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@127.0.0.1:55432/triage
```

Use URL-encoded credentials when your password contains URI-reserved characters. Connection URLs are not returned by API endpoints, logged, or placed in worker command-line arguments. The API, CLI, and worker use the same configured backend. An explicit `Store(path)` remains SQLite for isolated tests; application entry points pass the configured PostgreSQL URL. The dashboard shows **Storage: PostgreSQL** or **Storage: SQLite**.

## This workspace's local database

A portable PostgreSQL **18.6** cluster runs under `.local/postgres` on **127.0.0.1:55432**. It is not a system service and does not start automatically after reboot. Binaries are under `.tools/postgresql-18.6-5/pgsql`. Generated admin/app credentials are under `.secrets`, and the application role `triage` is not a superuser and cannot create databases or roles. `.tools`, `.local`, `.secrets`, `.env`, and `output` are ignored by Git. These directories are private local state, not encrypted by this application.

After restarting your computer, run from the project directory:

```powershell
.\.venv\Scripts\python.exe scripts/local_postgres.py start
.\.venv\Scripts\python.exe -m triage.db_cli status
.\.venv\Scripts\python.exe -m triage.api
```

Then start Streamlit in a second terminal:

```powershell
.\.venv\Scripts\python.exe -m streamlit run dashboard.py
```

`scripts/local_postgres.py status` checks the local server. `stop` shuts it down cleanly; stop scheduled triage and the API before stopping its database. Starting an already running cluster is harmless. Start dedicated scheduler and worker services separately; the dashboard enables/disables the saved schedule. See [service startup](JOBS.md).

## Recreate this local development setup

1. Download the Windows x64 binary archive linked from [EDB's PostgreSQL binaries page](https://www.enterprisedb.com/download-postgresql-binaries). This workspace used `https://get.enterprisedb.com/postgresql/postgresql-18.6-5-windows-x64-binaries.zip`.
2. Extract into `.tools/postgresql-18.6-5`, yielding `pgsql/bin/initdb.exe` inside it. The recorded SHA-256 of this downloaded archive is `e2246ba91d22345bc3d017586c09ede52d9df180b1eeb480f050445f1cad84e2`. This records the tested artifact; it is not a separate publisher signature.
3. Run `python scripts/local_postgres.py init`, then `python scripts/local_postgres.py start` using this project's virtual environment.
4. Run `python scripts/configure_local_database.py` to create the restricted application role/database. The URL is saved to `.secrets/postgres_app_url`, without printing the password.
5. Put that URL into local `.env`, then run `python -m triage.db_cli upgrade` before starting the API.

The helper refuses to overwrite an initialized cluster. You may instead use an existing supported PostgreSQL service and set `TRIAGE_DATABASE_URL`; the Windows helper is optional. For a hosted database, use its TLS settings and a secret manager. Deployment, role separation for migrations, and user-level authorization remain future milestones.

## Migration lifecycle

```powershell
.\.venv\Scripts\python.exe -m triage.db_cli status
.\.venv\Scripts\python.exe -m triage.db_cli upgrade
```

`0001_initial` freezes the application schema and adopts compatible legacy SQLite tables. It adds the previously optional digest usage columns where needed. `0002_indexes` adds indexes for review pagination, account/date queries, audit lookup, and scheduled-message accounting. Revision files are packaged with the application and should not be edited after release; add a new revision for the next schema change.

PostgreSQL requests fail closed with an actionable message if migrations are not current. They never create schema automatically. SQLite retains zero-setup developer behavior: on first open, it creates a consistent backup before applying missing revisions to legacy files. Afterward there is no scattered service-level CREATE TABLE/ALTER TABLE logic. Fresh schemas use foreign keys and uniqueness constraints. Existing SQLite tables keep their original constraints; moving them to PostgreSQL validates the stronger target constraints.

`upgrade` also runs LangGraph's own versioned PostgreSQL checkpoint setup. Application revisions are recorded in `alembic_version`; LangGraph maintains its separate checkpoint migration table. Runtime checkpoint access does not perform PostgreSQL setup. On Windows, the app selects a selector event-loop policy because Psycopg async connections do not support the proactor loop.

The baseline downgrade is deliberately disabled because dropping all tables would destroy data. Roll back code/schema using a verified backup and a maintenance window; the index-only second revision has a reversible downgrade for migration testing. No downgrade or data deletion occurs during normal upgrades.

## Import existing SQLite data

Stop the API, dashboard, and scheduled workers first. Point local configuration at a newly migrated, **empty** PostgreSQL database, retaining the old `TRIAGE_DB_PATH`. Then run:

```powershell
.\.venv\Scripts\python.exe -m triage.db_cli import-sqlite --source output/triage.sqlite3
```

The importer:

- Uses SQLite's backup API for a consistent source snapshot; the original file is not edited or deleted.
- Refuses a target with existing application rows and refuses unsupported source columns rather than silently dropping them.
- Imports related tables transactionally, preserves IDs, review/action/send states and audit history, and resets generated-ID sequences.
- Validates PostgreSQL foreign-key/unique constraints. A bad reference or import failure rolls back target data.
- Resets only scheduler ownership/process/control fields to a stopped state, preventing an old worker from silently resuming.

The cutover performed in this workspace preserved **15 emails, 5 label actions, 25 audit events, 5 proposals, 1 saved reply, and 1 digest**. Every imported row was compared with the SQLite snapshot, excluding the intentional scheduler-control reset. Details are in private `output/database-cutover.json`. Original SQLite files, a consistent snapshot, the previous `.env`, and a PostgreSQL custom-format backup are retained locally.

SQLite graph checkpoint files are retained unchanged and are not converted to PostgreSQL's internal checkpoint schema. Completed-message deduplication is imported, so saved messages are not classified again. A classification interrupted before its email row was saved may run again on PostgreSQL and incur another API call. Resume it on SQLite before cutover if that is unacceptable.

## Backup and restore

```powershell
.\.venv\Scripts\python.exe -m triage.db_cli backup
```

SQLite uses its online backup API. PostgreSQL uses `pg_dump` in custom format and saves under `output/backups`. The helper passes passwords through the child environment, not command-line arguments. Install a compatible `pg_dump` on PATH or use this workspace's matching portable binaries. Dumps can contain email data, reply text, and local history; keep them private.

Restore a PostgreSQL dump into a new empty database with a compatible `pg_restore`, supplying credentials via `PGPASSFILE`/a protected password file or a managed secret. Inspect the restored schema/records before pointing the app at it. Integration tests rehearse dump/restore into a separate synthetic database. Never use `--clean` against a live database as a routine rollback.

Clearing `TRIAGE_DATABASE_URL` returns to the original SQLite database, but it does **not** synchronize changes made after PostgreSQL cutover. That file is a pre-cutover fallback, not an ongoing replica. Restore a current PostgreSQL backup for recovery after using the new backend.

## Concurrency and limits

PostgreSQL pools are bounded per process (5 connections plus up to 5 overflow). Email action/reply state transitions lock the relevant email row with `FOR UPDATE`, preserving approval and duplicate-send protections across API threads/processes. Session advisory locks coordinate ingestion and scheduler singleton ownership across machines connected to the same database. SQLite uses local file locks and write transactions. Locks are released by their session/process lifecycle.

This is still a single-owner application with a local scheduler; Multiple-user isolation is not implemented. A durable PostgreSQL queue is now implemented in migration `0003_jobs`; see [jobs and workers](JOBS.md). Datetimes remain normalized UTC ISO strings for compatibility with preserved records, and JSON payloads remain text. Future revisions can introduce native timestamp/JSONB columns with explicit backfills. API SQL is parameter-bound; the connection boundary adapts placeholders, not user-provided SQL.

## Verification

Default offline suite (SQLite plus API mocks):

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Real PostgreSQL suite, with the local cluster running:

```powershell
$env:TRIAGE_TEST_POSTGRES = "1"
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

An external test environment can provide `TRIAGE_TEST_ADMIN_URL` for a dedicated PostgreSQL test administrator. Tests create and drop only uniquely named `triage_test_...` databases, never the configured app database. Coverage includes legacy adoption/backups, migration idempotence, API behavior on both backends, transactional import/sequence continuity, row/advisory locking, concurrent apply/send, PostgreSQL checkpoint recovery, and custom-format backup/restore. Gmail/OpenAI calls are mocked.
