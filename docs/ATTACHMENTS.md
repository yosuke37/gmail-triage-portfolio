# Attachment-aware review

New Gmail ingestion and explicit original-message/draft requests include bounded text from PDF, DOCX, plain-text, PNG and JPEG attachments. Image-only PDF pages and standalone images use local English Tesseract OCR. DOCX body paragraphs and tables are read directly from bounded, hardened XML parsing. Both AI and keyword-demo classification can use the extracted text. The AI drafting assistant can cite attachment facts in an editable reply. This feature does not send files or replies automatically.

## Try it without Gmail or API credits

With the local Docker stack running:

```powershell
docker compose --env-file deploy/compose.env --profile local-db exec worker python -m triage --input data/attachment_demo.json
```

Refresh [the local dashboard](https://localhost:8443/), select source `sample`, and expand **Saved attachment evidence** and **Agent action proposal**. Five clearly marked synthetic examples show an invoice request, an unreadable scan, a prompt-injection string, a DOCX proposal and OCR-derived image text. These fixtures contain pre-extracted sample text; they are not Gmail messages or measurements of AI quality. Automated parser tests separately generate and process real PDF bytes. Sample rows cannot perform Gmail actions.

For live messages, use **Fetch and classify** with a small batch. AI mode sends the supported extracted text to OpenAI along with the body and uses API credits. No additional OAuth scope is needed beyond the existing Gmail read authorization. Existing processed messages are not automatically reclassified or backfilled; **Load / refresh original** and **Generate AI draft** fetch fresh attachment context for an existing Gmail card.

## Limits and supported formats

| Limit | First version |
|---|---|
| Supported files | `.pdf`, `.docx`, `.txt`, `.png`, `.jpg` / `.jpeg`, with matching PDF/OOXML/plain-text/PNG/JPEG MIME types |
| Text encodings | UTF-8, UTF-8 BOM, UTF-16 BOM |
| Number of files | First 5 named MIME attachments; additional files are marked omitted |
| Download budget | 2 MiB per file, 4 MiB combined declared bytes per email |
| Extracted text | 6,000 characters per file, 12,000 combined per email |
| PDF pages | First 10 pages, potentially fewer if the character limit is reached |
| MIME traversal | 200 parts / 20 levels |
| DOCX archive | At most 500 members, 16 MiB total declared expanded bytes, 4 MiB per parsed XML part, maximum 100:1 expansion ratio |
| OCR | English; at most 3 image-only pages per PDF; 8 seconds per render or OCR call |
| Images | Single-frame PNG/JPEG, at most 12 million input pixels, resized to fit 2,400 × 2,400 pixels for OCR |
| Extraction time | Up to 35 seconds per file; shared 45-second per-email parsing window; downloads are not a strict wall-time guarantee |
| Helper limits | Separate process; on Linux, 20 CPU seconds and 768 MiB address-space cap, inherited by native children |

Unsupported formats (including macro-enabled Word, legacy `.doc`, spreadsheets, general archives, GIF/TIFF/WebP and executable files), encrypted PDFs, malformed data, unknown sizes and download failures show their status. OCR failures or missing native tools are visible as `ocr_failed` / `ocr_unavailable`. Partial previews are marked truncated. DOCX images, headers, notes and complex layout are omitted. Password-protected Office containers are not decrypted. OCR can misread names, dates and amounts; handwriting, rotated scans, non-English text and low-quality images are not reliably supported. Read the original document before making consequential decisions.

PDF extraction first uses native text on each page. Pages with no native text attempt local rendering and OCR, including mixed native/scanned PDFs. A page containing only a small native-text layer is not automatically OCRed; this heuristic can miss text in an image on that page. The first 10 PDF pages are inspected, with OCR attempted on at most 3. Partial native text is preserved when another page cannot be OCRed. Metadata shows the extraction method, OCR attempts, skipped pages and warnings.

Docker installs Tesseract with English language data and Poppler. Native Windows execution needs these tools on PATH for OCR; DOCX/plain-text/native PDF extraction still works without them. OCR runs locally and makes no OpenAI requests or extra OCR-service charges. AI classification/drafting still sends bounded recognized text to OpenAI and uses API credits.

Only files with matching supported filename extensions and MIME types are downloaded. Metadata sizes are checked before download and actual decoded data is checked afterward. Gmail's client receives the attachment response before the post-download check, so this is not an absolute transport-memory guarantee against incorrect provider metadata. Raw bytes are sent over stdin to a separate helper. OCR uses private temporary scratch files with fixed internal names; the parent removes them after processing and terminates its process tree on timeout. Binary attachments are not retained as downloads or database records. Filenames never become filesystem paths. DOCX ZIPs are not extracted onto disk: the reader checks member count, expanded size, compression ratio, duplicate/unsafe paths, encryption, macros and embedded active objects. Only the body XML and content-type manifest are read; DTDs, entities and external XML references are forbidden. Relationships and external links are not fetched. PDF scripts, macros and embedded programs are not executed. Poppler rasterizes selected PDF pages locally for OCR. Linux resource limits apply inside Docker; Windows has time/input/page/text bounds but no OS address-space cap in this implementation. This helper is not a complete malware sandbox or antivirus scanner.

## Evidence, policy and replies

An action citation has a source `field`, exact `quote`, and an `attachment_id` when the field is `attachment`. Validation requires that ID to exist and the quote to occur in that attachment's extracted text inside the same bounded input. Missing IDs, invented quotes and claims based on omitted text fail grounding. Hashes record the downloaded source bytes; the policy fingerprint includes the bounded attachment context.

Every attachment-bearing classification is forced into human review, even when the model reports no risk and an automatic label rule matches. Such messages do not qualify for automatic labels or scheduled unflagged reply suggestions. You can review/apply a label manually and explicitly generate an AI draft. Attachment content never grants permission to pay, send, forward, or execute a tool. Common prompt-injection phrases are checked in attachment text and filenames as well as email fields; this is defense in depth, not proof that arbitrary injection can always be detected.

AI reply output includes typed citations. Citations are checked before returning the draft; a fabricated citation fails the job and preserves the existing editor text. Missing attachment citations cause an explicit review note. Citation validity proves only that the text appeared in the supplied source, not that the attachment is truthful or that every generated statement is supported. Sending keeps the existing exact-body/recipient review and explicit confirmation. User edits are not automatically revalidated against source citations.

## Storage and compatibility

Migration `0005_attachments` adds a bounded `email_attachments` evidence table. New classification snapshots store filenames, statuses, source hashes and extracted text with their email row; binary files are not retained. These snapshots are private mailbox data included in database backups. Draft job results and graph checkpoints can also contain the bounded source text. API detail endpoints are authenticated; email list endpoints do not include attachment previews. The dashboard uses plain text rendering, never HTML from an attachment. Secrets and actual mailbox/attachment data remain excluded from Git and Docker images.

Review decisions are preserved: duplicate ingestion cannot replace a manually reviewed row or its stored snapshot. The workflow/prompt version is now `docx-ocr-v3`, so new classification checkpoints do not silently reuse older extraction plans. Existing stored plans remain readable. The legacy SQLite-to-PostgreSQL importer refuses nonempty attachment evidence rather than silently dropping it; use full backup/restore for such databases.

Back up PostgreSQL before upgrading. Stop API/consumers during schema changes and restart after `python -m triage.db_cli upgrade`; Compose runs its migration service before starting consumers. Older benchmark reports retain their recorded version and do not measure attachment quality.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_attachments -v
$env:TRIAGE_TEST_POSTGRES='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Tests use generated DOCX ZIP/XML files, images, scanned/mixed PDFs, authored text, fake Gmail responses and mocked model output. Real OCR tests run on Linux CI and in Docker; native environments without Tesseract/Poppler explicitly skip those integration cases. They cover extraction bounds, page limits, encrypted/empty PDFs, parser timeout, DOCX expansion/entity/macro rejection, image pixel/frame limits, real OCR, unsupported/oversized files, grounding, injection review gates, persistence, agent input and dashboard previews. They make no live Gmail/OpenAI requests and are not a measured accuracy benchmark for the deployed model.

Primary references: [Gmail attachment retrieval](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments/get), [pypdf extraction limits](https://pypdf.readthedocs.io/en/stable/user/extract-text.html), [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).


Run the actual DOCX/image/scanned-PDF integration smoke checks inside Docker:

```powershell
docker compose --env-file deploy/compose.env --profile local-db exec worker python deploy/ocr_smoke.py
```

These generate synthetic files in memory, call the real local parsers/OCR binaries, verify recognized text, and clean their private scratch files. They do not contact Gmail or OpenAI.

Native tool references: [Tesseract command-line usage](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html), [Pillow image safety](https://pillow.readthedocs.io/en/stable/handbook/security.html), [defusedxml](https://github.com/tiran/defusedxml).
