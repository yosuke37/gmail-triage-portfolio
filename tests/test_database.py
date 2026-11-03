import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from triage.database import engine_for, normalized_url
from triage.db_migrate import heads, revisions, upgrade
from triage.store import Store
from triage.core import Email, Suggestion
from tests import test_api as api_tests


class MigrationTests(unittest.TestCase):
    def test_backup_password_is_not_in_process_arguments(self):
        from types import SimpleNamespace
        from triage.db_backup import backup_postgres

        with tempfile.TemporaryDirectory() as folder, patch("triage.db_backup.shutil.which", return_value="pg_dump"), patch("triage.db_backup.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            backup_postgres("postgresql+psycopg://user:synthetic-password@127.0.0.1:5432/test", folder)
            self.assertNotIn("synthetic-password", " ".join(run.call_args.args[0]))
            self.assertEqual(run.call_args.kwargs["env"]["PGPASSWORD"], "synthetic-password")

    def test_fresh_schema_is_versioned_and_indexed_and_upgrade_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            store = Store(path)
            self.assertEqual(revisions(store.db.engine), heads())
            names = {item["name"] for item in inspect(store.db.engine).get_indexes("emails")}
            self.assertIn("ix_emails_account_status_id", names)
            store.add("sample", "local", Email("1", "x", "Hi", "Body"), Suggestion("Other", "Review", True), "demo")
            upgrade(store.db.engine)
            self.assertEqual(len(store.rows()), 1)
            store.close()

    def test_legacy_sqlite_is_backed_up_and_adopted_without_losing_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.db"
            with sqlite3.connect(path) as connection:
                connection.executescript("""
                    CREATE TABLE emails(id INTEGER PRIMARY KEY,source TEXT NOT NULL,account TEXT NOT NULL,email_id TEXT NOT NULL,
                        subject TEXT NOT NULL,sender TEXT NOT NULL,label TEXT NOT NULL,reason TEXT NOT NULL,needs_review INTEGER NOT NULL,
                        mode TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created TEXT NOT NULL,UNIQUE(source,account,email_id));
                    INSERT INTO emails VALUES(42,'gmail','me@example.com','original','Receipt','sender','Receipts','Payment',0,'ai','approved','2026-11-02T00:00:00+00:00');
                    CREATE TABLE daily_digests(id INTEGER PRIMARY KEY,account TEXT NOT NULL,day TEXT NOT NULL,state TEXT NOT NULL,content TEXT,error TEXT,created TEXT NOT NULL,UNIQUE(account,day));
                    INSERT INTO daily_digests VALUES(7,'me@example.com','2026-11-02','ready','{}',NULL,'2026-11-02T00:00:00+00:00');
                """)
            connection.close()
            store = Store(path)
            self.assertEqual(store.get(42)["status"], "approved")
            self.assertEqual(store.db.execute("SELECT requests,total_tokens FROM daily_digests WHERE id=7").fetchone()["requests"], 0)
            self.assertTrue(list((path.parent / "backups").glob("*.sqlite3")))
            self.assertEqual(revisions(store.db.engine), heads())
            store.close()

    def test_transactions_rollback_and_placeholder_literals_remain_literals(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "test.db")
            value = store.db.execute("SELECT '?' AS literal, ? AS parameter", ("bound ? value",)).fetchone()
            self.assertEqual(value[0], "?")
            self.assertEqual(value["parameter"], "bound ? value")
            with self.assertRaises(RuntimeError):
                with store.db:
                    store.db.execute("INSERT INTO automation(id,config,updated) VALUES (1,'{}','now')")
                    raise RuntimeError("rollback")
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM automation").fetchone()[0], 0)
            store.close()


PG_ENABLED = os.getenv("TRIAGE_TEST_POSTGRES") == "1"


class PostgresFixture:
    def pg_setup(self, migrate=True):
        import psycopg
        from psycopg import sql
        from triage.checkpoints import native_url

        self.pg_name = "triage_test_" + uuid4().hex
        self.pg_admin_url = os.getenv("TRIAGE_TEST_ADMIN_URL")
        if not self.pg_admin_url:
            config = json.loads((Path(__file__).resolve().parent.parent / ".secrets" / "local_postgres.json").read_text())
            from sqlalchemy.engine import URL

            self.pg_admin_url = URL.create("postgresql", username="triage_admin", password=config["password"], host="127.0.0.1", port=config["port"], database="postgres").render_as_string(hide_password=False)
        from sqlalchemy.engine import make_url

        self.pg_url = make_url(self.pg_admin_url).set(drivername="postgresql+psycopg", database=self.pg_name).render_as_string(hide_password=False)
        with psycopg.connect(native_url(self.pg_admin_url), autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.pg_name)))
        self.pg_engine = engine_for(self.pg_url)
        if migrate:
            upgrade(self.pg_engine)

    def pg_cleanup(self):
        import psycopg
        from psycopg import sql
        from triage.checkpoints import native_url

        self.pg_engine.dispose()
        assert self.pg_name.startswith("triage_test_") and len(self.pg_name) == 44
        with psycopg.connect(native_url(self.pg_admin_url), autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(self.pg_name)))


