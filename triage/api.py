"""Local, single-owner application API. Run with python -m triage.api."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.trustedhost import TrustedHostMiddleware

from triage import settings
from triage.api_models import JobResponse, AutomationRequest, DigestRequest, DraftRequest, IngestRequest, PrepareRequest, ReadRequest, ReviewRequest, SendRequest, SummaryResponse, EmailPageResponse, EmailDetailsResponse
from triage.store import Store


def create_app(*, database_path=None, database_url=None, token=None):
    from triage.api_security import api_token

    secret = token or api_token(create=True)
    db_path = Path(database_path) if database_path else settings.database_path()
    db_url = database_url if database_url is not None else settings.database_url() if database_path is None else None
    app = FastAPI(title="Gmail Triage Application API", version="1.0.0")
    hosts = ["127.0.0.1", "localhost", "[::1]"]
    hosts += [h.strip() for h in os.getenv("TRIAGE_API_ALLOWED_HOSTS", "").split(",") if h.strip() and "*" not in h]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    bearer = HTTPBearer(auto_error=False)

    @contextmanager
    def database():
        # Open/use/close in the same request thread; no global SQLite connection.
        store = Store(db_path, database_url=db_url)
        try:
            yield store
        finally:
            store.close()

    def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not credentials or not secrets.compare_digest(credentials.credentials.encode(), secret.encode()):
            raise HTTPException(401, "Local API authentication required.", headers={"WWW-Authenticate": "Bearer"})
        return "local-owner"

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        from triage.request_context import request_id

        request.state.request_id = uuid4().hex
        token = request_id.set(request.state.request_id)
        try:
            response = await call_next(request)
        finally:
            request_id.reset(token)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def domain_error(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc) if type(exc) is ValueError else "Application data validation failed.", "request_id": request.state.request_id}, headers={"X-Request-ID": request.state.request_id})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not reflect request bodies, draft text, or credentials in errors.
        return JSONResponse(status_code=422, content={"detail": "Invalid request fields.", "fields": [list(e["loc"]) for e in exc.errors()], "request_id": request.state.request_id})

    @app.exception_handler(Exception)
    async def provider_error(request, exc):
        return JSONResponse(status_code=502, content={"detail": "Operation failed. Check authorization, API credits, and connectivity. Saved work is preserved; check Sent before retrying any send.", "request_id": request.state.request_id}, headers={"X-Request-ID": request.state.request_id, "Cache-Control": "no-store"})

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "gmail-triage-api", "version": "1.0.0"}

    @app.get("/ready")
    def ready():
        try:
            with database() as store:
                store.db.execute("SELECT 1").fetchone()
            return {"status": "ready"}
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not ready"})

    router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])

    def require_row(store, row_id, *, gmail=False):
        try:
            row = store.get(row_id)
        except ValueError:
            raise HTTPException(404, "Review item not found.") from None
        if gmail and row["source"] != "gmail":
            raise HTTPException(409, "Sample records cannot perform Gmail actions.")
        return row

    def gmail_for(store, row_id, *, writable=False):
        from triage.cli import gmail_client

        require_row(store, row_id, gmail=True)
        client = gmail_client(writable=writable)
        store.gmail_row(row_id, client)
        return client

    @router.get("/summary", response_model=SummaryResponse)
    def summary():
        with database() as store:
            result = dict(store.db.execute("SELECT COUNT(*) AS total,COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END),0) AS pending,COALESCE(SUM(CASE WHEN status='applied' THEN 1 ELSE 0 END),0) AS applied FROM emails").fetchone())
            return {**result, "backend": store.db.backend}

    @router.get("/emails", response_model=EmailPageResponse)
    def emails(status: list[str] = Query(default=["pending", "approved", "rejected", "applied", "undone"]), source: str = "All", label: str = "All", search: str = Query(default="", max_length=500), flagged: bool = False, order: str = "Newest first", page: int = Query(default=1, ge=1), page_size: int = Query(default=10, ge=1, le=100)):
        conditions, values = [], []
        conditions.append("status IN (" + ",".join("?" for _ in status) + ")")
        values.extend(status)
        for column, value in (("source", source), ("label", label)):
            if value != "All":
                conditions.append(f"{column}=?")
                values.append(value)
        if flagged:
            conditions.append("needs_review=1")
        if search.strip():
            literal = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append("(subject || ' ' || sender || ' ' || account || ' ' || reason || ' ' || CAST(id AS TEXT)) LIKE ? ESCAPE '\\'")
            values.append(f"%{literal}%")
        where = " AND ".join(conditions)
        sort = {"Oldest first": "id ASC", "Needs attention first": "(status!='pending'),needs_review DESC,id DESC"}.get(order, "id DESC")
        with database() as store:
            total = store.db.execute(f"SELECT COUNT(*) FROM emails WHERE {where}", values).fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            rows = [dict(row) for row in store.db.execute(f"SELECT * FROM emails WHERE {where} ORDER BY {sort} LIMIT ? OFFSET ?", [*values, page_size, (page - 1) * page_size])]
            return {"items": rows, "total": total, "pages": pages, "page": page}

    @router.get("/emails/{row_id}", response_model=EmailDetailsResponse)
    def details(row_id: int):
        from triage.automation import Automation
        from triage.proposals import Policy, decisions
        from triage.summaries import Summaries

        with database() as store:
            row = require_row(store, row_id)
            bundle = store.proposal(row_id)
            verdicts = []
            if bundle:
                auto = Automation(store)
                config, state = auto.config(), auto.state()
                verdicts = decisions(bundle, Policy(mode=row["mode"], source=row["source"], account=row["account"], authorized_account=state["account"] or row["account"], auto_labels=tuple(config.auto_labels) if state["enabled"] else (), auto_draft_labels=tuple(config.suggested_reply_labels) if state["enabled"] else ()))
            return {"email": row, "proposal": bundle, "decisions": verdicts, "reply": store.reply(row_id), "suggestion": Summaries(store).suggestion(row_id)}

    @router.post("/emails/{row_id}/review")
    def review(row_id: int, body: ReviewRequest):
        with database() as store:
            require_row(store, row_id)
            store.review(row_id, body.label, approve=body.approve)
            return {"status": "approved" if body.approve else "rejected"}

    @router.post("/emails/{row_id}/label")
    def apply_label(row_id: int):
        with database() as store:
            return {"status": store.apply(row_id, gmail_for(store, row_id, writable=True))}

    @router.post("/emails/{row_id}/label/undo")
    def undo_label(row_id: int):
        with database() as store:
            return {"status": store.undo(row_id, gmail_for(store, row_id, writable=True))}

    @router.post("/emails/{row_id}/read-state")
    def read_state(row_id: int, body: ReadRequest):
        with database() as store:
            return {"status": store.read_state(row_id, gmail_for(store, row_id, writable=True), unread=body.unread)}

    @router.get("/emails/{row_id}/original")
    def original(row_id: int):
        with database() as store:
            client = gmail_for(store, row_id)
            return client.original(store.get(row_id)["email_id"])

    def enqueue(store, request, kind, payload):
        from triage.jobs import Jobs

        key = request.headers.get("Idempotency-Key") or uuid4().hex
        if not 1 <= len(key) <= 200 or not key.isascii():
            raise HTTPException(422, "Invalid idempotency key.")
        return Jobs(store).enqueue(kind, payload, key=key, request_id=request.state.request_id)

    @router.post("/emails/{row_id}/reply/draft", status_code=202, response_model=JobResponse)
    def draft(row_id: int, body: DraftRequest, request: Request):
        with database() as store:
            require_row(store, row_id, gmail=True)
            return enqueue(store, request, "draft_reply", {**body.model_dump(), "row_id": row_id})

    @router.post("/emails/{row_id}/reply/prepare")
    def prepare(row_id: int, body: PrepareRequest):
        with database() as store:
            return store.prepare_reply(row_id, gmail_for(store, row_id, writable=True), body.body)

    @router.post("/emails/{row_id}/reply/send")
    def send(row_id: int, body: SendRequest):
        with database() as store:
            return {"status": store.send_reply(row_id, gmail_for(store, row_id, writable=True), expected_message_id=body.expected_message_id)}

    @router.post("/ingestions", status_code=202, response_model=JobResponse)
    def ingest(body: IngestRequest, request: Request):
        with database() as store:
            return enqueue(store, request, "ingest", body.model_dump())

    @router.get("/jobs", response_model=list[JobResponse], response_model_exclude_unset=True)
    def jobs():
        from triage.jobs import Jobs
        with database() as store:
            return Jobs(store).list()

    @router.get("/services")
    def services():
        from triage.jobs import Jobs
        with database() as store:
            return Jobs(store).services()

    @router.get("/operations")
    def operations():
        from triage.jobs import Jobs
        with database() as store:
            return Jobs(store).operations()

    @router.get("/jobs/{job_id}", response_model=JobResponse)
    def job(job_id: str):
        from triage.jobs import Jobs
        with database() as store:
            try:
                return Jobs(store).get(job_id)
            except ValueError:
                raise HTTPException(404, "Job not found.") from None

    @router.post("/jobs/{job_id}/cancel", response_model=JobResponse)
    def cancel_job(job_id: str):
        from triage.jobs import Jobs
        with database() as store:
            try:
                return Jobs(store).cancel(job_id)
            except ValueError:
                raise HTTPException(404, "Job not found.") from None

    @router.post("/jobs/{job_id}/retry", status_code=202, response_model=JobResponse)
    def retry_job(job_id: str, request: Request):
        from triage.jobs import Jobs, OWNER
        with database() as store:
            row = store.db.execute("SELECT * FROM jobs WHERE id=? AND owner_id=?", (job_id, OWNER)).fetchone()
            if not row:
                raise HTTPException(404, "Job not found.")
            if row["state"] != "failed" or row["kind"] not in ("ingest", "draft_reply", "scheduled_triage"):
                raise HTTPException(409, "Only failed ingestion, draft, or triage jobs can be retried here. Use the digest panel for digest retries.")
            return enqueue(store, request, row["kind"], json.loads(row["payload"]))

    @router.get("/automation")
    def automation_info():
        from triage.automation import Automation

        with database() as store:
            auto = Automation(store)
            state = auto.state()
            state.pop("owner", None)
            return {"config": asdict(auto.config()), "state": state, "today_processed": auto.daily_processed(), "runs": auto.runs()}

    @router.put("/automation")
    def automation_save(body: AutomationRequest):
        from triage.automation import Automation, Config

        with database() as store:
            Automation(store).save(Config(**body.model_dump()))
            return {"status": "saved"}

    @router.post("/automation/{action}")
    def automation_action(action: str):
        from triage.automation import Automation, start_worker

        with database() as store:
            if action == "start":
                start_worker(store)
            elif action == "stop":
                Automation(store).stop()
            else:
                raise HTTPException(404, "Unknown worker action.")
            return {"status": action + " requested"}

    @router.get("/accounts")
    def accounts():
        from triage.summaries import Summaries

        with database() as store:
            Summaries(store)
            return [row[0] for row in store.db.execute("SELECT account FROM emails WHERE source='gmail' UNION SELECT account FROM daily_digests ORDER BY account")]

    @router.get("/digests")
    def digests(account: str):
        from triage.summaries import Summaries

        with database() as store:
            return Summaries(store).digests(account)

    @router.post("/digests/generate", status_code=202, response_model=JobResponse)
    def digest_generate(body: DigestRequest, request: Request):
        from triage.summaries import Summaries, TZ

        with database() as store:
            data = Summaries(store)
            if body.account not in accounts() or body.day > datetime.now(TZ).date():
                raise HTTPException(422, "Choose a saved Gmail account and a date no later than today.")
            payload = body.model_dump(mode="json")
            existing = next((row for row in data.digests(body.account) if row["day"] == body.day.isoformat()), None)
            if body.regenerate and existing and existing["state"] == "ready":
                payload.update(expected_digest_id=existing["id"], expected_digest_created=existing["created"])
            return enqueue(store, request, "daily_digest", payload)

    @router.post("/digests/{digest_id}/clear-attempt")
    def digest_clear(digest_id: int):
        from triage.summaries import Summaries

        with database() as store:
            Summaries(store)
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
            with store.db:
                count = store.db.execute("DELETE FROM daily_digests WHERE id=? AND (state='failed' OR (state='generating' AND created<?))", (digest_id, cutoff)).rowcount
            return {"cleared": bool(count)}

    @router.get("/audit")
    def audit():
        with database() as store:
            return [dict(row) for row in store.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100")]

    @router.get("/evaluations")
    def evaluations():
        return {name: json.loads((settings.ROOT / "reports" / f"proposals-{name}.json").read_text(encoding="utf-8")) for name in ("dev", "holdout") if (settings.ROOT / "reports" / f"proposals-{name}.json").exists()}

    app.include_router(router)
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host=os.getenv("TRIAGE_API_BIND", "127.0.0.1"), port=int(os.getenv("PORT", "8000")), access_log=False)
