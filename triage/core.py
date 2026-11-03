from dataclasses import asdict, dataclass
from typing import Literal

Label = Literal["Action needed", "Recruiting", "Receipts", "Newsletters", "Other"]
LABELS = ("Action needed", "Recruiting", "Receipts", "Newsletters", "Other")


@dataclass(frozen=True)
class Email:
    id: str
    sender: str
    subject: str
    body: str
    attachments: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Suggestion:
    label: Label
    reason: str
    needs_review: bool
    proposal: dict | None = None


def demo_classify(email: Email) -> Suggestion:
    """Deterministic fixture classifier for exercising the UI without an API key.

    This is deliberately a keyword baseline, not an AI simulation or quality metric.
    """
    from triage.attachments import attachment_context
    text = (f"{email.subject}\n{email.body}\n" + "\n".join(a["text"] for a in attachment_context(email))).lower()
    if "newsletter" in text or "unsubscribe" in text:
        return Suggestion("Newsletters", "Contains newsletter or unsubscribe text.", False)
    if "interview" in text or "recruiter" in text:
        return Suggestion("Recruiting", "Mentions an interview or recruiter.", False)
    if "receipt" in text or "payment received" in text:
        return Suggestion("Receipts", "Contains receipt or payment confirmation text.", False)
    if "please confirm" in text or "please review" in text:
        return Suggestion("Action needed", "Contains a direct request for action.", False)
    return Suggestion("Other", "No baseline rule matched; human review required.", True)


def preview_record(email: Email, suggestion: Suggestion) -> dict:
    if suggestion.label not in LABELS:
        raise ValueError(f"Unsupported label: {suggestion.label}")
    return {
        "email_id": email.id,
        "subject": email.subject,
        **asdict(suggestion),
        "status": "review_required" if suggestion.needs_review else "suggested",
        "applied": False,
    }