@unittest.skipUnless(PG_ENABLED, "Set TRIAGE_TEST_POSTGRES=1 for isolated real PostgreSQL tests")
class PostgresApiTests(PostgresFixture, api_tests.ApiTests):
    def setUp(self):
        self.pg_setup()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self.pg_cleanup()


@unittest.skipUnless(PG_ENABLED, "Set TRIAGE_TEST_POSTGRES=1 for isolated real PostgreSQL tests")
class PostgresTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.pg_setup(migrate=False)
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "metadata.db"

    def tearDown(self):
        self.pg_cleanup()
        self.temp.cleanup()

    def test_runtime_requires_migrations_and_upgrade_is_idempotent(self):
        with self.assertRaisesRegex(ValueError, "schema is not current"):
            Store(self.path, database_url=self.pg_url)
        upgrade(self.pg_engine)
        upgrade(self.pg_engine)
        store = Store(self.path, database_url=self.pg_url)
        self.assertEqual(store.rows(), [])
        self.assertEqual(revisions(self.pg_engine), heads())
        store.close()

    def test_import_preserves_reviews_audit_replies_and_sequences_and_refuses_repeat(self):
        from triage.db_import import import_sqlite
        from triage.automation import Automation

        source = Store(self.path)
        source.add("gmail", "me@example.com", Email("1", "sender", "Receipt", "Body"), Suggestion("Receipts", "Payment", False), "ai")
        row_id = source.rows()[0]["id"]
        source.review(row_id, "Receipts", approve=True)
        Automation(source)
        with source.db:
            source.db.execute("INSERT INTO replies(email_row,payload,state,created) VALUES (?,'{}','sent','now')", (row_id,))
        source.close()
        upgrade(self.pg_engine)
        result = import_sqlite(self.path, self.pg_engine)
        self.assertEqual(result["counts"]["emails"], 1)
        target = Store(self.path, database_url=self.pg_url)
        self.assertEqual(target.get(row_id)["status"], "approved")
        self.assertEqual(target.reply(row_id)["state"], "sent")
        self.assertFalse(Automation(target).state()["enabled"])
        target.add("sample", "local", Email("2", "x", "New", "Body"), Suggestion("Other", "Review", True), "demo")
        self.assertGreater(target.rows()[0]["id"], row_id)
        target.close()
        with self.assertRaisesRegex(ValueError, "not empty"):
            import_sqlite(self.path, self.pg_engine)

    def test_import_rolls_back_when_legacy_data_has_orphaned_references(self):
        from triage.db_import import import_sqlite

        source = Store(self.path)
        source.add("sample", "local", Email("1", "x", "Hi", "Body"), Suggestion("Other", "Review", True), "demo")
        source.close()
        connection = sqlite3.connect(self.path)
        connection.execute("INSERT INTO audit(email_row,event,details,created) VALUES (999,'legacy','{}','now')")
        connection.commit()
        connection.close()
        upgrade(self.pg_engine)
        with self.assertRaises(IntegrityError):
            import_sqlite(self.path, self.pg_engine)
        with self.pg_engine.connect() as target:
            self.assertEqual(target.execute(text("SELECT COUNT(*) FROM emails")).scalar_one(), 0)
            self.assertEqual(target.execute(text("SELECT COUNT(*) FROM audit")).scalar_one(), 0)

    def test_postgres_session_locks_coordinate_different_local_paths(self):
        from triage.db_locks import store_lock

        upgrade(self.pg_engine)
        first = Store(self.path, database_url=self.pg_url)
        second = Store(self.path.with_name("other.db"), database_url=self.pg_url)
        with store_lock(first, "ingestion"):
            with self.assertRaisesRegex(ValueError, "Another ingestion"):
                with store_lock(second, "ingestion"):
                    pass
        with store_lock(second, "ingestion"):
            pass
        first.close()
        second.close()

    def test_custom_backup_can_restore_into_another_isolated_database(self):
        import subprocess
        import psycopg
        from psycopg import sql
        from sqlalchemy.engine import make_url
        from triage.db_backup import backup_postgres
        from triage.checkpoints import native_url

        upgrade(self.pg_engine)
        store = Store(self.path, database_url=self.pg_url)
        store.add("sample", "local", Email("1", "x", "Backup test", "Body"), Suggestion("Other", "Review", True), "demo")
        store.close()
        dump = backup_postgres(self.pg_url, Path(self.temp.name))
        self.assertEqual(dump.read_bytes()[:5], b"PGDMP")
        restored_name = "triage_test_" + uuid4().hex
        restored_url = make_url(self.pg_url).set(database=restored_name)
        import shutil
        binary = shutil.which("pg_restore")
        if not binary:
            binary = Path(__file__).resolve().parent.parent / ".tools" / "postgresql-18.6-5" / "pgsql" / "bin" / "pg_restore.exe"
        self.assertTrue(Path(binary).is_file(), "Install a PostgreSQL pg_restore client compatible with the test server")
        with psycopg.connect(native_url(self.pg_admin_url), autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restored_name)))
        try:
            result = subprocess.run([str(binary), "--dbname", restored_url.set(drivername="postgresql")._replace(password=None).render_as_string(hide_password=False), "--no-owner", "--no-privileges", str(dump)],
                env={**os.environ, "PGPASSWORD": restored_url.password or ""}, capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.assertEqual(result.returncode, 0, "PostgreSQL restore failed (provider details omitted)")
            restored_engine = engine_for(restored_url.render_as_string(hide_password=False))
            with restored_engine.connect() as connection:
                self.assertEqual(connection.execute(text("SELECT COUNT(*) FROM emails")).scalar_one(), 1)
            restored_engine.dispose()
        finally:
            with psycopg.connect(native_url(self.pg_admin_url), autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(restored_name)))

    def test_concurrent_apply_and_send_execute_provider_once(self):
        upgrade(self.pg_engine)
        store = Store(self.path, database_url=self.pg_url)
        store.add("gmail", "me@example.com", Email("1", "x", "Receipt", "Body"), Suggestion("Receipts", "Payment", False), "ai")
        row_id = store.rows()[0]["id"]
        store.review(row_id, "Receipts", approve=True)
        gmail = MagicMock()
        gmail.account = "me@example.com"
        gmail.label_id.return_value = "Label_receipts"
        gmail.labels.return_value = []
        gmail.reply_context.return_value = {"recipient":"sender@example.com","subject":"Receipt","thread_id":"1","in_reply_to":"<original@example.com>","references":"<original@example.com>"}
        gmail.send_reply.return_value = "sent-1"

        def apply(_):
            connection = Store(self.path, database_url=self.pg_url)
            try:
                return connection.apply(row_id, gmail)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(apply, range(2)))
        gmail.modify.assert_called_once()
        draft = store.prepare_reply(row_id, gmail, "Thanks")

        def send(_):
            connection = Store(self.path, database_url=self.pg_url)
            try:
                return connection.send_reply(row_id, gmail, expected_message_id=draft["payload"]["message_id"])
            except ValueError:
                return "blocked"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertIn("blocked", list(pool.map(send, range(2))))
        gmail.send_reply.assert_called_once()
        store.close()

    def test_postgres_checkpoint_resumes_completed_classification(self):
        from triage import settings
        from triage.checkpoints import setup_postgres, checkpoint_saver
        from triage.workflow import build_workflow

        upgrade(self.pg_engine)

        async def scenario():
            await setup_postgres(self.pg_url)
            store = Store(self.path, database_url=self.pg_url)
            classify = AsyncMock(return_value=Suggestion("Other", "Review", True))
            config = {"configurable":{"thread_id":"recovery-test"}}
            async with checkpoint_saver(store) as saver:
                graph = build_workflow(classify, checkpointer=saver)
                with patch("triage.workflow.preview_record", side_effect=RuntimeError("interrupted")):
                    with self.assertRaises(RuntimeError):
                        await graph.ainvoke({"email":Email("1","x","Hi","Body")}, config, durability="sync")
            async with checkpoint_saver(store) as saver:
                graph = build_workflow(classify, checkpointer=saver)
                result = await graph.ainvoke(None, config, durability="sync")
                self.assertEqual(result["preview"]["label"], "Other")
                self.assertEqual(classify.await_count, 1)
            store.close()

        asyncio.run(scenario())
