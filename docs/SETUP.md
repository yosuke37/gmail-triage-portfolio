# Connect OpenAI and Gmail

You can complete all offline checks before creating accounts or credentials. Keep keys and OAuth files on your own computer; do not paste them into chat.

## 1. OpenAI API setup

1. Sign in to the [OpenAI API platform](https://platform.openai.com/).
2. Create/select your API project and configure API billing or available credits.
3. Create an API key using the platform's API keys page.
4. Copy `.env.example` to `.env` in this project, then edit `.env` locally:

```dotenv
OPENAI_API_KEY=your-key-here
OPENAI_MODEL=your-model-id
GOOGLE_CREDENTIALS_PATH=.secrets/credentials.json
TRIAGE_DB_PATH=output/triage.sqlite3
```

Choose a text model available to your API project that supports structured outputs. Keep this configurable rather than hardcoding a model. Your terminal's environment variables take precedence over `.env`. See the [official OpenAI quickstart](https://developers.openai.com/api/docs/quickstart).

Run configuration checks, then classify only synthetic emails first:

```powershell
.\.venv\Scripts\python.exe -m triage doctor
.\.venv\Scripts\python.exe -m triage evaluate --mode ai
.\.venv\Scripts\python.exe -m triage --mode ai
```

`doctor` prints only configuration status, never key values. AI mode sends sender, subject, and up to 12,000 body characters per email to OpenAI and incurs API usage. Framework tracing is disabled. Evaluation does not write review items. Ingestion upgrades pending demo items to AI results, but preserves already reviewed items.

## 2. Gmail OAuth setup

Follow the [Google Gmail Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python) for the current Console screens:

1. Create/select a [Google Cloud project](https://console.cloud.google.com/).
2. Enable the **Gmail API** for that project.
3. Configure the Google Auth Platform branding and audience. For a personal Gmail test, use an external app in Testing and add your Gmail address as a test user.
4. Create an OAuth client of type **Desktop app**. This project uses a localhost callback, not a web-app client or a service account.
5. Download the client JSON and save it as `.secrets/credentials.json` inside this project.
6. Run read-only authorization and finish sign-in in the browser Google opens:

```powershell
.\.venv\Scripts\python.exe -m triage auth
```

Start with a small batch:

```powershell
.\.venv\Scripts\python.exe -m triage --source gmail --mode ai --limit 5 --query "in:inbox newer_than:2d"
```

The read-only token cannot change mailbox labels. Tokens are stored next to your client JSON in separate `token_readonly.json` and `token_modify.json` files. If a token expires or is revoked, rerun the matching authorization command. See [Google's scope documentation](https://developers.google.com/workspace/gmail/api/auth/scopes) for access and verification requirements; public distribution needs additional work beyond this personal prototype.

## 3. Review, apply, undo

Start the local application API first:

```powershell
.\.venv\Scripts\python.exe -m triage.api
```

Keep it running and launch the dashboard in another terminal:

```powershell
.\.venv\Scripts\python.exe -m streamlit run dashboard.py
```

Visit `http://127.0.0.1:8501`. Inspect each explanation, change the suggested label if needed, and approve or reject it. Find the local review ID shown on its card. With write authorization already configured, **Apply approved label to Gmail** and **Undo applied label** perform the respective action directly from the card. These buttons change Gmail immediately; approval by itself still only saves a local review decision.

The dashboard now calls the local API at port 8000 for its operations. The API generates `.secrets/api_token` automatically; the UI reads it server-side, so you do not need to enter another credential. If the UI says the API is unavailable, start it with the command above. See [the architecture guide](ARCHITECTURE.md) for endpoint documentation, authentication, and troubleshooting the service boundary.

### Fetch and review from the dashboard

Use **Fetch new emails** to enter a Gmail search (default `in:inbox newer_than:7d`), select 1–100 new messages, and choose AI classification or the keyword baseline. Click **Fetch and classify** to start. AI mode sends the bounded email text to OpenAI and uses API credits; the baseline does not contact OpenAI. Neither mode changes Gmail. The panel shows progress, success, empty results, or an actionable error. Completed suggestions survive a failure and are skipped on retry. An ingestion lock prevents dashboard/CLI workers from processing the same database concurrently. Fetching does not start merely by opening or refreshing the dashboard.

Under **Review emails**, search saved subjects, senders, accounts, explanations, or local IDs. Combine status, source, label, and closer-review filters. Sort by newest/oldest saved ID or prioritize pending, flagged items. The list shows ten cards per page; source includes both real Gmail and synthetic sample entries. Save reply edits with **Prepare reply for review** before hiding their card with filters or pagination. Original-message previews remain explicitly loaded on demand, with a last-refresh read status; list filtering makes no Gmail or AI calls.

When you want approved changes to reach Gmail, separately authorize write access:

```powershell
.\.venv\Scripts\python.exe -m triage auth --write
.\.venv\Scripts\python.exe -m triage apply --id 6
.\.venv\Scripts\python.exe -m triage undo --id 6
```

Replace `6` with the actual review ID. The apply command only adds a custom label such as `AI Triage/Receipts`. Read/unread and reply actions are separate dashboard actions. Gmail's `gmail.modify` scope is broader than these operations, so keep the token private. Undo removes only the label this action added and preserves labels that were present before the action. It cannot detect another user reapplying the same label later; avoid concurrent edits to the same triage label while applying/undoing. Empty custom label definitions remain after undo.

### Read/unread and replies

On a Gmail card, expand **Read status and reply**. Applied cards are included in the default status filter. **Mark as read** removes only the UNREAD label; **Mark as unread** adds it. Both take effect immediately and write audit events. Use the opposite button to reverse the read-state change. You can also use:

```powershell
.\.venv\Scripts\python.exe -m triage mark-read --id 6
.\.venv\Scripts\python.exe -m triage mark-unread --id 6
```

To reply, type a plain-text message, click **Prepare reply for review**, inspect the recipient, subject, and complete message, then check the confirmation box and click **Send reviewed reply**. Preparing a reply saves it locally; it does not create a Gmail draft or send anything. It replies to the single Reply-To address, or From if absent, without copying CC/BCC recipients. The original subject and thread/reference headers are preserved. The existing write token uses `gmail.modify`, which also permits sending, so no new scope is needed. See [Google's send API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send).

### Original messages and AI reply drafting

Expand **Original message** on a Gmail card and click **Load / refresh original**. This fetches current read status and up to 12,000 characters of inline text without marking the email read. HTML is converted to text; remote images, active HTML, and attachments are not rendered. **Open in Gmail** searches the original message in the appropriate account. The preview is held in the browser session; it can be stale until refreshed, and does not represent the full conversation or attachments.

Under **Read status and reply**, enter a short instruction (for example, "Politely decline and thank them"), choose **Professional**, **Friendly**, or **Concise**, and click **Generate AI draft**. This fetches a fresh original preview and sends bounded sender/subject/body fields plus your instruction to OpenAI using `OPENAI_MODEL`; it uses API credits. The drafting agent has no tools and cannot send or change recipients. It is instructed to ignore commands embedded in email content, avoid invented commitments, and flag missing details for review. These instructions do not guarantee factual accuracy; inspect the output, placeholders, and review notes.

Generation replaces the reply editor and clears previous send confirmation. On an API error, the prior editor text remains. Edit the generated reply, prepare it for review, inspect the recipient and exact text, and explicitly confirm sending as with a manual reply. Generating alone never sends, creates a Gmail draft, or saves the body to the replies table. Click **Prepare reply for review** to persist the text locally; unprepared edits can be lost when the session ends. The audit trail records generation events without the original body or user instruction. Framework tracing remains disabled.

Editing the text requires preparing and reviewing it again. The app supports one reply lifecycle per ingested message. Sending is not automatically retried and cannot be undone by this app. A missing response can mean Gmail sent the reply but the app could not record success; in that case the reply is marked **uncertain** (or **sending** after a process crash), and repeated sends are blocked. Check Gmail Sent before manually composing anything further. Reply text is stored in the local review database, with the same privacy considerations as other local data. Sample cards have no mailbox action controls.

Sample items cannot be applied to Gmail, and the authorized Gmail account must match the ingested item's account. Repeating an apply is idempotent. If Gmail accepted a label change but the response was lost, rerun the same command; saved intent preserves undo information. Do not edit an item with a prepared action; finish its apply, then undo if needed.

## 4. Scheduled ingestion and recovery

### Dedicated services and dashboard schedule controls

Start the worker and scheduler in separate terminals using [the jobs runbook](JOBS.md). Manual fetching, drafts, and digests also require a worker. The Background jobs panel displays availability and durable progress.

#### Suggested replies

In automation settings, choose categories under **Suggest replies for these categories**, set a tone and instruction, then save. Only new, unflagged AI messages from scheduled ingestion qualify; existing saved messages can still use manual **Generate AI draft**. The worker fetches the eligible message's bounded text and calls the drafting agent once. Suggestions are saved separately from prepared replies and cannot be sent by the worker. Existing prepared replies are preserved. On a Gmail card, click **Use suggested reply in editor**, edit it, then use the usual **Prepare reply for review** and explicit send confirmation. A suggestion failure is stored and not automatically retried; you can draft manually from the card instead. Sending/read status remain separate manual actions.

#### Daily digest

Enable **Generate a daily AI digest** and choose its hour/minute in America/New_York, then save and run the worker. At or after that time, the scheduler enqueues a job to save one digest per account/date in the dashboard. If you start it after the selected time, it can generate today's digest immediately. Missed dates are not backfilled automatically. The scheduler checks due digests every few seconds independently of ingestion. A busy queue or laptop sleep can delay execution. Daily digests still work when the scheduled-classification message limit is reached. No digest is emailed or sent to another person.

The **Daily digest** panel supports account/date selection, **Generate daily digest now**, explicit regeneration, and JSON download. Opening the panel alone makes no Gmail or OpenAI calls. Empty scopes produce a local no-data summary without an API call. Nonempty digests send bounded saved sender/subject/label/reason/status metadata to OpenAI, not full message bodies. Counts cover all app-ingested messages on the chosen local date; AI details cover up to 50 recent ingested records plus up to 20 possible outstanding Recruiting/Action needed records. These candidates exclude rejected/undone items and replies this app already sent, but do not prove that a real-world task remains unresolved. Gmail receipt dates, external replies, and whole conversations are not synchronized for this digest.

Digests are snapshots: generating one manually early in the day also fills that date's scheduled slot. Use **Regenerate selected digest** when you want an updated snapshot, which uses additional credits. Unknown message references from the model are rejected. Failed/interrupted attempts do not retry automatically; inspect the error and explicitly clear the attempt before generating again. Generating attempts must be at least three minutes old before they can be cleared. Reply suggestions, digests, and their errors are stored only in the local database and remain private data. Suggested replies add to run token totals; digest request/token totals appear separately in the digest history. Neither is capped by the classification-message limit. All generated content requires your review.

Expand **Scheduled triage and automation**. It starts stopped, with a five-minute interval, five messages per run, a 50-message daily limit, AI mode, and automatic labels disabled. Set the Gmail query and limits, optionally select categories under **Automatically apply these labels**, then click **Save automation settings**. Selecting categories authorizes the worker to approve and apply those matching labels to newly ingested, unflagged AI suggestions while it runs. Existing manual approvals/rejections and flagged emails remain for human handling. Demo mode cannot enable label rules. Replies and read/unread changes remain manual.

Click **Start scheduled triage** to validate configuration, pin the authorized Gmail account, and enable its durable schedule. The dedicated scheduler enqueues the first batch when available; subsequent due times use the configured interval from enqueue time, with no overlapping active classification jobs. Workers continue after the browser closes. Restart the services after reboot: queued work and an enabled schedule persist. Browser refreshes do not start service processes. Background jobs displays service health; automation status displays run history and successful token totals.

Click **Stop scheduled triage** to request a cooperative stop. A Gmail or AI request already in flight can finish and save its result; no next classification or automatic action will begin after the stop is observed. Restart once the worker reports stopped. Saving query/batch/interval changes takes effect on the next cycle; category rules and stop requests are rechecked before applying labels. Changing authorization to a different Gmail account pauses processing until you stop/restart with the intended account. Automatic label changes use the existing write token and the same auditable/undoable apply path as manual actions.

The scheduler pauses after three consecutive failed cycles. Check run history and credentials/network access before starting again. An unresponsive worker is detected from its heartbeat, and cross-process locks prevent a second worker or overlapping ingestion from doing duplicate work. Completed suggestions survive errors. Interrupted automatic label applications can retry idempotently; suggestions saved before an earlier failure but not yet automatically approved remain for manual review.

The daily limit counts messages saved by this scheduler according to America/New_York calendar days, including partial runs. It does not cap manual fetching, failed model requests, or API retries and is not a dollar spending limit. Token totals cover successful responses observed by the classifier, not the OpenAI billing ledger. API credits and platform billing remain the source of actual charges. Framework tracing remains disabled. Settings and run history live in your local review database; durable job state and service heartbeats are stored there too. Worker diagnostics go to its terminal.

### Terminal-only polling alternative

Use either the dedicated queue services or the terminal polling command below for scheduled operation; the CLI does not apply the dashboard's automatic category rules or daily scheduler limit.

Poll for new suggestions every five minutes while this terminal stays open:

```powershell
.\.venv\Scripts\python.exe -m triage --source gmail --mode ai --limit 10 --interval 300 --cycles 0
```

Use Ctrl+C to stop. This command never auto-applies labels. For OS scheduling, use Windows Task Scheduler to run a single ingestion command with this project's `.venv\Scripts\python.exe`, set the working directory to this project, and prevent overlapping instances. No OS task is installed by this project.

Completed messages are deduplicated by source/account/message ID. Gmail pagination skips processed IDs before downloading full message bodies. Only messages matching the query are considered; a time-bounded query can miss old messages after a long outage. Run one ingestion worker per database to avoid duplicate AI calls from overlapping workers. If an error occurs, the process exits and completed items remain saved; rerun it or let your scheduler retry.

LangGraph saves node checkpoints in `output/triage.checkpoints.sqlite3`. After an interrupted run, rerun the same mode/source/query to resume eligible messages. A model call interrupted before its checkpoint commits can be repeated and billed again. The review database is `output/triage.sqlite3`; both paths follow `TRIAGE_DB_PATH`. These local files contain private email metadata, and graph checkpoints contain the bounded email body. They are not encrypted by this application. Restrict access to your OS account and remove the `output` directory when you want to clear local data. Never commit it.

## Troubleshooting

- `doctor` reports a missing key: edit `.env`, not `.env.example`, then rerun.
- Google reports access denied: check the test-user address, enabled Gmail API, and Desktop OAuth client configuration.
- Label apply says not approved: approve the same ID in the dashboard first.
- Account mismatch: authorize the Gmail account that supplied the message.
- Nothing new is processed: existing items are intentionally skipped; inspect them in the dashboard.
- An applied item cannot be re-reviewed: undo it if necessary. The current prototype handles one review/apply lifecycle per message.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m triage evaluate
```

Tests use fake Gmail calls and a mocked Agents SDK runner. They do not use credentials, send email, or incur API charges. The synthetic development evaluation includes ambiguous wording and a prompt injection example. Demo mode is a deliberately limited keyword baseline; use AI evaluation results to measure improvements rather than claiming the baseline is an agent.
