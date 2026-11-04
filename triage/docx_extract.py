"""Read limited DOCX body XML without extracting an archive or following relationships."""
import io
from pathlib import PurePosixPath
import zipfile

from defusedxml.ElementTree import fromstring

from triage.attachments import MAX_TEXT, clean_text

MAX_EXPANDED = 16 * 1024 * 1024
MAX_XML = 4 * 1024 * 1024
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def parse_docx(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 500 or sum(e.file_size for e in entries) > MAX_EXPANDED:
            return {"status": "limit_reached", "text": "", "truncated": True}
        names = set()
        for entry in entries:
            name = entry.filename.replace("\\", "/")
            if name in names or name.startswith("/") or ".." in PurePosixPath(name).parts:
                raise ValueError("Unsafe or duplicate archive member")
            names.add(name)
            if entry.flag_bits & 1:
                return {"status": "encrypted", "text": "", "truncated": False}
            if entry.file_size / max(1, entry.compress_size) > 100:
                return {"status": "limit_reached", "text": "", "truncated": True}
            if "vbaproject" in name.lower() or name.lower().startswith(("word/activex/", "word/embeddings/")):
                return {"status": "unsupported", "text": "", "truncated": False, "warnings": ["Macros or embedded active content are not supported."]}
        def xml(name):
            info = archive.getinfo(name)
            if info.file_size > MAX_XML:
                raise ValueError("XML part exceeds extraction limit")
            with archive.open(info) as stream:
                raw = stream.read(MAX_XML + 1)
            if len(raw) > MAX_XML:
                raise ValueError("XML part exceeds extraction limit")
            return fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        types = xml("[Content_Types].xml")
        values = [node.attrib.get("ContentType", "") for node in types]
        if any("macroenabled" in value.lower() for value in values):
            return {"status": "unsupported", "text": "", "truncated": False}
        if "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml" not in values:
            raise ValueError("Not a DOCX document")
        root = xml("word/document.xml")
        body = root.find(W + "body")
        if body is None:
            raise ValueError("Missing document body")
        chunks, size, count, truncated = [], 0, 0, False
        def paragraph(node):
            return "".join((child.text or "") if child.tag == W + "t" else "\t" if child.tag == W + "tab" else "\n" if child.tag in (W + "br", W + "cr") else "" for child in node.iter())
        for node in body:
            if node.tag == W + "p":
                lines = [paragraph(node)]
            elif node.tag == W + "tbl":
                lines = [" | ".join(" ".join(paragraph(p) for p in cell.iter(W + "p")) for cell in row.findall(W + "tc")) for row in list(node.findall(W + "tr"))[:1001]]
            else:
                continue
            for line in lines:
                text = clean_text(line).strip()
                chunks.append(text[:MAX_TEXT + 1])
                size += len(text) + 1
                count += 1
                if size > MAX_TEXT or count >= 1000:
                    truncated = True
                    break
            if truncated:
                break
        text = "\n".join(chunks).strip()
        return {"status": "extracted" if text else "no_text", "text": text[:MAX_TEXT], "truncated": truncated or len(text) > MAX_TEXT,
            "extraction_method": "docx_body", "warnings": ["DOCX preview includes body paragraphs and tables. Images, headers, notes and complex layout are omitted; verify the original document."]}
