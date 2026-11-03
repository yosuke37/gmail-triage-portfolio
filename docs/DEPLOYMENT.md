# Reproducible local deployment and private hosting

This milestone adds Compose packaging, a login-protected HTTPS gateway, file-based secrets, readiness/heartbeat checks, restart policies, scheduled database dumps, per-attempt operational metrics, and GitHub CI. **No cloud resources have been provisioned.** The local Linux-container build and deployment smoke checks passed on Docker Desktop: HTTPS/login enforcement, service health, a full backup restore, a restricted application database role, and job persistence through service restart. No Gmail or OpenAI requests were made. CI repeats these checks on a fresh stack.

The deployment remains single-owner. Gateway login protects access to your app; it does not add separate users or OAuth connections. There is no public API/database/dashboard port behind the gateway. Sending replies retains its explicit confirmation and uncertain-send protections.

## Local Windows setup

1. Install [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/) with Linux containers and start it. Follow its WSL/virtualization prerequisites. Confirm `docker version` reports a running server and `docker compose version` is at least **2.24.4** (needed for the cloud port override).
2. In the project folder:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[deploy]"
.\.venv\Scripts\python.exe scripts/prepare_deployment.py
Copy-Item deploy/compose.env.example deploy/compose.env
docker compose --env-file deploy/compose.env --profile local-db config --quiet
docker compose --env-file deploy/compose.env --profile local-db up --build --wait --wait-timeout 300
```

`prepare_deployment.py` generates independent database and service credentials, a random owner login, and a bcrypt password hash. It never prints their values or copies credentials by default. Rerunning it retains existing secret values. Local Compose secrets are bind-mounted files, not an encrypted secret vault; keep `.secrets/deploy` private and out of Git. The setup script restricts its directory on POSIX systems; protect that directory with your Windows account permissions too. Application secrets are not Docker build arguments, image layers, process arguments, or Compose plaintext environment values. [Docker secret mounts](https://docs.docker.com/compose/how-tos/use-secrets/)

The new stack uses its own PostgreSQL volume and starts with an empty review database, no jobs, and scheduling disabled. It does not import or overwrite the existing native application database. Only the gateway is published, at **https://localhost:8443**; the existing native dashboard at port 8501 can continue running separately.

Open `.secrets/deploy/login.json` locally to read your username/password. Do not paste it into chat or commit it. Caddy uses a local development certificate; your browser will not initially trust its container CA. Copy the public root certificate out of Caddy and trust it locally if you want browser-trusted development HTTPS:

```powershell
docker compose --env-file deploy/compose.env cp gateway:/data/caddy/pki/authorities/local/root.crt .local/triage-development-ca.crt
```

Import that **public certificate** into your user's trusted root certificate store only if you intend to trust this local development CA. Do not distribute or copy Caddy's CA private key. Public cloud domains use the platform's trusted HTTPS certificate instead. [Caddy local HTTPS](https://caddyserver.com/docs/automatic-https#local-https)

## Enable live Gmail/AI in containers

The initial deployment works without provider credentials, showing an empty dashboard. To use your existing local authorization in this separate local deployment:

```powershell
.\.venv\Scripts\python.exe scripts/prepare_deployment.py --use-local-credentials
```

Set `OPENAI_MODEL` in `deploy/compose.env` to your existing configured model ID, then recreate services:

```powershell
docker compose --env-file deploy/compose.env --profile local-db up -d --force-recreate api worker dashboard scheduler
```

The copy flag copies only the existing OpenAI key, Google Desktop client, and read/write OAuth token files into private local secret files; it does not copy email data or enable scheduling. Existing nonempty deployment secrets are retained. To rotate them, stop the stack, update the individual secret files locally, and recreate the services. Google tokens are seeded from read-only secret mounts; refreshed credentials are atomically written to `/app/output/oauth` on the persistent `app_state` volume. All images exclude your `.env`, `.secrets`, `output`, and non-synthetic data/reports through a build-context allowlist.

For container authorization, complete the existing Desktop OAuth flow **on your local computer** first; do not run its browser callback flow on a remote headless server. Hosted OAuth tokens still depend on Google's consent/testing/verification configuration and can expire or be revoked. Reauthorize locally and replace the cloud secret seeds if needed. Rotating seeds also requires clearing/replacing the appropriate cached OAuth token after stopping services; a cached token takes precedence over its seed.

## Service lifecycle and checks

```powershell
docker compose --env-file deploy/compose.env --profile local-db ps --all
docker compose --env-file deploy/compose.env --profile local-db logs --tail 50 api worker scheduler backups
docker compose --env-file deploy/compose.env --profile local-db stop
docker compose --env-file deploy/compose.env --profile local-db start
```

Compose waits for a successful one-shot schema migration before starting processing services; the dashboard waits for API readiness and the gateway waits for the dashboard. `/health` is API liveness, `/ready` checks database connectivity and schema, and worker/scheduler checks use each container's own heartbeat. `unless-stopped` restarts exited processes and resumes services after the Docker daemon starts. A health check marking a container unhealthy does **not** itself restart that container; monitor unhealthy status and investigate stalled provider calls. The gateway's public `/gateway-health` returns only `ok` and bypasses login for platform probes. Every other path, including Streamlit's WebSocket/health paths, requires login. [Compose dependency conditions](https://docs.docker.com/compose/how-tos/startup-order/)

Before upgrades, stop scheduled triage, let workers drain, and create a backup. Stop workers/scheduler, deploy the new image, and run migrations before restarting consumers. Avoid running old and new schema-dependent workers during migration. A standard `down` keeps named volumes; **do not use `down --volumes` on a deployment whose data you want to retain**. Each explicitly stopped service stays stopped after daemon restart until started again.

## Backup and recovery

The backup service creates a custom-format PostgreSQL dump at startup and every 24 hours, validates the archive with `pg_restore --list`, and retains the latest seven successful archives. Failed dumps never prune prior good archives. Its health check detects a missing/stale successful backup. The backup image uses PostgreSQL 18 clients; use a client version compatible with your hosted database, and update the image when moving to a newer major server version.

```powershell
docker compose --env-file deploy/compose.env exec backups python3 /backup_service.py once
docker compose --env-file deploy/compose.env cp backups:/backups .local/container-backups
```

Archives contain private mailbox data and OAuth-independent application state. Protect and encrypt exports. The local backup volume is on the **same host**: it is not off-site disaster recovery. It does not include Google credentials, provider secret files, Caddy's certificates, or cached OAuth tokens; back up or regenerate these separately through secure secret management. For cloud hosting, enable and verify the provider's managed backups/PITR, document retention, and keep encrypted off-site exports as needed. Alert on failed jobs, stale service health, and missed backups using your provider's monitoring.

Restore into a new empty database with `pg_restore --exit-on-error --no-owner`, validate migrations and record counts, disable the restored automation schedule, and only then change services to its connection secret. A restored schedule must not start modifying Gmail before you review it. Test restores regularly. The local deployment smoke script performs a full restore into a temporary database in the isolated Compose PostgreSQL service.

## Automated deployment evidence

On a **fresh synthetic local stack only**, run:

```powershell
.\.venv\Scripts\python.exe deploy/smoke.py
```

The script requires an empty queue and disabled scheduling. It checks unauthenticated access is denied, authenticated dashboard access succeeds, internal services are healthy, backup archives restore, and a future-due synthetic job survives an API restart. It stops/restarts workers and cancels the synthetic job before it can run. It never calls Gmail/OpenAI. It leaves that cancelled job as evidence. Recreate a disposable stack for a repeat run; do not run this lifecycle test on your live deployment.

GitHub Actions runs offline/isolated-PostgreSQL tests and builds/starts a disposable Compose deployment for these smoke checks. It has no production credentials and cannot deploy your mailbox app. The workflow has read-only repository permissions. Dependency versions are captured in `requirements.lock`; base image tags track their named release family, so this is a reproducible deployment recipe rather than a bit-for-bit immutable image. Pin registry digests and update the dependency snapshot through CI before production releases. CI has been added locally but has not run on GitHub yet.

To generate portable **synthetic** recovery evidence without Docker:

```powershell
.\.venv\Scripts\python.exe scripts/recovery_demo.py --output reports/recovery-demo.json
```

The dashboard's Background jobs panel and authenticated `GET /v1/operations` now show state counts, attempt history, durations, observed SDK requests/tokens, and expired lease recovery. Attempt rows persist separately from job results. Nested drafting/digest usage listeners forward counters to the enclosing job, so usage is counted once per job attempt. Only SDK responses actually observed by the app are counted; interrupted/failed calls can still be billed. Queue wait time is not included in attempt duration. Older jobs predating this migration have no backfilled attempt history. No email bodies, instructions, raw provider errors, or credentials are included in operational metrics.

## Private Render deployment — prepared, not provisioned

Render is the recommended first hosted target here because it supports private services, background workers, and managed PostgreSQL. `render.yaml` defines two private services (API/dashboard), two persistent worker processes (consumer/scheduler), one public login gateway, and managed PostgreSQL. The public gateway receives the platform's `onrender.com` HTTPS address; a purchased domain is optional. Raw API/dashboard services have no public address. [Render private services](https://render.com/docs/private-services), [Blueprint reference](https://render.com/docs/blueprint-spec)

Before creating the Blueprint, review current [Render pricing](https://render.com/pricing): it provisions **five paid compute services plus paid PostgreSQL**. No resource has been purchased or created by this work. Keep automatic deployments off during initial setup and review deployment diffs before updating a live mailbox app.

1. Create a Render account and a private GitHub repository after reviewing Git exclusions. Connect that repo to a Blueprint using `render.yaml`, review the resource/cost plan, then provision only when you are ready to pay.
2. The Blueprint creates a shared random API service token. In `triage-provider-secrets`, add **secret files** named `openai_key`, `google_credentials`, `google_read_token`, and `google_write_token`, populated privately with your existing authorized values. Add the desired `OPENAI_MODEL` to API and worker. These files mount at `/etc/secrets/...`; no credentials belong in the Blueprint or repository. [Render secret files](https://render.com/docs/configure-environment-variables#secret-files)
3. In `triage-gateway-secrets`, add a secret file `gateway_auth` with the contents of your locally generated `.secrets/deploy/gateway_auth`. Keep the corresponding `.secrets/deploy/login.json` privately for browser login. Until this file exists and parses, the gateway fails closed.
4. Redeploy the services. The API runs serialized database/checkpoint migrations at startup; worker/scheduler wait for the schema. Initial scheduling is disabled. The internal API origin is derived from the exact Render private hostname/port. The gateway strips the browser's Basic login header before forwarding to Streamlit; Streamlit uses the independent service token for internal API calls.
5. Enable and verify managed database backups/recovery in Render. Restrict external database access (the Blueprint starts with `ipAllowList: []`). Configure provider health/failure notifications and billing alerts. Review service counts and observe memory usage before changing plan sizes.
6. Verify a logged-out request cannot load the app and logged-in access works; verify Background jobs shows both services available. Run one small manual batch before enabling any automatic labels or scheduled triage. Use a single scheduler instance. Keep worker services always running; do not configure them to sleep.

Render holds provider-secret seeds; the per-service OAuth cache is ephemeral and can be rebuilt by refreshing from those seeds after restart. If Google rotates a refresh token, update the managed secret seed from a fresh local authorization. Do not assume ephemeral containers retain refreshed tokens forever. A persistent token store/encryption is future work for multi-user hosting.

Cloud activation still requires your account, connected GitHub repo, secret configuration, cost review, and the platform deployment checks above. The code/configuration do not establish a live hosted service by themselves.

## Alternative: Compose on a VM with hosted PostgreSQL

Use the same Compose files on a Linux VM with Docker, a domain pointing to it, and only ports 80/443 exposed. Store production secrets in your provider's secret manager and materialize only required files with restrictive host permissions. Do not copy the development login/keys to a publicly reachable host; generate production values and configure MFA/network controls at the hosting account.

Set the database connection secret to a hosted PostgreSQL TLS URL using the hidden prompt:

```powershell
.\.venv\Scripts\python.exe scripts/prepare_deployment.py --hosted-database
docker compose --env-file deploy/compose.env -f compose.yaml -f compose.cloud.yaml config --quiet
docker compose --env-file deploy/compose.env -f compose.yaml -f compose.cloud.yaml up --build --wait
```

Set `TRIAGE_PUBLIC_HOST` in `deploy/compose.env` first. Do **not** enable the `local-db` profile for this hosted-database configuration. The cloud override publishes only Caddy's ports 80/443; Caddy obtains/renews certificates for the configured DNS name. Configure DNS/firewall correctly before starting it. A paid VM and hosted PostgreSQL continue running while your computer is off; Docker on your laptop does not. [Caddy public HTTPS prerequisites](https://caddyserver.com/docs/automatic-https)
