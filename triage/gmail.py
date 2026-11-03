"""Gmail OAuth, labels, read state, and explicitly reviewed replies."""
import base64
from email.message import Message
from email.message import EmailMessage
from email.headerregistry import Address
from email.utils import getaddresses
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import os
import tempfile

from triage.core import Email, LABELS

READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def connect(credentials_path: Path, *, writable=False, authorize=False):
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scope = WRITE_SCOPE if writable else READ_SCOPE
    token_path = Path(os.getenv("GOOGLE_TOKEN_DIR", str(credentials_path.parent))) / ("token_modify.json" if writable else "token_readonly.json")
    mounted = os.getenv("GOOGLE_WRITE_TOKEN_FILE" if writable else "GOOGLE_READ_TOKEN_FILE")
    source = token_path if token_path.exists() else Path(mounted) if mounted else token_path
    creds = Credentials.from_authorized_user_file(str(source), [scope]) if source.exists() and source.stat().st_size else None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            if authorize:
                creds = None
            else:
                raise ValueError("Gmail authorization expired or was revoked. Rerun 'python -m triage auth' (add --write for label changes).") from None
    if not creds or not creds.valid:
        if not authorize:
            raise ValueError("Gmail is not authorized. Run 'python -m triage auth' first (add --write for label changes).")
        if not credentials_path.exists():
            raise ValueError("Save your Google Desktop OAuth client JSON to .secrets/credentials.json first.")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), [scope])
        creds = flow.run_local_server(host="localhost", port=0, timeout_seconds=180)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=token_path.parent, prefix=".oauth-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(creds.to_json())
        os.replace(temporary, token_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return Gmail(build("gmail", "v1", credentials=creds, cache_discovery=False), writable=writable)


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def body_text(payload: dict) -> str:
    """Prefer plain text, skip attachments, and bound content sent to the model."""
    if payload.get("filename") or payload.get("body", {}).get("attachmentId"):
        return ""
    parts = payload.get("parts", [])
    if parts:
        plain = [p for p in parts if p.get("mimeType") == "text/plain" and not p.get("filename") and not p.get("body", {}).get("attachmentId")]
        return "\n".join(text for p in (plain or parts) if (text := body_text(p)))[:12000]
    if payload.get("mimeType", "text/plain") not in ("text/plain", "text/html"):
        return ""
    data = payload.get("body", {}).get("data", "")
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    header = Message()
    content_type = next((h["value"] for h in payload.get("headers", []) if h["name"].lower() == "content-type"), "text/plain")
    header["content-type"] = content_type
    try:
        text = raw.decode(header.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    if payload.get("mimeType") == "text/html":
        parser = TextExtractor()
        parser.feed(text)
        text = unescape(" ".join(parser.text))
    return text[:12000]


class Gmail:
    def __init__(self, service, *, writable=False):
        self.service = service
        self.writable = writable
        self.account = service.users().getProfile(userId="me").execute()["emailAddress"].lower()

    def email_preview(self, message):
        from triage.attachments import read_attachments
        payload = message.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        def download(attachment_id):
            return self.service.users().messages().attachments().get(userId="me", messageId=message["id"], id=attachment_id).execute(num_retries=0).get("data")
        attachments = read_attachments(payload, download)
        return Email(message["id"], headers.get("from", ""), headers.get("subject", "(no subject)"), body_text(payload), attachments)

    def original(self, email_id: str):
        """Read a text preview and current status without changing UNREAD."""
        message = self.service.users().messages().get(userId="me", id=email_id, format="full").execute()
        payload = message.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        return {
            "email": self.email_preview(message),
            "unread": "UNREAD" in message.get("labelIds", []),
            "date": headers.get("date", ""),
            "reply_to": headers.get("reply-to", ""),
            "message_id": headers.get("message-id", ""),
        }

    def fetch(self, query: str, limit: int, exclude=None):
        if not 1 <= limit <= 100:
            raise ValueError("Batch size must be between 1 and 100.")
        page = None
        count = 0
        while True:
            listing = self.service.users().messages().list(userId="me", q=query, maxResults=100, pageToken=page).execute()
            for item in listing.get("messages", []):
                if exclude and exclude(item["id"]):
                    continue
                msg = self.service.users().messages().get(userId="me", id=item["id"], format="full").execute()
                payload = msg.get("payload", {})
                headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
                yield self.email_preview(msg)
                count += 1
                if count >= limit:
                    return
            page = listing.get("nextPageToken")
            if not page:
                return

    def label_id(self, label: str):
        if not self.writable or label not in LABELS:
            raise ValueError("Only approved triage labels may be created with write access.")
        name = f"AI Triage/{label}"
        labels = self.service.users().labels().list(userId="me").execute().get("labels", [])
        match = next((x for x in labels if x["name"] == name), None)
        if match:
            return match["id"]
        return self.service.users().labels().create(userId="me", body={"name": name}).execute()["id"]

    def labels(self, email_id: str):
        return self.service.users().messages().get(userId="me", id=email_id, format="minimal").execute().get("labelIds", [])

    def modify(self, email_id: str, label_id: str, *, remove=False):
        if not self.writable or not label_id.startswith("Label_"):
            raise ValueError("Write access and a custom label are required.")
        key = "removeLabelIds" if remove else "addLabelIds"
        self.service.users().messages().modify(userId="me", id=email_id, body={key: [label_id]}).execute()

    def set_unread(self, email_id: str, unread: bool):
        if not self.writable:
            raise ValueError("Authorize Gmail write access first.")
        key = "addLabelIds" if unread else "removeLabelIds"
        self.service.users().messages().modify(userId="me", id=email_id, body={key: ["UNREAD"]}).execute()

    def reply_context(self, email_id: str):
        msg = self.service.users().messages().get(
            userId="me", id=email_id, format="metadata",
            metadataHeaders=["Reply-To", "From", "Message-ID", "Subject", "References"],
        ).execute()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        recipient = headers.get("reply-to") or headers.get("from", "")
        for value in headers.values():
            if "\r" in value or "\n" in value:
                raise ValueError("Reply headers contain unexpected line breaks.")
        addresses = getaddresses([recipient])
        if len(addresses) != 1 or not addresses[0][1]:
            raise ValueError("Reply requires a single valid Reply-To or From address.")
        address = Address(addr_spec=addresses[0][1])
        if not address.username or not address.domain:
            raise ValueError("Reply recipient must have a complete email address.")
        original_id = headers.get("message-id", "").strip()
        if not original_id.startswith("<") or not original_id.endswith(">"):
            raise ValueError("Original message lacks a valid Message-ID for a threaded reply.")
        subject = headers.get("subject", "")
        return {
            "recipient": str(address),
            "subject": subject,
            "thread_id": msg["threadId"], "in_reply_to": original_id,
            "references": f"{headers.get('references', '')} {original_id}".strip(),
        }

    def send_reply(self, reply: dict):
        if not self.writable:
            raise ValueError("Authorize Gmail write access first.")
        message = EmailMessage()
        message["From"] = self.account
        message["To"] = reply["recipient"]
        message["Subject"] = reply["subject"]
        message["In-Reply-To"] = reply["in_reply_to"]
        message["References"] = reply["references"]
        message["Message-ID"] = reply["message_id"]
        message.set_content(reply["body"])
        payload = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii"), "threadId": reply["thread_id"]}
        # Sending is not idempotent: never automatically retry a send request.
        return self.service.users().messages().send(userId="me", body=payload).execute(num_retries=0)["id"]
