"""Bounded Gmail attachment extraction. No file execution, OCR, or URL fetching."""
import base64
import hashlib
import json
from pathlib import PurePosixPath
import subprocess
import sys

MAX_FILES = 5
MAX_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_TEXT = 6000
MAX_TOTAL_TEXT = 12000
MAX_PAGES = 10


def supported(filename, mime):
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    return (suffix == ".pdf" and mime == "application/pdf") or (suffix == ".txt" and mime == "text/plain")


def clean_text(text):
    return "".join(c for c in text if c.isprintable() or c in "\n\r\t")


def extract(data, mime):
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
    if not data.startswith(b"%PDF-"):
        return {"status": "unreadable", "text": "", "truncated": False}
    try:
        # The helper parses only supplied bytes. It is a separate, timed process;
        # Linux additionally applies CPU and address-space limits before parsing.
        result = subprocess.run([sys.executable, "-m", "triage.attachment_parser"], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        if result.returncode or len(result.stdout) > MAX_TEXT * 8:
            raise ValueError("Extraction failed")
        output = json.loads(result.stdout)
        if output["status"] not in ("extracted", "encrypted", "no_text", "unreadable"):
            raise ValueError("Invalid extraction result")
        output["text"] = clean_text(output.get("text", ""))[:MAX_TEXT]
        return output
    except (subprocess.TimeoutExpired, ValueError, KeyError, OSError):
        return {"status": "unreadable", "text": "", "truncated": False}


def read_attachments(payload, download):
    """Traverse bounded MIME structure. Download only supported, sized files."""
    output, stack = [], [(payload, 0)]
    consumed, characters, visited = 0, 0, 0
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
        if characters >= MAX_TOTAL_TEXT:
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
        item.update(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), **extract(data, mime))
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
        for key in ("size_bytes", "page_count", "pages_read"):
            if isinstance(raw.get(key), int) and raw[key] >= 0:
                item[key] = min(raw[key], 10 ** 9)
        digest = str(raw.get("sha256", ""))
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            item["sha256"] = digest
        text = str(raw.get("text", ""))[:min(MAX_TEXT, remaining)] if raw.get("status") == "extracted" else ""
        item["text"] = text
        item["truncated"] = bool(raw.get("truncated") or len(str(raw.get("text", ""))) > len(text))
        remaining -= len(text)
        output.append(item)
    return output


def source_fields(email):
    return {"sender": email.sender[:1000], "subject": email.subject[:1000], "body": email.body[:12000], "attachments": attachment_context(email)}
