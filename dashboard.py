"""Run with: python -m streamlit run dashboard.py"""
import streamlit as st

from triage.core import LABELS
from triage.api_client import ApiClient
from triage.drafting import TONES
from urllib.parse import quote
from triage.automation_ui import automation_panel
from triage.digest_ui import digest_panel
from triage.evaluation_ui import evaluation_panel
from triage.jobs_ui import jobs_panel, track_job, job_status
from triage.attachment_ui import attachment_previews

st.set_page_config(page_title="Gmail Triage", page_icon="📬", layout="wide")
st.title("Gmail Triage")
st.caption("Fetch messages, review suggestions, and apply approved labels. Approval saves a local decision; applying changes Gmail.")


def ingestion_panel(store):
    with st.expander("Fetch new emails", expanded=True):
        st.caption("Fetch unprocessed Gmail messages matching your search. AI mode sends bounded message and supported attachment text to OpenAI and uses API credits. Text PDFs and .txt files are supported; attachment-based suggestions require review. Fetching only saves suggestions; it does not change Gmail or send replies.")
        with st.form("fetch-emails"):
            query = st.text_input("Gmail search", value="in:inbox newer_than:7d", key="fetch-query", max_chars=500)
            st.caption('Examples: is:unread · from:someone@example.com · newer_than:2d')
            batch = st.number_input("Batch size", min_value=1, max_value=100, value=5, step=1, key="fetch-limit")
            mode = st.radio("Classification", ["ai", "demo"], format_func=lambda value: "AI (API credits)" if value == "ai" else "Keyword demo (no OpenAI calls)", key="fetch-mode", horizontal=True)
            submitted = st.form_submit_button("Fetch and classify", type="primary")
        if submitted:
            try:
                job = store.fetch(query=query, limit=int(batch), mode=mode)
                track_job(job)
                st.session_state["fetch-job"] = job["id"]
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        if st.session_state.get("fetch-job"):
            job = store.job(st.session_state["fetch-job"])
            job_status(job)
            if job["state"] == "succeeded":
                st.session_state["fetch-result"] = job["result"]
            elif job["state"] in ("failed", "cancelled"):
                st.session_state.pop("fetch-result", None)
        result = st.session_state.get("fetch-result")
        if result:
            if result["processed"]:
                st.success(f"Last fetch saved {result['processed']} suggestion(s). Review them below.")
            else:
                st.info("Last fetch found no new eligible messages. Already processed messages are skipped; try a broader Gmail search if needed.")


