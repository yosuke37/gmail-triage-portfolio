"""Bounded attachment extraction and local OCR. No document code or URL execution."""
import base64
import hashlib
import json
import os
from pathlib import PurePosixPath
import subprocess
import sys
import tempfile
import time
import signal

MAX_FILES = 5
MAX_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_TEXT = 6000
MAX_TOTAL_TEXT = 12000
MAX_PAGES = 10
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PARSE_SECONDS = 35
EMAIL_PARSE_SECONDS = 45


def supported(filename, mime):
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    return (suffix == ".pdf" and mime == "application/pdf") or (suffix == ".txt" and mime == "text/plain") or (suffix == ".docx" and mime == DOCX_MIME) or (suffix == ".png" and mime == "image/png") or (suffix in (".jpg", ".jpeg") and mime == "image/jpeg")


def clean_text(text):
    return "".join(c for c in text if c.isprintable() or c in "\n\r\t")


def _run_parser(data, mime, timeout):
    with tempfile.TemporaryDirectory(prefix="triage-extract-") as scratch:
        command = [sys.executable, "-m", "triage.attachment_parser", "--mime", mime, "--work-dir", scratch, "--budget", str(max(.5, timeout - 2))]
        env = {key: value for key, value in os.environ.items() if not key.startswith(("OPENAI_", "GOOGLE_", "TRIAGE_", "PG"))}
        env.update(OMP_THREAD_LIMIT="1", OMP_NUM_THREADS="1")
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, start_new_session=sys.platform != "win32", creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        try:
            stdout, _ = process.communicate(data, timeout=timeout)
            if process.returncode:
                raise ValueError("Extraction failed")
            return stdout
        finally:
            if sys.platform != "win32":
                # Also kill any native renderer/OCR descendant after a timeout
                # or helper failure, before removing the private scratch dir.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                process.kill()
            process.communicate()


def extract(data, mime, *, timeout=PARSE_SECONDS):
    if len(data) > MAX_BYTES:
        return {"status": "too_large", "text": "", "truncated": True}
    if mime == "text/plain":
        try:
            encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            text = data.decode(encoding)
            if "\x00" in text:
                return {"status": "unreadable", "text": "", "truncated": False}
            text = clean_text(text).strip()
            return {"status": "extracted" if text else "no_text", "text": text[:MAX_TEXT], "truncated": len(text) > MAX_TEXT}
        except UnicodeError:
            return {"status": "unreadable", "text": "", "truncated": False}
    signatures = {"application/pdf": b"%PDF-", DOCX_MIME: b"PK", "image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8"}
    if mime not in signatures or not data.startswith(signatures[mime]):
        return {"status": "unreadable", "text": "", "truncated": False}
    try:
        result = _run_parser(data, mime, timeout)
        if len(result) > MAX_TEXT * 12 + 8000:
            raise ValueError("Extraction failed")
        output = json.loads(result)
        if output["status"] not in ("extracted", "encrypted", "no_text", "unreadable", "unsupported", "limit_reached", "ocr_unavailable", "ocr_failed"):
            raise ValueError("Invalid extraction result")
        output["text"] = clean_text(output.get("text", ""))[:MAX_TEXT]
        return output
    except (subprocess.TimeoutExpired, ValueError, KeyError, OSError):
        return {"status": "unreadable", "text": "", "truncated": False}


