# Admin interface and retention (local implementation, not deployed)

The deployed recovery release remains unchanged. This implementation adds an
opt-in admin runtime inside the same image/process: one collector worker owns
Emporia clients, writes and the SQLite ledger; bounded HTTP threads read cached
status and admit a job only when that worker is idle. There is no collection
queue. Normal installations without `VUEGRAF_ADMIN_ENABLED=true` keep the
existing entrypoint and collector loop.

## Enable locally

Set `VUEGRAF_ADMIN_ENABLED=true`, `VUEGRAF_ADMIN_USERNAME` and
`VUEGRAF_ADMIN_PASSWORD` in Docker environment settings. Missing credentials
fail closed. Set `VUEGRAF_ADMIN_BIND=0.0.0.0` inside Docker and publish only
`127.0.0.1:3001:8080` during development. The process default is loopback:8080.
`compose.admin.yaml` is an overlay for the normal integration stack; it requires
local credentials. Both local admin instances use `admin` / `admin` for testing
and are published only on loopback. Never use these credentials in production.

For the production VueGraf service, use this environment fragment when preparing
the reviewed Portainer YAML (not applied yet):

```yaml
environment:
  VUEGRAF_ADMIN_ENABLED: "true"
  VUEGRAF_ADMIN_BIND: "0.0.0.0"
  VUEGRAF_ADMIN_PORT: "8080"
  VUEGRAF_ADMIN_USERNAME: "knight9"
  VUEGRAF_ADMIN_PASSWORD: "" # Supply your password in Portainer YAML before deployment.
```

The empty production password deliberately fails startup until supplied. No
production password is stored in this repository or defaulted to `admin`.

The UI and API share HTTP Basic authentication. Keep it on the trusted LAN;
HTTP transmits credentials/data without encryption. Do not expose it publicly.
State-changing requests require JSON, a same-origin browser request and
`X-Vuegraf-Request: 1`. No permissive CORS, shell, Docker socket, arbitrary Flux
or database-deletion API is provided. HTTP connections are capped at 16.

The UI polls cached status once a second, stops after five minutes without
interaction, shows active/paused and last-checked time, and resumes with Check
now. Stopping polling never stops collection. Manual second refresh accepts
selected circuits and 1–10800 seconds of lookback. All collection kinds share
one admission mutex. Unavailable data is reported as partial, not invented.
Success markers are not advanced after write failure. Job status is in memory
and resets on process restart; the recovery coverage ledger remains durable.

Scheduled second fetches occur after the configured interval from completion;
manual bounded refreshes do not postpone full-account scheduled collection or
advance its full-account boundary. Their writes still share the coverage ledger.
Minute jobs include bounded recovery before releasing the worker. Hour/day
jobs fetch completed intervals; durable recovery fills missed intervals.
Existing `--historydays` collection is tracked as a startup history job. Admin
mode refuses `--dryrun` and `--resetdatabase`.

Recovery status separates retryable gaps from permanently unavailable source
intervals. Five empty responses, with exponential backoff between them, mark
only the affected account/device/channel/metric/resolution interval unavailable
in `/opt/vuegraf/state/coverage.sqlite3`; it then stops consuming recovery
requests. A later successful write supersedes overlapping unavailable coverage.

## AI access and exports

Visit `/api-docs` and `/api/openapi.json` for examples and schemas. Endpoints:

- `GET /api/status`: active job, job results/timestamps, catalog and cached storage/recovery.
- `GET /api/discovery`: circuit IDs, names, units and resolution/query limits.
- `GET /api/coverage`: observed first/last stored samples, cached five minutes;
  these bounds do not assert that every intermediate timestamp exists.
- `POST /api/collect/second`: 202 admitted, 409 busy with current status; no queue.
- `POST /api/export`: CSV for one resolution/field, optional account/device/channel
  filters, raw or bounded aggregation. Output caps: 100000 rows, 32 MiB, 15-second
  query budget, two concurrent query/download slots. Temporary files are removed
  when downloads finish. Input errors/limits fail rather than silently truncate.

