from datetime import datetime
import json

import streamlit as st

from triage.jobs_ui import track_job
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")


def digest_panel(store):
    with st.expander("Daily digest"):
        st.caption("Summaries of messages saved by this app and possible outstanding requests. Counts reflect ingestion dates, not Gmail received dates. A digest is a snapshot, not a live inbox view.")
        accounts = store.accounts()
        if not accounts:
            st.info("Fetch Gmail messages to create your first digest.")
            return
        account = st.selectbox("Digest account", accounts, key="digest-account")
        day = st.date_input("Digest date (America/New_York)", value=datetime.now(TZ).date(), max_value=datetime.now(TZ).date(), key="digest-date").isoformat()
        st.caption("Generate uses OpenAI API credits and sends saved sender/subject/classification metadata. Full email bodies are not included. One snapshot is kept per account/date.")
        if st.button("Generate daily digest now", key="digest-generate"):
            try:
                job = store.generate_digest(account, day)
                track_job(job)
                st.success("Digest queued. Follow its progress in Background jobs.")
            except Exception:
                st.error("Digest generation failed. Check OpenAI configuration and credits. Details are shown below.")
        digests = store.digests(account)
        selected = next((row for row in digests if row["day"] == day), None)
        if selected and selected["state"] == "ready":
            content = json.loads(selected["content"])
            stats = content["stats"]
            st.text(content["overview"])
            st.caption(f"Ingested: {stats['ingested_count']} · Flagged for review: {stats['needs_review_count']} · Possible outstanding items (up to 20): {stats['outstanding_candidates']}")
            st.text("Labels: " + json.dumps(stats["labels"]))
            for item in content["highlights"]:
                st.text(f"Review #{item['row_id']}: {item['summary']}\nSuggested next step: {item['suggested_next_step']}")
            st.download_button("Download digest JSON", selected["content"], file_name=f"digest-{day}.json", mime="application/json", key="digest-download")
            if st.button("Regenerate selected digest (uses API credits)", key="digest-regenerate"):
                try:
                    with st.spinner("Updating digest snapshot…"):
                        job = store.generate_digest(account, day, regenerate=True)
                        track_job(job)
                    st.rerun()
                except Exception:
                    st.error("Could not regenerate the digest. Check configuration and credits, then refresh this panel.")
        elif selected:
            st.warning(selected["error"] or "Generation is in progress or was interrupted. No automatic retry will incur additional charges.")
            if selected["state"] in ("failed", "generating"):
                confirm = st.checkbox("I checked that generation is not still running and want to allow a new attempt (API credits).", key="digest-retry-confirm")
                if st.button("Clear failed/interrupted attempt", disabled=not confirm, key="digest-retry"):
                    store.clear_digest(selected["id"])
                    st.rerun()
        else:
            st.info("No digest saved for this date. Generate one now or enable daily digests in automation settings.")
        if digests:
            st.dataframe([{k: row[k] for k in ("day", "state", "created", "requests", "total_tokens")} for row in digests], hide_index=True, width="stretch")