def mailbox_actions(store, row):
    """No network requests until the user explicitly clicks an action."""

    row_id = row["id"]
    original_key = f"original-{row_id}"
    with st.expander("Original message"):
        st.caption("Load a text preview and bounded supported attachment text from Gmail without marking it as read. Body text is limited to 12,000 characters. Limits: 5 attachments, 2 MiB each, 4 MiB combined; up to 10 PDF pages and 12,000 total attachment characters.")
        if st.button("Load / refresh original", key=f"load-{row_id}"):
            try:
                st.session_state[original_key] = store.original(row_id)
            except Exception as exc:
                st.error(str(exc) if isinstance(exc, ValueError) else "Could not load the original message. Check Gmail authorization.")
        original = st.session_state.get(original_key)
        if original:
            email = original["email"]
            st.text(f"From: {email.sender}\nSubject: {email.subject}\nDate: {original['date']}")
            if original["reply_to"]:
                st.text(f"Reply-To: {original['reply_to']}")
            st.text("Gmail status at last refresh: " + ("Unread" if original["unread"] else "Read"))
            st.text(email.body or "No readable inline text. Open the message in Gmail to inspect it.")
            attachment_previews(getattr(email, "attachments", ()))
            query = f"rfc822msgid:{original['message_id']}" if original["message_id"] else email.subject
            st.link_button("Open in Gmail", f"https://mail.google.com/mail/u/?authuser={quote(row['account'], safe='')}#search/{quote(query, safe='')}")
    with st.expander("Read status and reply"):
        st.caption("These actions use your existing Gmail write authorization. Read/unread buttons change Gmail immediately.")
        read, unread = st.columns(2)
        try:
            if read.button("Mark as read", key=f"read-{row_id}"):
                st.success(store.read_state(row_id, unread=False))
                if original_key in st.session_state:
                    st.session_state[original_key]["unread"] = False
            if unread.button("Mark as unread", key=f"unread-{row_id}"):
                st.success(store.read_state(row_id, unread=True))
                if original_key in st.session_state:
                    st.session_state[original_key]["unread"] = True
        except Exception as exc:
            st.error(str(exc) if isinstance(exc, ValueError) else "Gmail action failed. Check authorization and retry.")
        reply = store.reply(row_id)
        st.caption("Compose a plain-text reply to the original sender or Reply-To address. This does not reply-all.")
        if not reply or reply["state"] == "draft":
            suggestion = store.suggestion(row_id)
            if suggestion and suggestion["state"] == "ready":
                st.write("Suggested reply from scheduled triage")
                st.text(suggestion["body"])
                st.text("Review notes: " + (suggestion["notes"] or ""))
                if st.button("Use suggested reply in editor", key=f"use-suggestion-{row_id}"):
                    st.session_state[f"body-{row_id}"] = suggestion["body"]
                    st.session_state[f"notes-{row_id}"] = suggestion["notes"]
                    st.session_state.pop(f"draft-evidence-{row_id}", None)
                    st.session_state.pop(f"confirm-{row_id}", None)
                    st.rerun()
            elif suggestion:
                st.caption(suggestion["error"] or "An automatic suggestion is generating or was interrupted. You can still draft manually below.")
            st.write("Draft with AI")
            instruction = st.text_input("What should the reply say?", placeholder="Politely decline and thank them for reaching out.", key=f"instruction-{row_id}", max_chars=2000)
            tone = st.selectbox("Reply tone", TONES, key=f"tone-{row_id}")
            st.caption("Generating sends this message's text preview and your instruction to OpenAI, uses API credits, and replaces the reply editor. It does not send a reply.")
            draft_job_key = f"draft-job-{row_id}"
            draft_job = store.job(st.session_state[draft_job_key]) if st.session_state.get(draft_job_key) else None
            if draft_job:
                job_status(draft_job)
                if draft_job["state"] == "succeeded":
                    draft = draft_job["result"]
                    from types import SimpleNamespace
                    original = draft["original"]
                    original["email"] = SimpleNamespace(**original["email"])
                    st.session_state[original_key] = original
                    st.session_state[f"body-{row_id}"] = draft["body"]
                    st.session_state[f"notes-{row_id}"] = draft["review_notes"]
                    st.session_state[f"draft-evidence-{row_id}"] = draft.get("evidence", [])
                    st.session_state.pop(f"confirm-{row_id}", None)
                    st.session_state.pop(draft_job_key, None)
                elif draft_job["state"] in ("failed", "cancelled"):
                    st.session_state.pop(draft_job_key, None)
            active_draft = bool(draft_job and draft_job["state"] in ("queued", "running"))
            if st.button("Generate AI draft", key=f"generate-{row_id}", disabled=not instruction.strip() or active_draft):
                try:
                    job = store.draft(row_id, instruction, tone)
                    track_job(job)
                    st.session_state[draft_job_key] = job["id"]
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
            if st.session_state.get(f"notes-{row_id}"):
                st.text("AI review notes: " + st.session_state[f"notes-{row_id}"])
            for evidence in st.session_state.get(f"draft-evidence-{row_id}", []):
                st.text(f"Draft evidence — {evidence['field']} {evidence.get('attachment_id') or ''}: {evidence['quote']}")
            if st.session_state.get(f"draft-evidence-{row_id}"):
                st.caption("Source citations from the generated draft; edits still require your review.")
            if f"body-{row_id}" not in st.session_state:
                st.session_state[f"body-{row_id}"] = reply["payload"]["body"] if reply else ""
            body = st.text_area("Reply text", key=f"body-{row_id}", max_chars=20000)
            if st.button("Prepare reply for review", key=f"prepare-{row_id}"):
                try:
                    store.prepare_reply(row_id, body)
                    st.session_state.pop(f"confirm-{row_id}", None)
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc) if isinstance(exc, ValueError) else "Could not prepare reply. Check Gmail authorization.")
            if reply:
                payload = reply["payload"]
                st.text(f"To: {payload['recipient']}\nSubject: {payload['subject']}")
                st.text(payload["body"])
                changed = body != payload["body"]
                if changed:
                    st.info("Prepare the edited reply again to update the send preview.")
                confirmed = st.checkbox("I reviewed this recipient and exact message and want to send it now.", key=f"confirm-{row_id}")
                if st.button("Send reviewed reply", key=f"send-{row_id}", disabled=not confirmed or changed, type="primary"):
                    try:
                        store.send_reply(row_id, expected_message_id=payload["message_id"])
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc) if isinstance(exc, ValueError) else "Send did not complete normally. Check Gmail Sent and the local audit trail before continuing.")
        elif reply["state"] == "sent":
            st.success("Reply sent. This reply cannot be sent again from this card.")
        else:
            st.warning("Reply send is in progress or its outcome is uncertain. Check Gmail Sent before any manual resend. Automatic retry is blocked.")

try:
    store = ApiClient(st.session_state.setdefault("queue-keys", {}))
except ValueError as exc:
    st.error(str(exc))
    st.stop()
