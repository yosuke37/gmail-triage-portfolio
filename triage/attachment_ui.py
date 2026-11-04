"""Text-only attachment previews. Never renders attachment HTML or active files."""
import streamlit as st


def attachment_previews(items):
    if not items:
        st.caption("No attachments were recorded in this preview.")
        return
    st.caption("Extracted text is untrusted source material and may omit layout or pages. OCR can misread names, dates and amounts. Verify important facts in the original document.")
    for item in items:
        st.text(f"{item.get('filename', 'Attachment')} · {item.get('id', '')} · {item.get('status', 'unknown')}")
        if item.get("extraction_method"):
            st.caption("Extraction: " + item["extraction_method"])
        if item.get("ocr_used"):
            st.warning(f"Local English OCR was attempted on {item.get('ocr_pages', 1)} page/image(s). Recognized text may contain errors.")
        for note in item.get("warnings", []):
            st.text(note)
        if item.get("truncated"):
            st.warning("Only part of this attachment was included because an extraction limit was reached.")
        if item.get("status") == "extracted":
            if item.get("page_count") is not None:
                st.caption(f"Pages read: {item['pages_read']} / {item['page_count']}")
            st.text(item.get("text", ""))
        else:
            st.caption("Content was not included. Supported formats: PDF, DOCX, UTF-8/UTF-16 .txt, PNG and JPEG. Encrypted files, unsupported content, OCR/download failures and files over the limits require manual inspection.")
