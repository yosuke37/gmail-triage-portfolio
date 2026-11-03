"""Read secrets supplied by a container/cloud secret-file mount."""
import os
from pathlib import Path


def secret_value(name, default=""):
    path = os.getenv(name + "_FILE", "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            raise ValueError(f"Cannot read configured {name}_FILE.") from None
    return os.getenv(name, default).strip()


def load_provider_secrets():
    # The SDK reads this environment variable; the container specification and
    # process arguments contain only the path, never the mounted secret value.
    if os.getenv("OPENAI_API_KEY_FILE"):
        os.environ["OPENAI_API_KEY"] = secret_value("OPENAI_API_KEY")
