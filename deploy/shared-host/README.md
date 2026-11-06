# Shared Linux host deployment

Target: Ubuntu 22.04 amd64, 8 vCPU, 16 GB RAM, Minecraft Java and Xray/Happ alongside
mnogobase. CPU PyTorch is installed separately in the server copy of the project.
This directory prepares PostgreSQL/Qdrant/Neo4j, CPU model containers and application
configuration, grouped systemd services, nginx HTTPS on the public IP, and a cold-backup
helper with an isolated recovery check. The backup helper still needs a VPS runtime check;
Minecraft/VPN remain separate deployment steps.

Prerequisites: Docker with Compose, application sources and Python 3.12 at
`/srv/mnogobase/app`, system account/group `mnogobase`, and a mounted 60,000,000,000-byte
ext4 image at `/srv/mnogobase`. The mount is checked before setup and Compose commands.

On the server, from `/srv/mnogobase/app`:

```bash
sudo .venv/bin/python deploy/shared-host/prepare-storage.py
sudo bash deploy/shared-host/storage config --quiet
sudo bash deploy/shared-host/storage up -d
sudo bash deploy/shared-host/storage ps
sudo bash deploy/shared-host/storage logs --tail 40 postgres qdrant neo4j
```

The setup creates three bind-backed named volumes inside the mounted filesystem.
Existing credentials are preserved on repeated setup; never delete or regenerate the
environment file when databases already contain data. Credentials are stored in
`/srv/mnogobase/config/storage.env`, root:mnogobase, 0640. Do not paste this file or
unredacted `docker compose config` output into chat. `config --quiet` is safe.
Later systemd units must load this same file for DATABASE_URL and NEO4J_PASSWORD.

Docker ports remain on localhost. Limits: PostgreSQL 768 MiB/0.75 CPU, Qdrant 1 GiB/1 CPU,
Neo4j 2 GiB/2 CPU (heap up to 768 MiB, page cache 256 MiB), total RAM ceilings 3.75 GiB.
These are pilot ceilings, not reserved resources or a guarantee of workload capacity.
Swap is disabled for these containers. The host's 8 GiB swap remains available for other
services. Test ingestion/analysis and gameplay together before raising concurrency.

Shared Docker images, container writable layers/logs and system journals remain outside
the 60 GB data filesystem. The base Compose rotates container logs. The filesystem cap
covers only paths actually stored under `/srv/mnogobase`; move future model caches there.
A mounted filesystem must be required by future systemd units. Bind-source directories
must not be recreated automatically on the root filesystem when the mount is absent.
The Compose project name is `mnogobase-server`, separate from local development projects.

Recovery: `sudo bash deploy/shared-host/storage stop` stops containers without removing
data. Do not use `down -v` or volume prune for routine updates. Changing a volume's driver
options requires a planned migration; never silently reuse a pre-existing differently
configured Docker volume.

## Native model CLI and application configuration stage

User decision: switch between **mnogobase + VPN** and **Minecraft + VPN**. Vision remains
on by default. Intended RAM ceilings total 13 GiB: stores 3.75, models 7 (embedding 2,
reranker 2, Vision 3), and Python API/worker/monitor together 2.25 GiB through
`mnogobase-app.slice`. Install the slice and services in the systemd stage below;
until then the aggregate Python ceiling is not enforced. These are ceilings, not reserved
RAM; validate real workload peaks. VPN remains independent.

Models use the existing `mnogobase models up/down/status/logs` implementation and original
`docker-compose.models.yml`. The new optional setting merges a server override into every
Docker lifecycle command without changing local defaults:

```yaml
serving:
  compose_override: /srv/mnogobase/app/deploy/shared-host/models.override.yml
```

Environment override: `MNOGOBASE_SERVING__COMPOSE_OVERRIDE`. There is no separate model
runner. The server override changes CPU resource limits and binds `llama_models` into
`/srv/mnogobase/volumes/models`. Compose project remains `mnogobase-models`. Model identities,
GGUF quants, templates, CPU validation, sequential startup, retries, readiness and logs use
the original code. Context is 4096, embedding batches 4, threads/batch threads 2, slots 1.
All three roles start by default; LLM stays on the configured external API.

