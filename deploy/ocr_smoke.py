"""Real DOCX/image/scanned-PDF checks using generated synthetic files only."""
import io
import json
from pathlib import Path
import zipfile

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter

from triage.attachments import DOCX_MIME, extract


def document_bytes(xml=None, *, extra=None, compression=zipfile.ZIP_DEFLATED):
    xml = xml or '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Please review the proposal.</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Total</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>USD 125.00</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=compression) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("word/document.xml", xml)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return target.getvalue()


def scan_image(text="PLEASE REVIEW INVOICE\nTOTAL USD 125.00\nREFERENCE DEMO 125", *, ink="black"):
    image = Image.new("RGB", (1500, 500), "white")
    fonts = [Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("C:/Windows/Fonts/arial.ttf")]
    font_path = next((path for path in fonts if path.is_file()), None)
    font = ImageFont.truetype(str(font_path), 46) if font_path else ImageFont.load_default(size=46)
    ImageDraw.Draw(image).multiline_text((70, 80), text, font=font, fill=ink, spacing=28)
    return image


def image_bytes(fmt="PNG"):
    target = io.BytesIO()
    scan_image().save(target, format=fmt)
    return target.getvalue()


def scanned_pdf():
    target = io.BytesIO()
    scan_image().save(target, format="PDF", resolution=150)
    return target.getvalue()


def run():
    results = {}
    for name, mime, data in (("docx", DOCX_MIME, document_bytes()), ("png", "image/png", image_bytes()),
        ("jpeg", "image/jpeg", image_bytes("JPEG")), ("scanned_pdf", "application/pdf", scanned_pdf())):
        output = extract(data, mime)
        assert output["status"] == "extracted", f"{name}: {output['status']}"
        assert "125" in output["text"] and "review" in output["text"].casefold(), f"{name}: required synthetic content not recognized"
        if name != "docx":
            assert output["ocr_used"], f"{name}: OCR was not used"
        results[name] = {key: output.get(key) for key in ("status", "extraction_method", "ocr_used", "ocr_pages")}
    return results


if __name__ == "__main__":
    print(json.dumps({"synthetic_only": True, "provider_calls": 0, "checks": run()}))
