"""Per-container health evidence; one worker cannot hide another stalled worker."""
import json
import os
from pathlib import Path
import sys
import time
from urllib.request import urlopen


def path_for(kind):
    return Path(os.getenv("TRIAGE_SERVICE_STATE_DIR", "/tmp/triage-health")) / (kind + ".json")


def record(kind, state):
    if os.getenv("TRIAGE_SERVICE_KIND") != kind:
        return
    path = path_for(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"heartbeat": time.time(), "state": state}), encoding="utf-8")
    temporary.replace(path)


def check(kind):
    if kind in ("api", "dashboard"):
        url = "http://127.0.0.1:8000/ready" if kind == "api" else "http://127.0.0.1:8501/_stcore/health"
        with urlopen(url, timeout=5) as response:
            return response.status == 200
    value = json.loads(path_for(kind).read_text(encoding="utf-8"))
    return value["state"] != "stopped" and time.time() - value["heartbeat"] < 45


if __name__ == "__main__":
    try:
        healthy = check(sys.argv[1])
    except Exception:
        healthy = False
    raise SystemExit(0 if healthy else 1)
