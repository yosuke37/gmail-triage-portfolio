"""PDF-only subprocess helper. Never renders or executes embedded PDF content."""
import io
import json
import sys


def parse(data):
    from pypdf import PdfReader
    from triage.attachments import MAX_PAGES, MAX_TEXT, clean_text
    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted:
        return {"status": "encrypted", "text": "", "truncated": False}
    count, chunks, used, read = len(reader.pages), [], 0, 0
    for index in range(min(count, MAX_PAGES)):
        page = reader.pages[index]
        contents = page.get_contents()
        if contents is not None and len(contents.get_data()) > 2 * 1024 * 1024:
            return {"status": "unreadable", "text": "", "truncated": True}
        text = clean_text(page.extract_text() or "").strip()
        chunks.append(text)
        used += len(text) + 1
        read += 1
        if used > MAX_TEXT:
            break
    text = "\n".join(chunks).strip()
    return {"status": "extracted" if text else "no_text", "text": text[:MAX_TEXT],
        "truncated": count > read or len(text) > MAX_TEXT, "page_count": count, "pages_read": read}


def main():
    if sys.platform != "win32":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    from triage.attachments import MAX_BYTES
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    try:
        if len(data) > MAX_BYTES:
            raise ValueError("File too large")
        output = parse(data)
    except Exception:
        output = {"status": "unreadable", "text": "", "truncated": False}
    sys.stdout.write(json.dumps(output))


if __name__ == "__main__":
    main()
