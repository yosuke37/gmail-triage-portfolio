"""Automation panel; rendering it never starts a background worker."""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from types import SimpleNamespace
from triage.core import LABELS
from triage.drafting import TONES


def local_time(timestamp):
    if not timestamp:
        return "—"
    return datetime.fromtimestamp(timestamp, ZoneInfo("America/New_York")).strftime("%b %d, %I:%M:%S %p %Z")


def automation_panel(store):
    view = store.automation_info()
    config = SimpleNamespace(**view["config"])
    state = view["state"]
    with st.expander("Scheduled triage and automation"):
        st.caption("Start enables the saved schedule. Keep the dedicated scheduler and worker services running; they continue after the browser closes. AI mode sends matching emails to OpenAI and uses API credits. Replies are never sent automatically.")
        stale = state["enabled"] and (not state["heartbeat"] or time.time() - state["heartbeat"] > 180)
        st.text(f"Schedule: {'unresponsive — stop/restart to recover' if stale else state['state']}\nAccount: {state['account'] or 'Chosen from Gmail authorization when started'}\nNext run: {local_time(state['next_run'])}")
        st.caption(f"Scheduled messages processed today (America/New_York): {view['today_processed']} / {config.daily_limit}")
        with st.form("automation-settings"):
            interval = st.number_input("Run every N minutes", min_value=1, max_value=1440, value=config.interval_minutes, key="auto-interval")
            batch = st.number_input("Messages per scheduled run", min_value=1, max_value=100, value=config.batch_size, key="auto-batch")
            daily = st.number_input("Daily scheduled-message limit", min_value=1, max_value=1000, value=config.daily_limit, key="auto-daily")
            query = st.text_input("Scheduled Gmail search", value=config.query, max_chars=500, key="auto-query")
            mode = st.radio("Scheduled classification", ["ai", "demo"], index=0 if config.mode == "ai" else 1, key="auto-mode", horizontal=True)
            labels = st.multiselect("Automatically apply these labels", LABELS, default=config.auto_labels, key="auto-labels")
            st.caption("Leave empty for review-only operation. Only new, unflagged AI suggestions matching enabled labels qualify. Existing manual decisions are preserved. Read/unread status stays unchanged.")
            reply_labels = st.multiselect("Suggest replies for these categories", LABELS, default=config.suggested_reply_labels, key="auto-reply-labels")
            reply_tone = st.selectbox("Suggested reply tone", TONES, index=TONES.index(config.reply_tone), key="auto-reply-tone")
            reply_instruction = st.text_area("Instruction for suggested replies", value=config.reply_instruction, max_chars=2000, key="auto-reply-instruction")
            digest = st.checkbox("Generate a daily AI digest", value=config.digest_enabled, key="auto-digest")
            hour = st.number_input("Digest hour (0–23, America/New_York)", min_value=0, max_value=23, value=config.digest_hour, key="auto-digest-hour")
            minute = st.number_input("Digest minute", min_value=0, max_value=59, value=config.digest_minute, key="auto-digest-minute")
            st.caption("Suggested replies add AI calls for new, unflagged matching emails and stay local for review. Daily digests use saved message metadata, appear in the dashboard, and are not emailed. Both are opt-in and use API credits.")
            if st.form_submit_button("Save automation settings"):
                try:
                    store.save_automation(dict(interval_minutes=int(interval), batch_size=int(batch), daily_limit=int(daily), query=query, mode=mode, auto_labels=labels,
                                     suggested_reply_labels=reply_labels, reply_tone=reply_tone, reply_instruction=reply_instruction,
                                     digest_enabled=digest, digest_hour=int(hour), digest_minute=int(minute)))
                    st.success("Settings saved. Rules are checked before each action; schedule/query changes take effect on the next cycle.")
                except ValueError as exc:
                    st.error(str(exc))
        start, stop, refresh = st.columns(3)
        stopping = state["state"] == "stopping" and state["heartbeat"] and time.time() - state["heartbeat"] < 180
        if start.button("Start scheduled triage", key="auto-start", disabled=bool(state["enabled"])):
            try:
                store.start_automation()
                st.rerun()
            except Exception as exc:
                st.error(str(exc) if isinstance(exc, ValueError) else "Unable to start worker. Check Gmail authorization, configuration, and local permissions.")
        if stop.button("Stop scheduled triage", key="auto-stop"):
            store.stop_automation()
            st.info("Stop requested. An in-flight Gmail/API request may finish; no further messages will be started.")
        if refresh.button("Refresh automation status", key="auto-refresh"):
            st.rerun()
        st.caption("The daily message limit applies to scheduled classification, not extra drafting/digest calls, manual fetching, or API retries. Run token totals include successful classification and suggested replies; consult OpenAI billing for actual charges. Start the worker and scheduler services again after a reboot; the enabled schedule and queued jobs are retained.")
        st.dataframe(view["runs"], width="stretch", hide_index=True)