Use CSV files with analysis tools rather than sending every sample through chat
context. Missing values are not zeros. Never sum mains plus branches, merged
circuits plus legs, or multiple resolutions. Aggregation means average available
samples; they do not time-weight gaps. Sum is limited to energy/charge. Timestamps
are UTC, stop exclusive; UTC aggregation windows and local/DST daily collection
boundaries are distinct. API date limits do not promise source availability.

## Legacy configuration

Set `legacyEnergyEnabled: false` in collector JSON to stop legacy `energy_usage`
requests/resume queries/writes in normal and historical paths. Shared requests
needed by telemetry remain. Omission defaults to true. Nothing deletes existing
legacy records. The UI reports this setting; it does not silently toggle it.

## Storage observations and notifications

The local admin Compose files run a dedicated `storage-monitor` container using
the same image. It has **only** the InfluxDB data volume mounted read-only and
a writable statistics-output volume. It has no network, credentials, Docker
socket, host paths, or privileged-container mode. Its root user has all Linux
capabilities dropped except `DAC_READ_SEARCH`, permitting directory traversal
despite InfluxDB's private permissions. No ownership or permission changes are
made to InfluxDB. Its root filesystem is read-only; memory/PIDs are bounded.
It only lists directories and reads file metadata, never database contents.

VueGraf stays UID 1012 and receives only the statistics volume read-only, at
`admin.storageSnapshotPath=/storage-stats/disk.json`; it does not mount the
InfluxDB volume. Atomic snapshots are produced every five minutes and rejected
after 15 minutes if the monitor stops. Existing `admin.storagePath` direct-mount
support remains available where directory permissions already allow scanning.
Filesystem capacity refers to the mount's underlying filesystem, not the
container's image. Directory scans are capped at two seconds/100000 files and
clearly marked incomplete. Hard-link/shared-extent accounting may differ from
physical disk usage. Storage and bucket retention refresh every five minutes,
never once per UI poll. Growth estimates need an hour of observations, are
approximate, and restart with the process.

The interface explicitly says no verified hard quota is configured. This release
does not enforce or discover filesystem project quotas. Retention is not a byte
quota; monitor host free space, backups and container logs separately.

Free filesystem space below 15% sets a UI warning. Optional
`VUEGRAF_ALERT_WEBHOOK` sends JSON to an operator-configured HTTP(S) destination,
at most hourly while the warning persists, with a three-second timeout and no
redirects. No webhook is configured by default. Status reports delivery type,
destination hostname and attempted delivery result without exposing URL secrets.
Confirm the destination and payload before enabling it. No email/chat service
is implicitly selected or contacted.

## Resolution-specific retention and migration

Configure a **new** bucket map under `influxDb.telemetryBuckets`:

```json
{"second":"vuegraf-seconds","minute":"vuegraf-minutes","hour":"vuegraf-coarse","day":"vuegraf-coarse"}
```

Targets: seconds 30 days, minutes 730 days, hours/days 1825 days (fixed-duration
two/five-year policies). The original `influxDb.bucket` retains legacy data.
Different retention durations cannot share a bucket or the legacy bucket.
All writes in a batch must succeed before the recovery ledger acknowledges it.
Changing the map creates a separate coverage scope; never reuse a ledger with
a database restored to older data.

1. Back up data/config. Keep the current collector routing unchanged while
   preparing a private proposed config with the new map.
2. Run `python tools/manage_retention.py CONFIG provision` to inspect the plan.
   Add `--apply` to create missing buckets. Existing retention is never shortened;
   a mismatch fails closed. Creating new retention buckets does not change source data.
3. Copy bounded historical windows with `python tools/manage_retention.py CONFIG
   migrate --resolution minute --start UTC_START --stop UTC_STOP --apply`.
   At most 31 days per invocation; source stays intact. The stop must be at least
   five minutes old, and the start must fit target retention with a one-hour
   safety margin. Copy is chunked, idempotent and compares ordered record hashes
   and counts. Without `--apply`, this command verifies existing copies only.