Synchronize **src/** and **deploy/** from the Mac; keep server CPU `pyproject.toml`,
`uv.lock`, `.venv` and config files. For the original full-project copy, exclusions of
runtime directories must be root-anchored (`/wiki/`, not `wiki/`): the latter also excludes
`src/mnogobase/wiki/`. Copying all of src restores that module if omitted previously.

On the server:

```bash
cd /srv/mnogobase/app
sudo .venv/bin/python deploy/shared-host/prepare_app.py
sudo -u mnogobase bash deploy/shared-host/run --help
sudo bash deploy/shared-host/run models up
sudo bash deploy/shared-host/run models status
```

`prepare_app.py` is one-time infrastructure setup: create writable cache/data directories,
create a separate config, and grant write access only to RapidOCR's library-managed
`.venv/.../rapidocr/models` checkpoint directory. Other package code remains read-only to
the service account. Rerun preparation after replacing the venv. Existing config is preserved;
an incompatible config requires explicit operator edits. No API credentials are generated.

`run` only checks the mount, loads private environment files, routes caches/temp files and
executes the native CLI with server config. It exports HF_HOME, DOCLING_CACHE_DIR,
FASTEMBED_CACHE_PATH and XDG_CACHE_HOME before importing libraries. Root operator logs are
separate from service logs to avoid root-owned files blocking application logging.

```bash
sudo -u mnogobase bash deploy/shared-host/run service init
sudo bash deploy/shared-host/run models logs vision
```

Model health readiness does not spend external LLM calls. Initial startup downloads public
HF weights. CPU image/build and model aliases come from the original Compose/config. The
internal `--host 0.0.0.0` allows container port forwarding; published ports remain localhost.
Do not launch models without the server config, which would use development cache/limits.

Python services run native `api serve`, `service work`, `service monitor`, grouped by a
systemd target for one-command start/stop. Accounts, mode switching, backups
and Minecraft/VPN setup remain outstanding. API key entry is a separate secure step;
never paste keys into chat or command arguments.

## Grouped systemd services

Prerequisites: models ready, private `/srv/mnogobase/config/api.env` containing the existing
`LLM_API_KEY`, successful `service init` and `doctor` as the service account. No application
logic or model runner is duplicated: units call the same wrappers and native CLI above.

After synchronizing `deploy/` to the server (keep the server CPU lockfile and venv), run
from `/srv/mnogobase/app`. These unit names are reserved for this deployment. For updates,
first use the complete stop command below and retain a copy of any operator-edited
installed units.

```bash
sudo install -o root -g root -m 644 deploy/shared-host/systemd/* \
  deploy/shared-host/mnogobase-app.slice /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/mnogobase*.service \
  /etc/systemd/system/mnogobase.target /etc/systemd/system/mnogobase-app.slice
sudo systemctl daemon-reload
sudo systemctl start mnogobase.target
```

`systemd-analyze verify` must pass before startup; it runs on the Ubuntu server, not the
development Mac. Applying new Compose settings may recreate the existing containers;
named persistent volumes and private environment files are retained. Target startup waits
for storage readiness, the native model CLI's readiness check, and API `/health`. Worker
and monitor run in the background; inspect their individual states and logs as well.

```bash
sudo systemctl --no-pager --full status mnogobase.target \
  mnogobase-api.service mnogobase-worker.service mnogobase-monitor.service
curl -fsS http://127.0.0.1:8080/health
sudo systemctl show mnogobase-app.slice -p MemoryCurrent -p MemoryHigh -p MemoryMax -p MemorySwapMax
sudo -u mnogobase bash deploy/shared-host/run service alerts
```

Expected API body: `{"status":"ok"}`. API, worker and monitor should be `active (running)`;
the storage/model units are `active (exited)` by design. The slice MemoryMax is
2,415,919,104 bytes (2304 MiB), MemoryHigh 2 GiB and MemorySwapMax 0. Models/stores have
separate container ceilings. Manual operator CLI commands outside systemd are outside
the Python slice. This gives the previously selected 13 GiB total runtime service ceiling,
not a reservation or a workload-capacity guarantee.

Useful diagnostics (never include private env-file contents in shared logs):

```bash
sudo journalctl -u mnogobase-api -u mnogobase-worker -u mnogobase-monitor -n 80 --no-pager
sudo journalctl -u mnogobase-storage -u mnogobase-models -n 80 --no-pager
sudo bash deploy/shared-host/storage ps
sudo bash deploy/shared-host/run models status
```

Lifecycle after initial validation:

```bash
sudo systemctl stop mnogobase.target \
  mnogobase-api.service mnogobase-monitor.service mnogobase-worker.service \
  mnogobase-models.service mnogobase-storage.service
sudo systemctl start mnogobase.target
sudo systemctl restart mnogobase.target
```

Stop/restart propagates through `PartOf=`. A target-only stop returns when its own job
finishes, while propagated service stop jobs may still be running. For a complete stop
before switching modes or changing files, explicitly name the target and all five services
as above: `systemctl` then waits for all requested jobs. Do not immediately start the target
while its children are stopping; the default job replacement policy can replace pending
stop jobs. `--wait` is not a stop option in systemd249. After stopping, check all six units,
`systemctl list-jobs` and running containers; command failure is not a successful stop.
Ordering stops the API before the worker,
Python processes before models, then models before stores. API/worker have up to 15 minutes
to stop before forced termination. The worker receives SIGINT; interruption or timeout
can leave a job/stage for the existing recovery mechanism on the next start. This is not
a promise to finish arbitrarily long documents during a mode switch. Normal stop retains
volumes, documents, model weights and credentials. Never use `down -v` or prune for switching.

Server overrides use `on-failure:5` container restart policies. Docker handles bounded
container crash retries; oneshot units control the stack lifecycle, not continuous container
health monitoring. Docker daemon restart does not independently start these containers,
so it cannot race the required mount on boot. After a Docker daemon restart, explicitly
run `sudo systemctl restart mnogobase.target` if the mnogobase mode should be active.
If startup fails partway through, unit cleanup stops its own partially started containers;
run the complete stop command above to release remaining successfully started pieces
and wait for their shutdown.

Verify a stop/start cycle and inspect services before enabling automatic boot:

```bash
sudo systemctl enable mnogobase.target
```

The target and each service require `/srv/mnogobase` to be mounted. Application processes
run as `mnogobase`, with read-only system files, writable data/runtime caches, and the venv
subtree permitted for RapidOCR's model cache; existing ownership still prevents changes to
package code. VPN is independent. Minecraft and mutual mode-switch units are a later stage;
this target by itself does not yet stop an independently started Minecraft server.

## nginx and HTTPS on the public IP

Prepared files are under `nginx/`; this stage is not installed automatically by the
systemd target. Use nginx independently so ACME renewal continues in Minecraft mode.
The current profile is for `152.234.234.206`; changing the IP also requires changing
server names, redirect and certificate paths. HTTPS uses TCP443; reserve a separate
port for the later VPN. HTTP TCP80 must remain publicly reachable for certificate renewal.

Before installation, verify target stop/start and inspect listeners/firewall:

```bash
sudo systemctl stop mnogobase.target \
  mnogobase-api.service mnogobase-monitor.service mnogobase-worker.service \
  mnogobase-models.service mnogobase-storage.service
sudo systemctl is-active mnogobase.target mnogobase-api mnogobase-monitor \
  mnogobase-worker mnogobase-models mnogobase-storage
sudo docker ps
sudo systemctl start mnogobase.target
sudo systemctl is-active mnogobase.target mnogobase-api mnogobase-worker mnogobase-monitor
curl -fsS http://127.0.0.1:8080/health
sudo ss -lntp '( sport = :80 or sport = :443 )'
sudo ufw status verbose
command -v nginx
```

Expected after stop: all six units are inactive and no mnogobase containers are running.
`is-active` returning nonzero for the expected inactive state is normal. After start all
four units must be active and health must return ok. This is a manual first-install guide:
if any installation/check fails, stop there and inspect it before continuing.

If nginx or an existing listener/site already exists, inspect its configuration locally
before installing this profile. Do not paste an unredacted `nginx -T` into chat. IP TLS
clients normally omit SNI, so the final config explicitly owns the default443 certificate;
never silently replace another site's default. `nginx -t` must pass before each reload.
These instructions install only our named configuration files and retain unrelated sites.
Do not rerun HTTP bootstrap over an already working HTTPS site during updates.

User also selected UFW installation on this VPS. If it is absent, first set up host
filtering with SSH22 allowed before activation; keep the current SSH session open and
test a second SSH connection after enabling. Do not reset existing firewall rules.

```bash
sudo apt-get update
sudo apt-get install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
```

Confirm the built-in enable prompt in the server terminal after the allow commands
succeed. From a second Mac terminal, test `ssh admin@152.234.234.206` before closing the
first connection. Once host firewall checks pass, start the stopped mnogobase target
and verify service states and local health again. Later MC/VPN ports get their own
deployment rules. Docker-published public ports can bypass UFW; retain the existing
localhost-only database/model bindings and do not disable Docker's own firewall rules.

After synchronizing `deploy/`, install bootstrap configuration on the server:

```bash
sudo apt-get update
sudo apt-get install -y nginx
sudo install -d -o root -g root -m 755 /var/www/letsencrypt/.well-known/acme-challenge
sudo install -o root -g root -m 644 deploy/shared-host/nginx/mnogobase-http-settings.conf /etc/nginx/conf.d/mnogobase.conf
sudo install -o root -g root -m 644 deploy/shared-host/nginx/mnogobase-proxy.conf /etc/nginx/snippets/mnogobase-proxy.conf
sudo install -o root -g root -m 644 deploy/shared-host/nginx/mnogobase-http.conf /etc/nginx/sites-available/mnogobase
sudo ln -s /etc/nginx/sites-available/mnogobase /etc/nginx/sites-enabled/mnogobase
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

If UFW is already active, allow SSH before the nginx profile; keep the current SSH session
open. Do not enable an inactive firewall as an incidental part of this step. A provider's
external firewall may separately need TCP80/443 rules.

```bash
sudo ufw allow 22/tcp
sudo ufw allow 'Nginx Full'
```

Verify the ACME webroot locally and from the Mac before requesting a certificate:

```bash
printf 'mnogobase-acme-ok\n' | sudo tee /var/www/letsencrypt/.well-known/acme-challenge/mnogobase-probe > /dev/null
curl -fsS -H 'Host: 152.234.234.206' http://127.0.0.1/.well-known/acme-challenge/mnogobase-probe
```

From the Mac: `curl --connect-timeout 10 -fsS http://152.234.234.206/.well-known/acme-challenge/mnogobase-probe`.
Expected body is `mnogobase-acme-ok`. Other HTTP paths in bootstrap return404, not the API.

Use a current official Certbot snap (snapd must be installed and working), and check the
version is at least5.4 for IP+webroot support. On this fresh VPS do not install the old
Ubuntu22.04 apt Certbot alongside it. Certbot will ask for account information and terms
interactively in the server terminal; no credentials belong in chat.

```bash
sudo snap install --classic certbot
sudo /snap/bin/certbot --version
sudo install -d -o root -g root -m 755 /etc/letsencrypt/renewal-hooks/deploy
sudo install -o root -g root -m 755 deploy/shared-host/nginx/reload-nginx /etc/letsencrypt/renewal-hooks/deploy/mnogobase-nginx
sudo /snap/bin/certbot certonly --webroot --webroot-path /var/www/letsencrypt \
  --ip-address 152.234.234.206 --preferred-profile shortlived --cert-name 152.234.234.206
```

After successful trusted certificate issuance, replace only our bootstrap site:

```bash
sudo install -o root -g root -m 644 deploy/shared-host/nginx/mnogobase-https.conf /etc/nginx/sites-available/mnogobase
sudo nginx -t
sudo systemctl reload nginx
curl -fsS https://152.234.234.206/health
curl -sS -o /dev/null -w '%{http_code}\n' https://152.234.234.206/api/auth/me
sudo /snap/bin/certbot renew --cert-name 152.234.234.206 --dry-run --run-deploy-hooks
sudo systemctl list-timers --all 'snap.certbot.*'
sudo /snap/bin/certbot certificates
sudo rm /var/www/letsencrypt/.well-known/acme-challenge/mnogobase-probe
```

Repeat HTTPS health and unauthenticated `/api/auth/me` tests from the Mac: expect ok and401,
with normal certificate verification (no `-k`). IP certificates last six days; verify an
enabled renewal timer with a future run and a successful renewal dry-run including the
nginx deploy hook. Dry-run uses staging without replacing the trusted certificate.
The hook uses `nginx -t -q`: successful config tests are quiet, while actual errors still
fail before reload. Plain `nginx -t` writes success messages to stderr, which Certbot labels
as hook "error output" even when the hook succeeds; that label alone is not an exit failure.
If the HTTPS config check fails, restore `mnogobase-http.conf` to our site, run `nginx -t`
and reload; internal API remains available through SSH while the TLS issue is resolved.

The proxy preserves bearer authentication, overwrites forwarded client headers, disables
response caching and automatic upstream retries, and streams raw uploads without nginx
request-body tempfiles. Its50MiB body ceiling matches the current service default; update
both if changed. Login protection is5requests/minute per source IP with burst5, not a team
quota. The3600s upstream timeout measures idle time between reads, not total operation time.
Custom access logs omit query strings; error logs can include request URIs. Nginx logs,
Certbot state and certificates live on the host filesystem outside the60GB project cap.
This is the backend API; it does not add a website UI. Accounts and real document/API tests
follow separately.

References: [Let's Encrypt IP/webroot instructions](https://letsencrypt.org/2026/03/11/shorter-certs-certbot.html),
[Certbot snap installation](https://certbot.eff.org/instructions?ws=other&os=snap),
[Certbot renewal guide](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates),
[nginx proxy directives](https://nginx.org/en/docs/http/ngx_http_proxy_module.html).

## Application users and two-team verification

Use native operator commands as the service account. These are application accounts,
separate from the SSH account; choose distinct passwords and enter them only at the
hidden prompts. Run each interactive user-creation command separately. User commands
print only the username/public UUID, not a password/session token. Team creation is not
idempotent: do not repeat it if a team was already created successfully.

```bash
sudo systemctl enable mnogobase.target
sudo -u mnogobase bash /srv/mnogobase/app/deploy/shared-host/run user create admin
sudo -u mnogobase bash /srv/mnogobase/app/deploy/shared-host/run user create tester
sudo -u mnogobase bash /srv/mnogobase/app/deploy/shared-host/run team create TeamA
sudo -u mnogobase bash /srv/mnogobase/app/deploy/shared-host/run team create TeamB
```

Take the returned identifiers and replace the placeholders below with the exact UUIDs:

```bash
sudo -u mnogobase bash /srv/mnogobase/app/deploy/shared-host/run team add-member TEAM_A_ID ADMIN_ID --role admin
sudo -u mnogobase bash /srv/mnogobase/app/deploy/shared-host/run team add-member TEAM_B_ID ADMIN_ID --role admin
sudo -u mnogobase bash /srv/mnogobase/app/deploy/shared-host/run team add-member TEAM_B_ID TESTER_ID --role member
```

This prepares the demo acceptance setup, not a hard limit on users or team count. Verify
through the native HTTP client at `https://152.234.234.206`: admin should list both teams,
tester onlyTeamB; access to TeamA as tester must return403. Use different client credential
files for the two users to avoid replacing one session with the other. Do not paste these
files or passwords into chat. User/team setup makes no LLM calls. Loading test documents
and asking questions do call the configured external LLM and must be checked separately,
including background jobs, citations, usage records, per-team sharing and isolation.

Small synthetic acceptance documents are provided in `smoke/`: team-a.md and team-b.md
use the same Project Atlas entity with distinct cities/dates/markers; common.md is the
identical-file ownership fixture to upload independently into both teams. Upload through
the native HTTP client from the Mac. It queues real server work using the configured
external LLM, so state the API/model impact before running. Expected answers must differ
by team (Helsinki vs Porto); source IDs/citations should belong to that team's corpus.
Verify sharing within TeamB using tester and denial for TeamA, then use common.md to check
independent ownership. Do not interpret prepared fixture files as completed runtime tests.

## Cold backup and isolated recovery check

Run through the server venv as root, from any working directory:

```bash
sudo /srv/mnogobase/app/.venv/bin/python /srv/mnogobase/app/deploy/shared-host/backup.py create --check
```

This briefly stops **all six explicit Mnogobase units**, archives the offline data,
starts isolated recovery databases, checks them, removes the temporary containers and
then restores the original service state. nginx/Certbot remain running. There are no
model servers or LLM calls in the recovery check. An initially stopped stack stays
stopped; a partial/failed stack is rejected before any stop/start operation. Normal
backup/check failures still restart a previously running stack. If recovery-container
cleanup fails, it retains their copied bind files and leaves production stopped to
avoid running two database stacks unexpectedly. Inspect the reported recovery project
before restarting production. SIGKILL/power loss cannot run the restart handler.

The printed timestamped directory under `/var/backups/mnogobase/` contains:

- `backup.tar.gz`: `project/config`, `project/data`, the complete three database
  volume directories, application sources and the **server CPU** pyproject/lock.
- `host/etc/...` inside that archive: installed nginx, Let's Encrypt configuration
  and keys, Mnogobase units/slice and a reference copy of fstab. Absolute host-config
  symlinks are rebased inside the archived host tree for safe isolated extraction.
- `manifest.json`: SHA-256, timestamp, exact local database image IDs and available
  registry digest references. The manifest is written last; a directory without it
  is an incomplete backup. The checksum detects corruption, not malicious replacement.

Downloaded model weights, caches, toolchains, `.venv`, Git internals, temporary files
and old deployment backups are excluded. Source `src/mnogobase/wiki` is retained;
only the application-root generated wiki is excluded. Directory permissions are0700,
archive/manifest0600. Backups contain secrets and source documents: retain those modes,
transfer only through SSH, and never commit them. Backups are outside the60GB project
filesystem and consume root-filesystem space. Existing backups are never overwritten
or automatically deleted. Scheduling/retention and offsite transfer are not automated.

Recovery uses a **fresh private directory** under `/srv/mnogobase/.restore-check-*`
and standalone `restore-test.yml` in a unique Compose project. It uses copied volume
paths, exact installed image IDs (`pull_policy: never`), an internal network, no API
or worker, and **no published ports**. The Linux host reads Qdrant via its container IP
after validating its recovery network, internal flag, project label and absence of
host port mappings. Direct host-to-container access on internal bridges is
[documented by Docker](https://docs.docker.com/reference/cli/docker/network/create/#network-internal-mode---internal).
This avoids host-port publishing on an internal-only network. Limits total3.75GiB,
with no container swap. It reads all application SQL table counts, checks uploaded
files against their SQL document IDs, reads Neo4j label counts and Qdrant collection
point counts. It then removes the recovery containers, their anonymous Docker volumes
and network before deleting copied files. This isolated project uses `down --volumes`;
do not use that command against the production project.

Expected completion includes `RECOVERY CHECK OK` and, if initially running, `All six
Mnogobase units are active.` Then verify HTTPS `/health`. This establishes extracted
files and database startup/reads, not a second full API deployment, graph-plugin
availability or a successful offsite disaster recovery. Compare recovered counts with
the known team corpus: in the current acceptance setup there are three retained uploads
(TeamA Atlas, TeamB Atlas and TeamB common).

To test a retained backup again, use its **exact printed directory**:

```bash
sudo /srv/mnogobase/app/.venv/bin/python /srv/mnogobase/app/deploy/shared-host/backup.py check /var/backups/mnogobase/TIMESTAMP
```

Keep a copy of the **whole directory** (archive plus manifest) on another machine.
After a successful check, export a private copy for the SSH account. Set the name
to the exact printed backup directory (the example below is the verified pilot copy).
On the server:

```bash
MB_BACKUP=20261105T222325Z-cb3e4b4f
sudo install -d -o admin -g admin -m700 /home/admin/mnogobase-backups "/home/admin/mnogobase-backups/$MB_BACKUP"
sudo install -o admin -g admin -m600 "/var/backups/mnogobase/$MB_BACKUP/backup.tar.gz" "/var/backups/mnogobase/$MB_BACKUP/manifest.json" "/home/admin/mnogobase-backups/$MB_BACKUP/"
```

On the Mac, from the repository root:

```bash
MB_BACKUP=20261105T222325Z-cb3e4b4f
mkdir -p .mnogobase/backups
chmod 700 .mnogobase/backups
scp -rp "admin@152.234.234.206:/home/admin/mnogobase-backups/$MB_BACKUP" .mnogobase/backups/
.venv/bin/python -c 'import json,sys; from pathlib import Path; p=Path(sys.argv[1]); print(json.loads((p/"manifest.json").read_text())["sha256"]+"  "+str(p/"backup.tar.gz"))' ".mnogobase/backups/$MB_BACKUP" | shasum -a 256 -c -
```

Expected: the copied archive path followed by `OK`. The local destination is gitignored;
do not move this secret-bearing archive into tracked source directories. The original
root-owned backup is retained. Do this before the reboot test: an archive on the same
VPS does not survive loss of that VPS. A fresh server also needs the existing account/mount/Docker/Python
prerequisites, rebuilt venv and downloadable models. Pull database images using the
saved registry digests for the same Linux amd64 platform, verify their local IDs
against the manifest, and repeat the isolated check before planning the live cutover.
If digest references are unavailable, export the installed database images separately
with `docker image save`; the archive does not contain image layers. Preserve the
server CPU dependency files when recreating the venv. Host fstab/certificates are
reference material: adapt mounts and renew/reissue expired certificates for the new
host. The helper deliberately offers no operation that overwrites live data; a real
cutover requires an empty mounted destination, stopped services and a retained rollback
copy, followed by native startup and the same two-team acceptance checks.

The cold-copy requirement follows PostgreSQL's
[file-system backup rules](https://www.postgresql.org/docs/18/backup-file.html).
Neo4j's [Cypher Shell documentation](https://neo4j.com/docs/operations-manual/current/cypher-shell/)
describes the environment-based credentials used by the recovery query; passwords are
not passed in arguments or printed. Unit/static checks run on the Mac; the actual
Linux recovery check remains a VPS step.

## Verification scope

VPS evidence so far: all storage checks and all three native model readiness checks passed;
`service init` and all `doctor` checks passed, including external API model listing and
APOC/GDS. The initial empty index was expected. Idle memory is not an ingestion or
gameplay benchmark.
Systemd target/API/worker/monitor are active on VPS and /health returns ok; the shared
Python MemoryMax2304MiB/MemorySwapMax0 are applied. Full stop with all six explicit units
and a subsequent start/health check passed. UFW is active with TCP22/80/443 allowed for
IPv4/IPv6. nginx syntax passed and its ACME probe returned the expected body from an
external HTTP check. Certbot issued the trusted IP certificate at the configured live paths;
the HTTPS site is active. External requests with normal certificate verification returned
200/ok for health and401 for anonymous auth/me. Certbot renewal dry-run including deploy
hooks succeeded and snap.certbot.renew.timer shows a future scheduled run. Native client
admin login lists TeamA+TeamB; tester lists onlyTeamB and gets403 for TeamA. The two
synthetic team documents completed ingestion on VPS with status done, one attempt and
no errors; repeating TeamA's upload returned its existing completed job. Native RAG
answers returned the correct distinct Atlas facts and cited each team's own document:
TeamA/admin Helsinki and TeamB/tester Porto, confirming sharing within TeamB for this
fixture. Persistent usage records show four completed external LLM calls per team
(answer/extract/query/resolve), with known token counts and no recorded errors or
incomplete calls. Monetary tariff configuration is deferred by the user; these calls
remain explicitly unpriced. The subsequent active-alert list was empty; Python used
about1.47GiB under its2.25GiB cap, all six containers were below their limits and the
host reported about10GiB available. These samples follow a tiny corpus and do not
establish peak-memory capacity. The identical common.md fixture also completed ingest
independently in both teams, each as job2 with one attempt and no errors. TeamA's
subsequent remove job3 also completed with one attempt and no errors. Post-removal
document lists and RAG searches confirm the common file is absent in TeamA and
retained/searchable by tester in TeamB. One TeamA search returned a transient409;
its repeated read succeeded. Other retrieval modes, Vision inference and reboot must
still be checked before declaring migration complete.

The first VPS cold-backup run created
`/var/backups/mnogobase/20261105T222325Z-cb3e4b4f`: recovered PostgreSQL retained two
users, two teams, three memberships, two sessions and twelve usage rows; all three
uploaded files matched their document IDs. Neo4j retained one document in TeamA and
two in TeamB, with their chunks/entities. The initial Qdrant published-port check
failed on the internal-only network; cleanup and restart completed and all six
production units became active. The helper now uses the validated internal container
IP with zero published ports. The repeat check against that retained archive passed
on the VPS: Qdrant recovered TeamA1chunk/3entities and TeamB2chunks/6entities (both
wiki collections empty), matching the SQL and graph document counts. All three source
files passed hash verification, `RECOVERY CHECK OK` printed, temporary resources were
removed and all six production units restarted active. This confirms the isolated
file/database recovery check. The archive and manifest were subsequently copied to
the Mac under `.mnogobase/backups/20261105T222325Z-cb3e4b4f/`; the local archive SHA-256
check returned `OK`. Actual reboot remains pending.
