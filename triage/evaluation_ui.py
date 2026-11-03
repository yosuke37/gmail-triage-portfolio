"""Read-only display of saved synthetic benchmark artifacts."""
import json

import streamlit as st



def evaluation_panel(store):
    with st.expander("Agent evaluation results"):
        st.caption("Saved synthetic benchmark results. Opening this panel makes no Gmail or OpenAI calls. These authored scenarios are engineering checks, not a production accuracy estimate.")
        available = store.evaluations()
        if not available:
            st.info("Run python -m triage benchmark --mode demo to create a JSON report; see docs/EVALUATION.md for report locations.")
            return
        split = st.selectbox("Evaluation split", list(available), key="evaluation-split")
        report = available[split]
        st.text(f"Model: {report['model'] or 'baseline only'} · Workflow: {report['workflow_version']} · Recorded: {report['created']}")
        st.dataframe([{"mode": run["mode"], **{k: run["metrics"][k] for k in ("case_count", "label_accuracy", "review_accuracy", "action_set_accuracy", "action_f1", "schema_valid_rate", "evidence_substring_rate", "unsafe_automatic_count", "latency_p50_seconds", "latency_p95_seconds")}} for run in report["runs"]], hide_index=True, width="stretch")
        mode = st.selectbox("Inspect run", [run["mode"] for run in report["runs"]], key="evaluation-mode")
        run = next(run for run in report["runs"] if run["mode"] == mode)
        failures = [case for case in run["cases"] if not (case["schema_valid"] and case["label_correct"] and case["review_correct"] and case["action_set_correct"]) or case["unsafe_automatic"]]
        st.text(f"Recorded requests/tokens: {run['metrics']['usage']}\nRegression gates: {report['gates'][mode]}")
        st.caption(f"Cases with at least one classification, review, action-set, or safety mismatch: {len(failures)}")
        if failures:
            case_id = st.selectbox("Inspect a mismatch", [case["id"] for case in failures], key="evaluation-case")
            case = next(case for case in failures if case["id"] == case_id)
            st.json(case)
        st.download_button("Download evaluation report", json.dumps(report, indent=2), file_name=f"proposals-{split}.json", mime="application/json", key="evaluation-download")
