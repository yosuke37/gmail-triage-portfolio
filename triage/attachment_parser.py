"""Timed DOCX/PDF/image extraction helper; scratch files belong to its parent."""
import argparse
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from triage.attachments import DOCX_MIME, MAX_BYTES, MAX_PAGES, MAX_TEXT, clean_text

MAX_OCR_PAGES = 3


def parse(data, *, scratch=None, deadline=None):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted:
        return {"status": "encrypted", "text": "", "truncated": False}
    count, chunks, used, read = len(reader.pages), [], 0, 0
    ocr_pages, skipped, notes, methods = 0, 0, [], set()
    failed_status = None
    deadline = deadline or time.monotonic() + 30
    for index in range(min(count, MAX_PAGES)):
        if time.monotonic() >= deadline:
            notes.append("Document time limit reached; remaining pages were omitted.")
            break
        page = reader.pages[index]
        contents = page.get_contents()
        if contents is not None and len(contents.get_data()) > 2 * 1024 * 1024:
            return {"status": "unreadable", "text": "", "truncated": True}
        text = clean_text(page.extract_text() or "").strip()
        read += 1
        if text:
            methods.add("pdf_text")
        elif ocr_pages >= MAX_OCR_PAGES:
            skipped += 1
            notes.append("OCR page limit reached; some image-only pages were not read.")
        elif scratch is None or not shutil.which("pdftoppm") or not shutil.which("tesseract"):
            skipped += 1
            failed_status = "ocr_unavailable"
            notes.append("PDF OCR requires local Poppler and Tesseract with English language data.")
        else:
            from triage.local_ocr import ocr_image
            source = Path(scratch) / "source.pdf"
            if not source.exists():
                source.write_bytes(data)
            target = Path(scratch) / "rendered"
            target.with_suffix(".png").unlink(missing_ok=True)
            ocr_pages += 1
            try:
                remaining = deadline - time.monotonic()
                if remaining <= .5:
                    raise subprocess.TimeoutExpired("renderer", 0)
                result = subprocess.run(["pdftoppm", "-f", str(index+1), "-l", str(index+1), "-singlefile", "-scale-to", "2400", "-r", "180", "-png", "-hide-annotations", str(source), str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=min(8, remaining))
                image = target.with_suffix(".png")
                if result.returncode or not image.is_file() or image.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError("Rendering failed")
                output = ocr_image(image.read_bytes(), scratch, deadline, expected_format="PNG")
                notes.extend(output.get("warnings", []))
                methods.add("tesseract")
                if output["status"] not in ("extracted", "no_text"):
                    failed_status = output["status"]
                    skipped += 1
                text = output.get("text", "")
                if output.get("truncated"):
                    notes.append("OCR output is incomplete.")
            except (subprocess.TimeoutExpired, ValueError, OSError):
                skipped += 1
                failed_status = "ocr_failed"
                notes.append("A PDF page could not be rendered/OCRed within its limits.")
        if text:
            chunks.append(text)
            used += len(text) + 1
        if used > MAX_TEXT:
            break
    text = "\n".join(chunks).strip()
    truncated = count > read or len(text) > MAX_TEXT or skipped > 0 or "OCR output is incomplete." in notes
    return {"status": "extracted" if text else failed_status or "no_text", "text": text[:MAX_TEXT],
        "truncated": truncated, "page_count": count, "pages_read": read,
        "ocr_used": ocr_pages > 0, "ocr_pages": ocr_pages, "ocr_skipped_pages": skipped,
        "ocr_language": "eng" if ocr_pages else "", "extraction_method": "+".join(sorted(methods)) or "pdf_text",
        "warnings": list(dict.fromkeys(notes))[:5]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mime", default="application/pdf")
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--budget", type=float, default=30)
    args = parser.parse_args()
    if sys.platform != "win32":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    try:
        if len(data) > MAX_BYTES:
            raise ValueError("File too large")
        deadline = time.monotonic() + min(30, max(.5, args.budget))
        if args.mime == "application/pdf":
            output = parse(data, scratch=args.work_dir, deadline=deadline)
        elif args.mime == DOCX_MIME:
            from triage.docx_extract import parse_docx
            output = parse_docx(data)
        elif args.mime in ("image/png", "image/jpeg"):
            from triage.local_ocr import ocr_image
            output = ocr_image(data, args.work_dir, deadline, expected_format="PNG" if args.mime == "image/png" else "JPEG")
        else:
            raise ValueError("Unsupported MIME")
    except Exception:
        output = {"status": "unreadable", "text": "", "truncated": False}
    sys.stdout.write(json.dumps(output))


if __name__ == "__main__":
    main()