def read_attachments(payload, download):
    """Traverse bounded MIME structure. Download only supported, sized files."""
    output, stack = [], [(payload, 0)]
    consumed, characters, visited = 0, 0, 0
    deadline = time.monotonic() + EMAIL_PARSE_SECONDS
    while stack:
        part, depth = stack.pop()
        visited += 1
        if visited > 200 or depth > 20:
            output.append({"id": "limit", "filename": "Additional MIME parts", "mime_type": "", "status": "limit_reached", "text": "", "truncated": True})
            break
        filename = part.get("filename", "")
        if not filename:
            children = part.get("parts", [])
            stack.extend((child, depth + 1) for child in reversed(children[:201]))
            continue
        if len(output) >= MAX_FILES:
            output.append({"id": "limit", "filename": "Additional attachments", "mime_type": "", "status": "limit_reached", "text": "", "truncated": True})
            break
        mime = part.get("mimeType", "").lower()
        body = part.get("body", {})
        item = {"id": f"attachment-{len(output)+1}", "filename": clean_text(str(filename))[:200],
            "mime_type": mime[:100], "status": "unsupported", "text": "", "truncated": False}
        output.append(item)
        if not supported(filename, mime):
            continue
        size = body.get("size")
        if not isinstance(size, int) or size < 0 or size > MAX_BYTES or consumed + size > MAX_TOTAL_BYTES:
            item["status"] = "too_large" if isinstance(size, int) else "unknown_size"
            continue
        if characters >= MAX_TOTAL_TEXT or time.monotonic() >= deadline:
            item.update(status="limit_reached", truncated=True)
            continue
        # Charge the declared size even if download/parsing fails.
        consumed += size
        try:
            encoded = body.get("data")
            if encoded is None and body.get("attachmentId"):
                encoded = download(body["attachmentId"])
            if not isinstance(encoded, str) or len(encoded) > (MAX_BYTES + 2) // 3 * 4 + 4:
                item["status"] = "too_large" if isinstance(encoded, str) else "download_failed"
                continue
            data = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            if len(data) > size or len(data) > MAX_BYTES:
                item["status"] = "size_mismatch"
                continue
        except Exception:
            item["status"] = "download_failed"
            continue
        item.update(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), **extract(data, mime, timeout=max(1, min(PARSE_SECONDS, deadline - time.monotonic()))))
        remaining = MAX_TOTAL_TEXT - characters
        if len(item["text"]) > remaining:
            item["text"], item["truncated"] = item["text"][:remaining], True
        characters += len(item["text"])
    return tuple(output)


def attachment_context(email):
    """One canonical bounded snapshot for model input, evidence and storage."""
    remaining, output = MAX_TOTAL_TEXT, []
    for index, raw in enumerate(email.attachments[:MAX_FILES + 1]):
        if index == MAX_FILES:
            raw = {"id": "limit", "filename": "Additional attachments", "status": "limit_reached", "truncated": True}
        if not isinstance(raw, dict):
            raw = {"id": f"attachment-{index+1}", "filename": "Unreadable attachment", "status": "unreadable"}
        item = {"id": str(raw.get("id", f"attachment-{index+1}"))[:100],
            "filename": clean_text(str(raw.get("filename", "Attachment")))[:200],
            "mime_type": str(raw.get("mime_type", ""))[:100], "status": str(raw.get("status", "unreadable"))[:30]}
        for key in ("size_bytes", "page_count", "pages_read", "ocr_pages", "ocr_skipped_pages"):
            if isinstance(raw.get(key), int) and raw[key] >= 0:
                item[key] = min(raw[key], 10 ** 9)
        digest = str(raw.get("sha256", ""))
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            item["sha256"] = digest
        item["ocr_used"] = bool(raw.get("ocr_used", False))
        for key in ("extraction_method", "ocr_language"):
            if key in raw:
                item[key] = str(raw[key])[:80]
        item["warnings"] = [clean_text(str(w))[:250] for w in raw.get("warnings", [])[:5]]
        text = str(raw.get("text", ""))[:min(MAX_TEXT, remaining)] if raw.get("status") == "extracted" else ""
        item["text"] = text
        item["truncated"] = bool(raw.get("truncated") or len(str(raw.get("text", ""))) > len(text))
        remaining -= len(text)
        output.append(item)
    return output


def source_fields(email):
    return {"sender": email.sender[:1000], "subject": email.subject[:1000], "body": email.body[:12000], "attachments": attachment_context(email)}
