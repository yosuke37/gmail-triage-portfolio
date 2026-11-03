import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class SecretTests(unittest.TestCase):
    def test_secret_file_precedence_and_redacted_missing_file_error(self):
        from triage.secrets_config import secret_value
        from triage.api_security import api_token
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secret"
            path.write_text("x" * 40)
            with patch.dict(os.environ, {"TRIAGE_API_TOKEN_FILE": str(path), "TRIAGE_API_TOKEN": "ignored"}):
                self.assertEqual(api_token(create=True), "x" * 40)
                path.unlink()
                with self.assertRaisesRegex(ValueError, "Cannot read configured"):
                    secret_value("TRIAGE_API_TOKEN")

    def test_explicit_internal_origin_is_exact_and_remote_default_stays_blocked(self):
        from triage.api_client import make_client
        base = "http://api.internal:8000"
        with patch.dict(os.environ, {"TRIAGE_API_URL": base, "TRIAGE_INTERNAL_API_ORIGIN": base}), patch("triage.api_client.api_token", return_value="x" * 40):
            client = make_client()
            self.assertEqual(str(client.base_url), base)
            client.close()
            for value in ("http://api.internal:8000.attacker.test", base + "/private", "http://user:password@api.internal:8000", base + "?key=x"):
                with patch.dict(os.environ, {"TRIAGE_API_URL": value}), self.assertRaises(ValueError):
                    make_client()

    def test_gmail_reads_seed_secret_and_writes_refreshed_token_to_cache(self):
        from triage.gmail import connect
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            seed = folder / "mounted-token"
            seed.write_text("original-secret")
            cache = folder / "cache"
            creds = SimpleNamespace(valid=True, expired=False, to_json=lambda: "refreshed-token")
            with patch.dict(os.environ, {"GOOGLE_READ_TOKEN_FILE": str(seed), "GOOGLE_TOKEN_DIR": str(cache)}), patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=creds) as load, patch("googleapiclient.discovery.build"), patch("triage.gmail.Gmail"):
                connect(folder / "credentials.json")
            self.assertEqual(load.call_args.args[0], str(seed))
            self.assertEqual(seed.read_text(), "original-secret")
            self.assertEqual((cache / "token_readonly.json").read_text(), "refreshed-token")
            self.assertEqual(list(cache.glob("*.tmp")), [])

    def test_prepare_is_idempotent_does_not_copy_credentials_by_default(self):
        prepare = module("prepare_deployment_test", "scripts/prepare_deployment.py")
        import bcrypt
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private"
            prepare.prepare(directory=path)
            first = (path / "api_token").read_text()
            login = json.loads((path / "login.json").read_text())
            encoded = (path / "gateway_auth").read_text().split()[3]
            self.assertTrue(bcrypt.checkpw(login["password"].encode(), encoded.encode()))
            prepare.prepare(directory=path)
            self.assertEqual((path / "api_token").read_text(), first)
            self.assertEqual((path / "openai_key").read_text(), "")


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.backup = module("backup_service_test", "deploy/backup_service.py")
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.secret = self.folder / "db-secret"
        self.secret.write_text("postgresql://owner:private%40password@db:5432/triage?sslmode=require")
        self.env = patch.dict(os.environ, {"TRIAGE_DATABASE_URL_FILE": str(self.secret), "BACKUP_DIR": str(self.folder / "backups"), "BACKUP_RETAIN_COUNT": "2"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_backup_validates_archive_before_retention_and_uses_safe_argv(self):
        def run(args, **kwargs):
            if args[0] == "pg_dump":
                self.assertEqual(kwargs["env"]["PGPASSWORD"], "private@password")
                self.assertEqual(kwargs["env"]["PGSSLMODE"], "require")
                self.assertNotIn("private", " ".join(args))
                Path(args[-1]).write_bytes(b"synthetic dump")
            return SimpleNamespace(returncode=0)
        with patch.object(self.backup.subprocess, "run", side_effect=run):
            for _ in range(3):
                self.backup.backup_once()
        self.assertEqual(len(list((self.folder / "backups").glob("*.dump"))), 2)
        self.assertTrue(self.backup.healthy())
        with patch.object(self.backup.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(ValueError):
                self.backup.backup_once()
        self.assertEqual(len(list((self.folder / "backups").glob("*.dump"))), 2)
        self.assertTrue(self.backup.healthy())

    def test_no_backup_success_marker_when_archive_validation_fails(self):
        def run(args, **kwargs):
            if args[0] == "pg_dump":
                Path(args[-1]).write_bytes(b"bad dump")
                return SimpleNamespace(returncode=0)
            return SimpleNamespace(returncode=1)
        with patch.object(self.backup.subprocess, "run", side_effect=run):
            with self.assertRaises(ValueError):
                self.backup.backup_once()
        folder = self.folder / "backups"
        self.assertFalse((folder / ".last-success.json").exists())
        self.assertEqual(list(folder.glob("*.partial")), [])


class PackagingTests(unittest.TestCase):
    def test_compose_publishes_only_authenticated_gateway_and_scopes_secrets(self):
        import yaml
        config = yaml.safe_load((ROOT / "compose.yaml").read_text())
        services = config["services"]
        self.assertEqual([name for name, value in services.items() if value.get("ports")], ["gateway"])
        self.assertEqual(services["gateway"]["ports"], ["127.0.0.1:8443:443"])
        self.assertEqual(services["dashboard"]["secrets"], ["api_token"])
        self.assertEqual(services["scheduler"]["secrets"], ["database_url"])
        for name in ("api", "dashboard", "worker", "scheduler", "backups"):
            self.assertEqual(services[name]["restart"], "unless-stopped")
            self.assertTrue(services[name]["healthcheck"])
        self.assertIn("basic_auth", (ROOT / "scripts/prepare_deployment.py").read_text())
        caddy = (ROOT / "deploy/Caddyfile").read_text()
        self.assertIn("import {$TRIAGE_GATEWAY_AUTH_FILE", caddy)
        self.assertIn("header_up -Authorization", caddy)

    def test_render_exposes_only_gateway_and_uses_dedicated_background_workers(self):
        import yaml
        config = yaml.safe_load((ROOT / "render.yaml").read_text())
        services = config["services"]
        self.assertEqual([s["name"] for s in services if s["type"] == "web"], ["triage-private-gateway"])
        self.assertEqual(len([s for s in services if s["type"] == "worker"]), 2)
        # Render's API enforces this service-type restriction beyond its JSON
        # schema. Private services receive platform TCP checks, not HTTP paths.
        for service in services:
            if service["type"] != "web":
                self.assertNotIn("healthCheckPath", service)
        self.assertEqual(config["databases"][0]["ipAllowList"], [])
        self.assertNotIn(".secrets", (ROOT / "Dockerfile").read_text())


class HealthTests(unittest.TestCase):
    def test_health_is_local_to_service_and_becomes_stale(self):
        from triage.service_health import record, check
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"TRIAGE_SERVICE_STATE_DIR": folder, "TRIAGE_SERVICE_KIND": "worker"}):
            record("worker", "busy")
            self.assertTrue(check("worker"))
            with patch("triage.service_health.time.time", return_value=10 ** 12):
                self.assertFalse(check("worker"))
            record("worker", "stopped")
            self.assertFalse(check("worker"))
