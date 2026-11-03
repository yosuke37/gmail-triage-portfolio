"""A local service credential, independent of OpenAI and Google credentials."""
import os
from pathlib import Path
import secrets


def api_token(*, create=False):
    from triage.secrets_config import secret_value

    if os.getenv("TRIAGE_API_TOKEN_FILE") or os.getenv("TRIAGE_API_TOKEN"):
        token = secret_value("TRIAGE_API_TOKEN")
        if len(token) < 32:
            raise ValueError("Configured API service credential must contain at least 32 characters.")
        return token
    path = Path(__file__).resolve().parent.parent / ".secrets" / "api_token"
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
    if not path.exists():
        raise ValueError("Start the application API first: python -m triage.api")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("Local API credential is invalid. Stop both services and remove .secrets/api_token, then restart the API.")
    return token
