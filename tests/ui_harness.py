"""Dashboard tests use real HTTP serialization/routes, with an in-process API."""
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

from triage.api import create_app


@contextmanager
def api_session(path):
    app = create_app(database_path=path, token="offline-test-token")
    clients = []

    def make_client():
        client = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer offline-test-token"}, raise_server_exceptions=False)
        clients.append(client)
        return client

    with patch("triage.api_client.make_client", side_effect=make_client):
        try:
            from types import SimpleNamespace
            from triage.job_worker import process_once
            def run_jobs():
                for _ in range(30):
                    if not process_once(path, heartbeat=False):
                        return
                raise AssertionError("Queue did not drain")
            yield SimpleNamespace(run_jobs=run_jobs)
        finally:
            for client in clients:
                client.close()
