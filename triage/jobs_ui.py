"""Durable job status, refreshed independently from review editors."""
import streamlit as st

from triage.api_client import ApiClient

TERMINAL = {"succeeded", "failed", "cancelled"}


def track_job(job):
    tracked = dict(st.session_state.get("tracked-jobs", {}))
    tracked[job["id"]] = job["state"]
    st.session_state["tracked-jobs"] = tracked
    if job["kind"] == "ingest":
        st.session_state["fetch-job"] = job["id"]
    elif job["kind"] == "draft_reply" and job.get("target_row_id"):
        st.session_state[f"draft-job-{job['target_row_id']}"] = job["id"]


def job_status(job):
    st.caption(f"{job['kind']}: {job['state']} · job {job['id'][:12]} · attempt {job['attempts']}/{job['max_attempts']}")
    if job["progress"]:
        st.json(job["progress"], expanded=False)
    if job["error"]:
        st.error(job["error"])


@st.fragment(run_every=2)
def jobs_panel():
    client = ApiClient(st.session_state.setdefault("queue-keys", {}))
    try:
        jobs = client.jobs()
        # A full refresh only on completion lets review cards see saved results.
        tracked = dict(st.session_state.get("tracked-jobs", {}))
        changed = False
        for job_id, previous in list(tracked.items()):
            if previous not in TERMINAL:
                current = client.job(job_id)["state"]
                tracked[job_id] = current
                changed |= current in TERMINAL
        initialized = st.session_state.get("jobs-monitor-ready", False)
        for job in jobs:
            if job["id"] not in tracked:
                tracked[job["id"]] = job["state"]
                changed |= initialized and job["state"] in TERMINAL
        st.session_state["jobs-monitor-ready"] = True
        st.session_state["tracked-jobs"] = tracked
        if changed:
            st.rerun()
        with st.expander("Background jobs"):
            st.caption("Jobs survive browser/API restarts. Cancellation is cooperative; an in-flight request may finish. Retries may use additional API credits. Sending replies always requires review.")
            services = client.services()
            operations = client.operations()
            totals = operations["totals"]
            st.caption(f"Attempts: {totals['attempts']} · Failed jobs: {operations['jobs'].get('failed', 0)} · Average attempt: {totals['average_seconds']:.1f}s · Observed tokens: {totals['total_tokens']} · Expired leases: {totals['expired_leases']}")
            st.caption("Usage includes observed SDK responses only; interrupted calls can still be billed. Attempt latency includes provider work; OpenAI billing is authoritative.")
            if operations["recent_attempts"]:
                st.dataframe(operations["recent_attempts"], hide_index=True, width="stretch")
            for kind in ("worker", "scheduler"):
                healthy = any(row["healthy"] and row["kind"] == kind for row in services)
                st.text(f"{kind.title()}: {'available' if healthy else 'offline — start python -m triage.' + ('job_worker' if kind == 'worker' else 'scheduler')}")
            if not jobs:
                st.info("No background jobs yet.")
            for job in jobs[:10]:
                job_status(job)
                if job["state"] in ("queued", "running"):
                    if st.button("Cancel job", key="cancel-job-" + job["id"], disabled=bool(job["cancel_requested"])):
                        client.cancel_job(job["id"])
                        st.rerun()
                elif job["state"] == "succeeded" and job["kind"] == "draft_reply":
                    if st.button("Load completed draft into editor", key="load-job-" + job["id"]):
                        track_job(job)
                        st.rerun()
                elif job["state"] == "failed" and job["kind"] != "daily_digest":
                    if st.button("Retry job (may use API credits)", key="retry-job-" + job["id"]):
                        track_job(client.retry_job(job["id"]))
                        st.rerun()
    except ValueError as exc:
        st.error(str(exc))
    finally:
        client.close()
