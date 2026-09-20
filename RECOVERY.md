# Telemetry gap recovery (opt-in)

The new recovery path supplements existing collection; it does not replace the
scheduler or implement the admin API. It is disabled unless explicitly enabled.
No production configuration is changed by this feature branch.

```json
"telemetry": {
  "enabled": true,
  "metrics": ["energy", "current", "voltage"],
  "recovery": {
    "enabled": true,
    "statePath": "/opt/vuegraf/state/coverage.sqlite3",
    "initialLookbackSecs": 3600,
    "maxRequestsPerCycle": 12,
    "pauseSecs": 0.2
  }
}
```

## Persistence and correctness

Mount a persistent writable directory at `/opt/vuegraf/state`, owned by the
container UID/GID (1012). The local integration Compose file supplies a named
volume; the configuration mount remains read-only. Production would require a
separately reviewed mount and configuration change. Never put the ledger in
`/tmp`, the read-only config directory, or the image's ephemeral writable layer.
The SQLite file contains coverage metadata, not credentials or readings.

The collector records covered intervals only after all configured databases
acknowledge a batch. Each interval is scoped to the destination configuration,
account, device, channel, metric, and resolution. Energy/current coverage also
requires their derived power/current fields. Newer readings do not hide older
holes. Historical imports update the same coverage ledger.

A failed/partial database write leaves the interval eligible for replay. If a
process dies after the database write but before the ledger commit, replay may
rewrite the same timestamp/series identities; it must not invent readings.
The ledger is not a database backup: retain it alongside the data, and discard
it when restoring an older database or deleting data. Otherwise it can claim
coverage that the restored database no longer has. Recovery refuses to start
with `--resetdatabase`; dry runs do not create or advance the ledger.

## Scope and request limits

On first seeing a stream, repair starts at `initialLookbackSecs` before the
current completed boundary (default one hour). Existing database history is
not assumed complete or scanned in full. To repair older pre-existing minute
gaps, choose up to 604800 seconds (seven days) when starting a fresh ledger;
increasing this setting does not rewind streams already registered. Plan the
extra API load before doing this. Keep an existing ledger across ordinary
restarts so outages since its recorded origin remain recoverable.

Each cycle runs normal collection/writes first, then at most 12 additional
channel/metric chart requests, sequentially, with a 0.2-second pause. Fair
selection rotates among streams; a backlog can take multiple cycles to clear.
Weighted round-robin reserves turns for all resolutions, favoring minutes over
coarse startup repairs. Multiple holes in a stream can be combined into one
bounded request, replaying covered samples between them without crossing a
source interval still under backoff. This avoids an ever-growing backlog when
normal polling intermittently skips a minute.

Requests cover no more than one hour of seconds, 12 hours of minutes, or 20 days
of hourly/daily data. Daily boundaries honor the configured timezone and DST.

Second repair is activated by the existing detail schedule. Once activated,
unfinished work can continue in subsequent minute cycles up to that scheduled
target; it does not turn on continuous second polling. Hour/day repair is only
enabled when the corresponding detailed collection settings are enabled.

Missing/unsupported source readings remain unresolved, with retries starting
after five minutes and backing off to one hour. Authentication, rate-limit,
transport, or write failures stop repair for the cycle and impose a five-minute
repair cooldown. This cooldown does not alter the pre-existing normal collector.
Recent intervals continue to be eligible even when older source readings are
unavailable. Synthetic balance/total/from-grid/to-grid channels are excluded
from chart repair, as in the historical import.

Second holes older than three hours and minute holes older than seven days
expire from the repair window with an explicit missing-duration log/counter.
Local InfluxDB retention does not extend these source-history windows. Hourly
and daily coverage is retained from the registered origin; source availability
still determines whether a hole can be filled.

## Limitations and verification

The main collector still has its original sequential loop and post-cycle wait.
Repair adds bounded work; it does not promise exact wall-clock minute polling
or data Emporia never supplied. Legacy recording and the proposed retention
changes are separate TODO items and are not enabled/disabled by recovery.

Offline tests use temporary ledgers, fake Emporia responses, and mocked writes.
Run `python -m pytest src/tests -q` in the development environment. Live local
verification should use the isolated local database, never production settings.
Watch `Recovering telemetry`, `Telemetry recovery incomplete`, and
`Telemetry recovery interrupted` logs for progress. The status API/UI will later
surface covered intervals, deferred holes, and expired coverage explicitly.

`tools/verify_recovery.py` is the real-database probe. It uses a hard-coded
test-only InfluxDB hostname (`vuegraf-recovery-influx-test`), organization/bucket
`recovery-test`, and a dummy token from `RECOVERY_TEST_TOKEN`; it never contacts
Emporia. Run phases `1` and `2` in separate image containers with the same
`/opt/vuegraf/state` volume and disposable database. Phase 1 writes two separated
snapshots and repairs five missing minutes. Phase 2 restarts and repairs three
more. Both query real stored values and check that a second repair pass issues
no additional source requests. Start with an empty test database/ledger, and
remove only those disposable resources afterward. Never point this probe at a
real database or share the test ledger with the real collector.

For live **local-only** acceptance tests, `ruby scripts/prepare-local-stack.rb
--recovery-test` creates separate ignored `int/recovery.env` inputs with recovery
enabled and a one-minute initial lookback, preserving `int/stack.env` and the
original inputs. Use a local Compose override selecting the recovery image.
`tools/audit_recovery.py`, run via local `docker exec ... python -`, reads
timestamps and ledger counts without modifying data. It accepts `--start`,
`--stop`, `--detail`, and an optional `--baseline-start` so wholly absent series
are not accidentally omitted from a gap check. `--show-missing` includes channel
identifiers and should be kept private. Day audits use fixed UTC durations and
are not a substitute for the DST-aware daily recovery tests.

## Local acceptance results (2026-09-19)

Tested the recovery image on OrbStack with real Emporia reads and local-only
InfluxDB/Grafana; no production access or data deletion. The live test exposed
startup starvation of minute repairs and inefficient one-hole requests. The
weighted scheduling and bounded hole coalescing described above address both.

- All 257 Python tests pass; the disposable real-InfluxDB restart probe passes.
- Stopped the collector and confirmed no minute samples existed for
  23:07–23:12 UTC. After recreation with its persistent ledger, all five samples
  were present for 37 energy/power, 36 current, and 32 voltage series.
- A test-only overdue initial detail timer triggered one second-data batch
  without reducing the normal hourly interval. The 23:38–23:40 UTC minute gaps
  during that long batch subsequently filled for the same series counts.
- The test used `initialLookbackSecs: 60`. Every second in the resulting
  23:36:29–23:37:29 UTC recovery window is present for those same counts.
  An earlier 23:35–23:36 audit was incomplete and is outside that configured
  window; this test does **not** establish complete hour-long second history.
- Six field/series combinations remain absent in the outage window: primary
  aggregate current/voltage, primary Balance energy/power, and secondary
  aggregate energy/power. Balance is intentionally excluded; unavailable
  aggregate chart readings are not fabricated. Per-leg mains are separate.
- All six local Grafana query checks pass, and the overview visibly renders
  power, voltage, current, and energy across the repaired outage.

The temporary overdue-timer override was removed from the running container.
The local stack remains on the ordinary collector entrypoint and hourly detail
schedule, with recovery enabled only through the ignored local test inputs.
These results do not change production configuration or prove that Emporia
supplies every channel/metric at every historical resolution.
