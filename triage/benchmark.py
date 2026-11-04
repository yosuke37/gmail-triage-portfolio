"""Reproducible, side-effect-free proposal evaluation with explicit oracles."""
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import statistics
import time

from triage.core import Email
from triage.proposals import Policy, PROMPT_VERSION, WORKFLOW_VERSION
from triage.store import now
from triage.usage import capture_usage


def read_cases(path, split="dev", max_cases=None):
    cases = json.loads(path.read_text(encoding="utf-8"))
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Evaluation case IDs must be unique.")
    selected = [case for case in cases if split == "all" or case["split"] == split]
    if max_cases:
        selected = selected[:max_cases]
    if not selected:
        raise ValueError("No evaluation cases selected.")
    return selected


async def evaluate_proposals(cases, classify, *, mode):
    from triage.workflow import build_workflow

    if not cases:
        raise ValueError("No evaluation cases selected.")

    results = []
    for case in cases:
        usage = {key: 0 for key in ("requests", "input_tokens", "output_tokens", "total_tokens")}

        def on_usage(event):
            for key in usage:
                usage[key] += event[key]

        started = time.perf_counter()
        row = {"id": case["id"], "tags": case["tags"], "expected_label": case["expected_label"], "expected_actions": case["expected_actions"], "usage": usage}
        try:
            policy = Policy(**{"mode": mode, "source": "gmail", "account": "owner@example.com", "authorized_account": "owner@example.com", **case.get("policy", {})})
            graph = build_workflow(classify, policy=policy)
            with capture_usage(on_usage):
                state = await graph.ainvoke({"email": Email(**case["email"])})
            suggestion = state["suggestion"]
            bundle = suggestion.proposal
            actual_actions = {action["kind"] for action in bundle["plan"]["actions"]}
            expected_actions = set(case["expected_actions"])
            automatic = [d["kind"] for d in bundle["decisions"] if d["status"] == "automatic"]
            violations = [kind for kind in automatic if kind not in case.get("allowed_automatic", [])]
            row.update({
                "schema_valid": True, "actual_label": suggestion.label, "label_correct": suggestion.label == case["expected_label"],
                "review_correct": suggestion.needs_review == case["expected_review"], "actual_review": suggestion.needs_review,
                "actual_actions": sorted(actual_actions), "action_set_correct": actual_actions == expected_actions,
                "action_tp": len(actual_actions & expected_actions), "action_fp": len(actual_actions - expected_actions), "action_fn": len(expected_actions - actual_actions),
                "grounded_actions": sum(bundle["evidence_valid"]), "action_count": len(bundle["evidence_valid"]),
                "unsafe_automatic": violations, "proposal": bundle,
            })
        except Exception as exc:
            # Failed cases count against metrics rather than being dropped.
            row.update({"schema_valid": False, "label_correct": False, "review_correct": False, "action_set_correct": False,
                        "action_tp": 0, "action_fp": 0, "action_fn": len(case["expected_actions"]), "grounded_actions": 0,
                        "action_count": 0, "unsafe_automatic": [], "error_type": type(exc).__name__})
        row["latency_seconds"] = round(time.perf_counter() - started, 4)
        results.append(row)
    count = len(results)
    tp, fp, fn = (sum(r[key] for r in results) for key in ("action_tp", "action_fp", "action_fn"))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    latency = sorted(row["latency_seconds"] for row in results)
    actions = sum(r["action_count"] for r in results)
    metrics = {
        "case_count": count, "schema_valid_rate": sum(r["schema_valid"] for r in results) / count,
        "label_accuracy": sum(r["label_correct"] for r in results) / count,
        "review_accuracy": sum(r["review_correct"] for r in results) / count,
        "action_set_accuracy": sum(r["action_set_correct"] for r in results) / count,
        "action_precision": precision, "action_recall": recall, "action_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "evidence_substring_rate": sum(r["grounded_actions"] for r in results) / actions if actions else 0.0,
        "unsafe_automatic_count": sum(len(r["unsafe_automatic"]) for r in results),
        "latency_p50_seconds": statistics.median(latency), "latency_p95_seconds": latency[math.ceil(count * .95) - 1],
        "usage": {key: sum(r["usage"][key] for r in results) for key in results[0]["usage"]},
    }
    slices = {}
    for tag in sorted({tag for case in cases for tag in case["tags"]}):
        group = [r for r in results if tag in r["tags"]]
        slices[tag] = {"count": len(group), "label_accuracy": sum(r["label_correct"] for r in group) / len(group),
                       "unsafe_automatic_count": sum(len(r["unsafe_automatic"]) for r in group)}
    return {"mode": mode, "metrics": metrics, "slices": slices, "cases": results}


def write_report(path, cases, runs, *, split, min_label_accuracy):
    root = Path(__file__).resolve().parent
    source = b"".join((root / name).read_bytes() for name in ("core.py", "classifier.py", "proposals.py", "workflow.py", "attachments.py", "attachment_parser.py", "docx_extract.py", "local_ocr.py", "benchmark.py"))
    gates = {run["mode"]: {
        "valid_schema": run["metrics"]["schema_valid_rate"] == 1,
        "zero_unsafe_automatic": run["metrics"]["unsafe_automatic_count"] == 0,
        "label_accuracy": run["metrics"]["label_accuracy"] >= min_label_accuracy,
        "grounded_evidence": run["metrics"]["evidence_substring_rate"] == 1,
    } for run in runs}
    report = {
        "created": now(), "split": split, "dataset_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
        "source_sha256": hashlib.sha256(source).hexdigest(), "prompt_version": PROMPT_VERSION, "workflow_version": WORKFLOW_VERSION,
        "model": os.getenv("OPENAI_MODEL") if any(run["mode"] == "ai" for run in runs) else None,
        "python": platform.python_version(), "dependencies": {name: importlib.metadata.version(name) for name in ("openai-agents", "langgraph", "pydantic", "pypdf", "Pillow", "defusedxml")},
        "minimum_label_accuracy": min_label_accuracy, "gates": gates, "passed": all(gates["ai" if "ai" in gates else "demo"].values()),
        "runs": runs,
        "limitations": ["Small authored synthetic development/holdout sets, not a production benchmark.",
                        "Substring evidence checks verify grounding, not semantic correctness.",
                        "Risk pattern matching is incomplete. Safety is measured only against these explicit cases.",
                        "Token counts cover returned SDK usage; provider billing and failed/retried calls may differ."],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report
