"""Offline deployment smoke checks. Never calls Gmail/OpenAI or enables automation."""
import base64
import json
from pathlib import Path
import ssl
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ["docker", "compose", "--env-file", "deploy/compose.env", "--profile", "local-db"]


def compose(*args, capture=False):
    return subprocess.run([*COMPOSE, *args], cwd=ROOT, check=True, capture_output=capture, text=True)


def request(path, *, authorized=False):
    headers = {}
    if authorized:
        login = json.loads((ROOT / ".secrets/deploy/login.json").read_text())
        headers["Authorization"] = "Basic " + base64.b64encode((login["username"] + ":" + login["password"]).encode()).decode()
    # Local smoke only: Caddy's development certificate is not installed in CI.
    context = ssl._create_unverified_context()
    try:
        with urlopen(Request("https://localhost:8443" + path, headers=headers), context=context, timeout=10) as response:
            return response.status
    except HTTPError as exc:
        return exc.code


def main():
    deadline = time.monotonic() + 240
    while True:
        try:
            if request("/gateway-health") == 200:
                break
        except (URLError, OSError):
            pass
        if time.monotonic() > deadline:
            raise RuntimeError("Gateway did not become ready")
        time.sleep(2)
    assert request("/") == 401, "Unauthenticated dashboard must be blocked"
    assert request("/_stcore/health") == 401, "Direct Streamlit paths must require login"
    assert request("/", authorized=True) == 200, "Authenticated dashboard must load"
    code = """from triage import settings
from triage.automation import Automation
from triage.jobs import Jobs
store=settings.open_store()
assert store.db.backend=='postgresql'
role=store.db.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()
assert not any(role.values()), 'Application role must not administer the database cluster'
assert not Automation(store).state()['enabled']
assert Jobs(store).list()==[]
services=Jobs(store).services()
assert all(any(s['kind']==k and s['healthy'] for s in services) for k in ('worker','scheduler'))
store.close()
print('Database, worker, scheduler and disabled schedule verified.')"""
    compose("exec", "-T", "api", "python", "-c", code)
    compose("exec", "-T", "backups", "python3", "/backup_service.py", "once")
    # Restore a validated dump into a disposable DB inside this fresh local
    # Compose project. The host application's PostgreSQL is never addressed.
    compose("exec", "-T", "postgres", "createdb", "-U", "triage_admin", "-O", "triage", "triage_restore_smoke")
    try:
        restore = """from pathlib import Path
import os,subprocess
from backup_service import connection_env
env=connection_env();env['PGDATABASE']='triage_restore_smoke'
archive=sorted(Path('/backups').glob('postgres-*.dump'))[-1]
subprocess.run(['pg_restore','--exit-on-error','--no-owner','--dbname','triage_restore_smoke',str(archive)],env=env,check=True,capture_output=True)
result=subprocess.run(['psql','-tAc','SELECT version_num FROM alembic_version'],env=env,check=True,capture_output=True,text=True)
assert '0004_job_attempts' in result.stdout
print('Backup restore verified in disposable local database.')"""
        compose("exec", "-T", "backups", "python3", "-c", restore)
    finally:
        compose("exec", "-T", "postgres", "dropdb", "-U", "triage_admin", "triage_restore_smoke")
    # Prove queue durability through actual consumer/API restarts with a safe,
    # future-due synthetic job; never let a worker contact an external provider.
    create = """from triage import settings
from triage.jobs import Jobs
s=settings.open_store();q=Jobs(s)
j=q.enqueue('ingest',{'query':'in:inbox','mode':'demo','limit':1},key='deployment-smoke')
with s.db:s.db.execute('UPDATE jobs SET available_at=? WHERE id=?',(q.seconds()+86400,j['id']))
s.close()"""
    compose("stop", "worker", "scheduler")
    try:
        compose("exec", "-T", "api", "python", "-c", create)
        compose("restart", "api")
        check = """from triage import settings
from triage.jobs import Jobs
s=settings.open_store();q=Jobs(s);jobs=q.list();assert len(jobs)==1 and jobs[0]['state']=='queued';q.cancel(jobs[0]['id']);s.close();print('Job persisted through API/worker stop and restart; cancelled before execution.')"""
        compose("exec", "-T", "api", "python", "-c", check)
    finally:
        compose("start", "worker", "scheduler")
    print("Deployment smoke checks passed. No mailbox or model requests were made.")


if __name__ == "__main__":
    main()
