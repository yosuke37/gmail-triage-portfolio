"""Text-only attachment previews. Never renders attachment HTML or active files."""
import streamlit as st


def attachment_previews(items):
    if not items:
        st.caption("No attachments were recorded in this preview.")
        return
    st.caption("Extracted text is untrusted source material and may omit layout, tables, images, or pages. Verify important facts in the original document.")
    for item in items:
        st.text(f"{item.get('filename', 'Attachment')} · {item.get('id', '')} · {item.get('status', 'unknown')}")
        if item.get("truncated"):
            st.warning("Only part of this attachment was included because an extraction limit was reached.")
        if item.get("status") == "extracted":
            if item.get("page_count") is not None:
                st.caption(f"Pages read: {item['pages_read']} / {item['page_count']}")
            st.text(item.get("text", ""))
        else:
            st.caption("Content was not included. Supported formats: text PDFs and UTF-8/UTF-16 .txt files. Encrypted/scanned documents, unsupported formats, failed downloads, and files over the limits require manual inspection.")