try:
    jobs_panel()
    automation_panel(store)
    digest_panel(store)
    evaluation_panel(store)
    ingestion_panel(store)
    summary = store.summary()
    st.caption("Storage: " + ("PostgreSQL" if summary["backend"] == "postgresql" else "SQLite"))
    cols = st.columns(3)
    cols[0].metric("Emails", summary["total"])
    cols[1].metric("Awaiting review", summary["pending"])
    cols[2].metric("Applied", summary["applied"])
    if not summary["total"]:
        st.info("Fetch new emails above, or load sample emails with: python -m triage")
    st.subheader("Review emails")
    st.caption("Prepare any reply edits for review before changing filters or pages so the text is saved locally.")
    search = st.text_input("Search saved emails", placeholder="Subject, sender, account, explanation, or review ID", key="review-search")
    filters = st.columns(3)
    statuses = filters[0].multiselect("Status", ["pending", "approved", "rejected", "applied", "undone"], default=["pending", "approved", "applied"], key="review-status")
    source = filters[1].selectbox("Source", ["All", "gmail", "sample"], key="review-source")
    label_filter = filters[2].selectbox("Label filter", ["All", *LABELS], key="review-label")
    flagged = st.checkbox("Only emails flagged for closer review", key="review-flagged")
    order = st.selectbox("Sort", ["Newest first", "Oldest first", "Needs attention first"], key="review-sort")
    page_size = 10
    result = store.list_emails(status=statuses, source=source, label=label_filter, search=search, flagged=flagged, order=order, page=int(st.session_state.get("review-page", 1)), page_size=page_size)
    pages = result["pages"]
    if st.session_state.get("review-page", 1) > pages:
        st.session_state["review-page"] = 1
    page = st.number_input("Page", min_value=1, max_value=pages, value=1, step=1, key="review-page")
    st.caption(f"{result['total']} of {summary['total']} saved emails match · Page {page} of {pages} · Up to {page_size} cards per page")
    if summary["total"] and not result["total"]:
        st.info("No saved emails match these filters. Clear the search or adjust the filters.")
    for row in result["items"]:
        with st.container(border=True):
            st.subheader(row["subject"])
            st.text(f"#{row['id']} · {row['sender']} · {row['source']} · {row['mode']} · {row['status']}")
            st.caption(f"Suggested label: {row['label']} · Saved: {row['created']} · Account: {row['account']}")
            st.text(row["reason"])
            proposal = store.proposal(row["id"])
            if proposal:
                with st.expander("Agent action proposal"):
                    verdicts = store.verdicts(row["id"])
                    st.caption("Policy preview only. Proposals do not execute actions. Eligibility is checked again by the scheduler; read/unread always needs explicit approval.")
                    st.text(f"Intent: {proposal['plan']['intent']} · Risks: {', '.join(proposal['risk_flags']) or 'none flagged'}")
                    for action, verdict in zip(proposal["plan"]["actions"], verdicts):
                        evidence = action['evidence']
                        st.text(f"{action['kind']}: {verdict['status']} ({verdict['reason_code']})\nEvidence from {evidence['field']} {evidence.get('attachment_id') or ''}: {evidence['quote']}")
                        if action["reply_instruction"]:
                            st.text("Draft instruction: " + action["reply_instruction"])
            if row["needs_review"]:
                st.warning("Classifier flagged this email for closer review.")
            if row["status"] in ("pending", "approved", "rejected"):
                label = st.selectbox("Label", LABELS, index=LABELS.index(row["label"]), key=f"label-{row['id']}")
                left, right = st.columns(2)
                try:
                    if left.button("Approve label", key=f"approve-{row['id']}"):
                        store.review(row["id"], label, approve=True)
                        st.rerun()
                    if right.button("Reject", key=f"reject-{row['id']}"):
                        store.review(row["id"], label, approve=False)
                        st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
            if row["source"] == "gmail" and row["status"] == "approved":
                if st.button("Apply approved label to Gmail", key=f"apply-{row['id']}"):
                    try:
                        store.apply(row["id"])
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc) if isinstance(exc, ValueError) else "Could not apply label. Completed actions are saved; retry the same item to continue.")
            if row["status"] == "applied":
                if st.button("Undo applied label", key=f"undo-{row['id']}"):
                    try:
                        store.undo(row["id"])
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc) if isinstance(exc, ValueError) else "Could not undo label. Check Gmail authorization and retry.")
            with st.expander("Saved attachment evidence"):
                st.caption("Snapshot used at classification time. Older saved emails are not reclassified automatically; load a Gmail original to inspect current attachments or generate a fresh draft.")
                attachment_previews(store.attachments(row["id"]))
            if row["source"] == "gmail":
                mailbox_actions(store, row)
    with st.expander("Local audit trail"):
        audit = store.audit()
        st.dataframe(audit, width="stretch")
except ValueError as exc:
    st.error(str(exc))
finally:
    store.close()
