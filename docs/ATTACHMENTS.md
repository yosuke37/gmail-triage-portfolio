# Attachment-aware review

New Gmail ingestion and explicit original-message/draft requests now include bounded text from supported attachments. Both AI and keyword-demo classification can use the extracted text. The AI drafting assistant can cite attachment facts in an editable reply. This feature does not send files or replies automatically.

## Try it without Gmail or API credits

With the local Docker stack running:

```powershell
docker compose --env-file deploy/compose.env --profile local-db exec worker python -m triage --input data/attachment_demo.json
```

Refresh [the local dashboard](https://localhost:8443/), select source `sample`, and expand **Saved attachment evidence** and **Agent action proposal**. Three clearly marked synthetic examples show an invoice request, an unreadable scan, and an attachment with a prompt-injection string. These fixtures contain pre-extracted sample text; they are not Gmail messages or measurements of AI quality. Automated parser tests separately generate and process real PDF bytes. Sample rows cannot perform Gmail actions.

For live messages, use **Fetch and classify** with a small batch. AI mode sends the supported extracted text to OpenAI along with the body and uses API credits. No additional OAuth scope is needed beyond the existing Gmail read authorization. Existing processed messages are not automatically reclassified or backfilled; **Load / refresh original** and **Generate AI draft** fetch fresh attachment context for an existing Gmail card.

## Limits and supported formats

| Limit | First version |
|---|---|
| Supported files | `.pdf` with `application/pdf`; `.txt` with `text/plain` |
| Text encodings | UTF-8, UTF-8 BOM, UTF-16 BOM |
| Number of files | First 5 named MIME attachments; additional files are marked omitted |
| Download budget | 2 MiB per file, 4 MiB combined declared bytes per email |
| Extracted text | 6,000 characters per file, 12,000 combined per email |
| PDF pages | First 10 pages, potentially fewer if the character limit is reached |
| MIME traversal | 200 parts / 20 levels |
| PDF parser | Separate Python process; 10-second timeout; on Linux, 5 CPU seconds and 384 MiB address-space cap |

Unsupported formats (including DOCX, spreadsheets, archives, executable files and images), encrypted PDFs, scanned PDFs without usable text, malformed data, unknown sizes and download failures are shown with their status. They do not supply evidence text. Partial previews are marked truncated. Extraction may lose layout, tables, or text embedded in images; OCR is not included. Read the original document before making consequential decisions.

Only files with matching supported filename extensions and MIME types are downloaded. Metadata sizes are checked before download and actual decoded data is checked afterward. Gmail's client receives the attachment response before the post-download check, so this is not an absolute transport-memory guarantee against incorrect provider metadata. Raw files are held in memory, passed over stdin for PDF parsing, and not saved as downloads. Filenames never become filesystem paths. PDF scripts, macros, links and embedded programs are not executed or opened. No external attachment URLs are followed. Linux resource limits apply inside Docker; Windows has the timeout and input/page/text bounds but no OS address-space cap in this implementation. This helper is not a complete malware sandbox or antivirus scanner.

## Evidence, policy and replies

An action citation has a source `field`, exact `quote`, and an `attachment_id` when the field is `attachment`. Validation requires that ID to exist and the quote to occur in that attachment's extracted text inside the same bounded input. Missing IDs, invented quotes and claims based on omitted text fail grounding. Hashes record the downloaded source bytes; the policy fingerprint includes the bounded attachment context.

Every attachment-bearing classification is forced into human review, even when the model reports no risk and an automatic label rule matches. Such messages do not qualify for automatic labels or scheduled unflagged reply suggestions. You can review/apply a label manually and explicitly generate an AI draft. Attachment content never grants permission to pay, send, forward, or execute a tool. Common prompt-injection phrases are checked in attachment text and filenames as well as email fields; this is defense in depth, not proof that arbitrary injection can always be detected.

AI reply output includes typed citations. Citations are checked before returning the draft; a fabricated citation fails the job and preserves the existing editor text. Missing attachment citations cause an explicit review note. Citation validity proves only that the text appeared in the supplied source, not that the attachment is truthful or that every generated statement is supported. Sending keeps the existing exact-body/recipient review and explicit confirmation. User edits are not automatically revalidated against source citations.

## Storage and compatibility

Migration `0005_attachments` adds a bounded `email_attachments` evidence table. New classification snapshots store filenames, statuses, source hashes and extracted text with their email row; binary files are not retained. These snapshots are private mailbox data included in database backups. Draft job results and graph checkpoints can also contain the bounded source text. API detail endpoints are authenticated; email list endpoints do not include attachment previews. The dashboard uses plain text rendering, never HTML from an attachment. Secrets and actual mailbox/attachment data remain excluded from Git and Docker images.

Review decisions are preserved: duplicate ingestion cannot replace a manually reviewed row or its stored snapshot. The workflow/prompt version was bumped so new classification checkpoints do not silently reuse pre-attachment plans. Existing stored plans remain readable. The legacy SQLite-to-PostgreSQL importer refuses nonempty attachment evidence rather than silently dropping it; use full backup/restore for such databases.

Back up PostgreSQL before upgrading. Stop API/consumers during schema changes and restart after `python -m triage.db_cli upgrade`; Compose runs its migration service before starting consumers. Older benchmark reports retain their recorded version and do not measure attachment quality.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_attachments -v
$env:TRIAGE_TEST_POSTGRES='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Tests use generated PDFs, authored text, fake Gmail responses and mocked model output. They cover extraction bounds, page limits, encrypted/empty PDFs, parser timeout, unsupported/oversized files, grounding, injection review gates, persistence, agent input and dashboard previews. They make no live Gmail/OpenAI requests and are not a measured accuracy benchmark for the deployed model.

Primary references: [Gmail attachment retrieval](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments/get), [pypdf extraction limits](https://pypdf.readthedocs.io/en/stable/user/extract-text.html), [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
