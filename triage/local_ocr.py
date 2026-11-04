"""Bounded local image decoding and Tesseract invocation. No network services."""
import io
import os
from pathlib import Path
import shutil
import subprocess
import time
import warnings

from PIL import Image, ImageOps

from triage.attachments import MAX_TEXT, clean_text

MAX_PIXELS = 12_000_000
MAX_SIDE = 2400


def ocr_image(data, scratch, deadline, *, expected_format=None):
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as original:
            if original.format not in ("PNG", "JPEG") or (expected_format and original.format != expected_format):
                raise ValueError("Image format mismatch")
            if original.width * original.height > MAX_PIXELS or getattr(original, "n_frames", 1) != 1:
                return {"status": "limit_reached", "text": "", "truncated": True}
            image = ImageOps.exif_transpose(original)
            if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                image = Image.alpha_composite(Image.new("RGBA", image.size, "white"), image.convert("RGBA"))
            image = image.convert("RGB")
            image.thumbnail((MAX_SIDE, MAX_SIDE))
            target = Path(scratch) / "ocr-input.png"
            image.save(target, format="PNG")
    if not shutil.which("tesseract"):
        return {"status": "ocr_unavailable", "text": "", "truncated": False, "warnings": ["Local Tesseract OCR is unavailable. Use the Docker image or install Tesseract with English language data."]}
    remaining = deadline - time.monotonic()
    if remaining <= .5:
        return {"status": "limit_reached", "text": "", "truncated": True}
    try:
        result = subprocess.run(["tesseract", str(target), "stdout", "-l", "eng", "--psm", "3"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=min(8, remaining), env={**os.environ, "OMP_THREAD_LIMIT": "1", "OMP_NUM_THREADS": "1"})
        if result.returncode:
            raise ValueError("OCR failed")
        text = clean_text(result.stdout.decode("utf-8", errors="replace")).strip()
        return {"status": "extracted" if text else "no_text", "text": text[:MAX_TEXT], "truncated": len(text) > MAX_TEXT,
            "ocr_used": True, "ocr_pages": 1, "ocr_language": "eng", "extraction_method": "tesseract",
            "warnings": ["OCR can misread names, amounts, dates and punctuation. Verify the original before deciding or replying."]}
    except (subprocess.TimeoutExpired, ValueError):
        return {"status": "ocr_failed", "text": "", "truncated": True, "ocr_used": True, "warnings": ["OCR did not finish successfully within its limits."]}
