"""Produce synthetic recovery evidence with zero external provider requests."""
import argparse
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from triage.jobs import Jobs
from triage.store import Store
from triage.usage import capture_usage, report_usage


def run():
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder) / "synthetic.db")
        clock = [1000.0]
        queue = Jobs(store, clock=lambda: clock[0])
        queue.enqueue("ingest", {"query": "synthetic", "mode": "demo", "limit": 1}, key="recovery-demo", request_id="synthetic-demo")
        first = queue.claim("worker-a")
        with capture_usage(lambda event: queue.usage(first, event)):
            report_usage(SimpleNamespace(context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=1, input_tokens=20, output_tokens=10, total_tokens=30))))
        clock[0] += 2
        queue.finish(first, error="Synthetic transient failure", retry=True)
        clock[0] += 10
        second = queue.claim("worker-a", lease_seconds=5)
        # Simulated process death: no finish or renewal for this attempt.
        clock[0] += 6
        third = queue.claim("worker-b")
        stale_write_rejected = not queue.finish(second, {"stale": True})
        clock[0] += 1
        queue.finish(third, {"processed": 1})
        evidence = queue.operations()
        store.close()
        return {"synthetic": True, "provider_calls": 0, "usage_is_simulated": True,
            "stale_worker_write_rejected": stale_write_rejected, **evidence}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    encoded = json.dumps(run(), indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
        print("Synthetic recovery evidence saved; no Gmail or OpenAI requests were made.")
    else:
        print(encoded)