4. Generate separate dashboard files with `ruby scripts/route-grafana-buckets.rb
   CONFIG INPUT_DASHBOARDS NEW_OUTPUT_DIR`. Review the output; the original
   dashboards are not overwritten. Queries choose buckets using the existing
   resolution selector. Test queries for all four resolutions.
5. Plan a short collector pause/cutover and copy the final overlap once it is
   old enough for verification. Review Portainer diffs before changing production
   collector routing and dashboard mounts. Verify real values, counts and coverage.
6. Keep the source bucket and old dashboards for rollback. **No source deletion or
   retention-shortening step is automated.** Any later cleanup requires explicit
   approval. New bucket expiration does not reclaim old source copies.

## Local tests (no production or Emporia requests)

```sh
python -m pytest src/tests -q
docker --context orbstack build -t vuegraf-telemetry:admin-dev .
docker --context orbstack compose -f compose.admin-test.yaml up -d
python tools/verify_admin.py
```

The disposable fixture is at `http://127.0.0.1:18080`, username `admin`, password
`admin`. It uses fake Emporia and its own `admin-test` InfluxDB,
buckets and named volumes. It cannot access production or real Emporia credentials.
`tools/verify_retention.py` runs on its Docker network and checks copy/hash
verification and replay against seeded dummy records. `tools/test_admin_polling.js`
uses an isolated fake DOM/clock to test five-minute inactivity without waiting.
Stop with `docker --context orbstack compose -f compose.admin-test.yaml stop`.
Do not mistake fixture readings for household data.

For real-source local testing, `ruby scripts/prepare-local-stack.rb --admin`
creates ignored `int/admin.env` (mode 0600) with local-only `admin` / `admin`
credentials, the existing LOCAL database/Emporia inputs, legacy recording off,
and a one-hour recovery lookback. Existing private source files are preserved.
Use it with `compose.integration.yaml` plus `compose.admin.yaml` and
`--profile collector`. This overlay uses port 3001 and a separate local admin
recovery ledger; it does not alter the old recovery ledger or production.
`tools/verify_admin_live.py` performs exactly one 60-second manual refresh on
one discovered branch, then checks its CSV output. It never points to production.
Stop the local collector after acceptance to avoid duplicate Emporia polling.

Acceptance on 2026-09-20: disposable integration passed auth/CSRF/busy/success,
CSV values and retention routing; migration copied and hash-verified 120 seeded
fields without deleting source data, including idempotent replay; all 160
generated example dashboard/variable queries executed against InfluxDB. The
isolated clock test passed polling pause/resume and duplicate-poll prevention.
The real-source local check succeeded with 300 telemetry values and all 60
power samples exported; all six existing Grafana query checks passed with
legacy recording disabled. The real local collector was stopped afterward.

Final local rebuild passed 297 Python tests, polling tests, disposable API
acceptance, migration replay, and 160 dashboard queries. Browser verification
confirmed manual refresh changes from running to successful/idle without moving
the scheduled second run. The isolated storage monitor resolves private-directory
access without changing InfluxDB permissions or elevating the collector.

Storage isolation was verified against both disposable and real-data local
stacks using `python tools/verify_storage_monitor.py` (append
`vuegraf-integration` for the latter). Both scans completed with nonzero database
size. The latest real-source manual 60-second refresh returned 298 values and
59 power samples; it correctly reported **partial**, rather than inventing the
missing source sample. All six real Grafana queries passed afterward. The local
real-data admin was verified at port 3001 and then stopped; its credentials
are the `VUEGRAF_ADMIN_USERNAME/PASSWORD` entries in private `int/admin.env`.
Production remains untouched. Stop the local collector and monitor after review:

```sh
docker --context orbstack compose --env-file int/admin.env \
  -f compose.integration.yaml -f compose.admin.yaml stop vuegraf storage-monitor
```
