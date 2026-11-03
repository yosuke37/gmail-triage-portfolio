import argparse
import asyncio
import json
import time
import hashlib
from pathlib import Path

from triage import settings
from triage.core import Email
from triage.store import Store


def gmail_client(*, writable=False, authorize=False):
    from triage.gmail import connect

    return connect(settings.local_path("GOOGLE_CREDENTIALS_PATH", ".secrets/credentials.json"), writable=writable, authorize=authorize)


async def ingest(args, store, gmail=None, *, on_progress=None, quiet=False):
    from triage.ingestion import ingestion_lock
    from triage.db_locks import store_lock

    with store_lock(store, "ingestion"):
        return await _ingest(args, store, gmail, on_progress=on_progress, quiet=quiet)


async def _ingest(args, store, gmail=None, *, on_progress=None, quiet=False):
    from triage.workflow import build_workflow
    from triage.checkpoints import checkpoint_saver
    from triage.proposals import Policy, WORKFLOW_VERSION

    if args.source == "gmail":
        account = gmail.account
        emails = gmail.fetch(args.query, args.limit, exclude=lambda email_id: store.seen(args.source, account, email_id, args.mode))
    else:
        emails = [Email(**item) for item in json.loads(args.input.read_text(encoding="utf-8"))]
        account = "local-samples"
    counts = {"processed": 0, "skipped": 0}
    async with checkpoint_saver(store) as saver:
        workflow = build_workflow(settings.classifier(args.mode), checkpointer=saver, policy=Policy(mode=args.mode, source=args.source, account=account, authorized_account=account))
        for email in emails:
            if store.seen(args.source, account, email.id, args.mode):
                counts["skipped"] += 1
                continue
            if on_progress:
                on_progress({**counts, "stage": "classifying", "limit": args.limit})
            thread_id = hashlib.sha256(json.dumps([WORKFLOW_VERSION, args.source, account, email.id, args.mode]).encode()).hexdigest()
            config = {"configurable": {"thread_id": thread_id}}
            saved = await workflow.aget_state(config)
            if saved.values and not saved.next:
                result = saved.values
            else:
                result = await workflow.ainvoke(None if saved.values else {"email": email}, config, durability="sync")
            store.add(args.source, account, email, result["suggestion"], args.mode)
            if not quiet:
                print(json.dumps(result["preview"], ensure_ascii=False))
            counts["processed"] += 1
            if on_progress:
                on_progress({**counts, "stage": "saved", "limit": args.limit, "email_id": email.id, "account": account})
    if not quiet:
        print(json.dumps(counts))
    return counts


async def evaluate(args):
    from triage.evaluation import evaluate_cases

    report = await evaluate_cases(json.loads(args.input.read_text(encoding="utf-8")), settings.classifier(args.mode))
    print(json.dumps({"mode": args.mode, **report}, indent=2))


async def benchmark(args):
    from triage.benchmark import read_cases, evaluate_proposals, write_report

    cases = read_cases(args.input, args.split, args.max_cases)
    modes = ["demo", "ai"] if args.compare else [args.mode]
    runs = [await evaluate_proposals(cases, settings.classifier(mode), mode=mode) for mode in modes]
    report = write_report(args.report, cases, runs, split=args.split, min_label_accuracy=args.min_label_accuracy)
    print(json.dumps({"report": str(args.report.resolve()), "gates": report["gates"], "runs": [{"mode": r["mode"], "metrics": r["metrics"]} for r in runs]}, indent=2))
    return report["passed"]


def main():
    parser = argparse.ArgumentParser(description="Email triage: ingest, review locally, then apply approved labels.")
    parser.add_argument("command", nargs="?", default="ingest", choices=("ingest", "auth", "apply", "undo", "evaluate", "doctor", "mark-read", "mark-unread", "benchmark"))
    parser.add_argument("--mode", choices=("demo", "ai"), default="demo")
    parser.add_argument("--source", choices=("sample", "gmail"), default="sample")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--query", default="in:inbox newer_than:7d")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--write", action="store_true", help="Authorize Gmail label changes (auth only).")
    parser.add_argument("--id", type=int, help="Local review ID for apply/undo.")
    parser.add_argument("--interval", type=int, default=0, help="Poll Gmail every N seconds (minimum 60); never auto-applies labels.")
    parser.add_argument("--cycles", type=int, default=1, help="Number of polling cycles; 0 means until Ctrl+C.")
    parser.add_argument("--split", choices=("dev", "holdout", "all"), default="dev")
    parser.add_argument("--compare", action="store_true", help="Benchmark both demo and AI (uses API credits).")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--report", type=Path, default=settings.ROOT / "output" / "evaluations" / "latest.json")
    parser.add_argument("--check", action="store_true", help="Exit nonzero when benchmark gates fail.")
    parser.add_argument("--min-label-accuracy", type=float, default=.8)
    args = parser.parse_args()
    if not args.input:
        args.input = settings.ROOT / "data" / ("proposal_cases.json" if args.command == "benchmark" else "evaluation.json" if args.command == "evaluate" else "sample_emails.json")
    if (args.max_cases is not None and args.max_cases < 1) or not 0 <= args.min_label_accuracy <= 1:
        parser.error("--max-cases must be positive and --min-label-accuracy must be between 0 and 1.")
    if args.interval and (args.interval < 60 or args.source != "gmail" or args.command != "ingest"):
        parser.error("--interval requires Gmail ingestion and at least 60 seconds.")
    if args.cycles < 0 or (args.cycles != 1 and not args.interval):
        parser.error("Use --interval with repeated cycles; --cycles must be nonnegative.")
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100.")
    store = None
    try:
        if args.command == "doctor":
            import os

            print(json.dumps({
                "openai_api_key_configured": bool(os.getenv("OPENAI_API_KEY")),
                "openai_model_configured": bool(os.getenv("OPENAI_MODEL")),
                "google_oauth_client_present": settings.local_path("GOOGLE_CREDENTIALS_PATH", ".secrets/credentials.json").exists(),
                "database": str(settings.database_path()),
            }, indent=2))
        elif args.command == "auth":
            client = gmail_client(writable=args.write, authorize=True)
            print(f"Gmail authorized for {'label changes' if args.write else 'read-only ingestion'}: {client.account}")
        elif args.command == "evaluate":
            asyncio.run(evaluate(args))
        elif args.command == "benchmark":
            passed = asyncio.run(benchmark(args))
            if args.check and not passed:
                parser.exit(1, "Benchmark regression gates failed; see the JSON report.\n")
        elif args.command in ("apply", "undo", "mark-read", "mark-unread"):
            if args.id is None:
                raise ValueError("Provide a local review ID with --id.")
            store = settings.open_store()
            client = gmail_client(writable=True)
            if args.command in ("mark-read", "mark-unread"):
                print(store.read_state(args.id, client, unread=args.command == "mark-unread"))
            else:
                print(getattr(store, args.command)(args.id, client))
        else:
            settings.classifier(args.mode)
            store = settings.open_store()
            client = gmail_client() if args.source == "gmail" else None
            cycle = 0
            while True:
                asyncio.run(ingest(args, store, client))
                cycle += 1
                if not args.interval or (args.cycles and cycle >= args.cycles):
                    break
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped. Completed emails remain saved locally.")
    except Exception as exc:
        message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else "Operation failed. Check credentials, network access, and configuration; completed items remain saved."
        parser.exit(1, f"{type(exc).__name__}: {message}\n")
    finally:
        if store:
            store.close()
