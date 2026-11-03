"""Generate private deployment secrets. Does not publish, copy mailbox data, or enable automation."""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent


def write_private(path, value, *, replace_empty=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if not replace_empty or path.stat().st_size:
            return
        path.write_text(value, encoding="utf-8")
    else:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
            handle.write(value)
    # Local Compose secret mounts retain host ownership. The enclosing directory
    # is private; mount individual files read-only into only selected services.
    if os.name != "nt":
        path.parent.chmod(0o700)
        path.chmod(0o644)


def prepare(*, use_local_credentials=False, directory=None):
    import bcrypt
    folder = Path(directory or ROOT / ".secrets" / "deploy")
    write_private(folder / "postgres_password", secrets.token_urlsafe(32))
    write_private(folder / "postgres_admin_password", secrets.token_urlsafe(48))
    password = (folder / "postgres_password").read_text().strip()
    write_private(folder / "database_url", f"postgresql://triage:{password}@postgres:5432/triage")
    write_private(folder / "api_token", secrets.token_urlsafe(48))
    write_private(folder / "login.json", json.dumps({"username": "owner", "password": secrets.token_urlsafe(32)}))
    login = json.loads((folder / "login.json").read_text())
    encoded = bcrypt.hashpw(login["password"].encode(), bcrypt.gensalt()).decode()
    write_private(folder / "gateway_auth", "basic_auth {\n    " + login["username"] + " " + encoded + "\n}\n")
    for name in ("openai_key", "google_credentials", "google_read_token", "google_write_token"):
        write_private(folder / name, "")
    if use_local_credentials:
        from triage import settings
        write_private(folder / "openai_key", os.getenv("OPENAI_API_KEY", ""), replace_empty=True)
        credentials = settings.local_path("GOOGLE_CREDENTIALS_PATH", ".secrets/credentials.json")
        for name, source in (("google_credentials", credentials), ("google_read_token", credentials.parent / "token_readonly.json"), ("google_write_token", credentials.parent / "token_modify.json")):
            if source.is_file():
                write_private(folder / name, source.read_text(encoding="utf-8"), replace_empty=True)
    print("Private deployment configuration prepared. Login is in .secrets/deploy/login.json; no secret values were printed.")
    return folder


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--use-local-credentials", action="store_true", help="Copy existing OpenAI/Gmail credentials into separate local Compose secret files; never copies the current database.")
    parser.add_argument("--hosted-database", action="store_true", help="Prompt privately for a hosted PostgreSQL URL, requiring TLS. Stop the deployment before changing its database.")
    args = parser.parse_args()
    folder = prepare(use_local_credentials=args.use_local_credentials)
    if args.hosted_database:
        value = getpass.getpass("Hosted PostgreSQL URL (hidden): ").strip()
        parsed = urlsplit(value.replace("postgresql+psycopg://", "postgresql://", 1))
        from urllib.parse import parse_qs
        if parsed.scheme not in ("postgresql", "postgres") or parse_qs(parsed.query).get("sslmode", [""])[0] not in ("require", "verify-full", "verify-ca"):
            raise ValueError("Provide a PostgreSQL URL with sslmode=require or certificate verification.")
        (folder / "database_url").write_text(value, encoding="utf-8")
        print("Hosted database secret saved privately. No deployment was started.")


if __name__ == "__main__":
    main()
