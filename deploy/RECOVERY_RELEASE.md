# Recovery production release

Deployed through Portainer on 2026-09-19 at approximately 17:49 Pacific
(2026-09-20 00:49 UTC).

- Source checkpoint: `b520c1c`, branch `feature/telemetry-gap-recovery`.
- Image: `knight9/vuegraf-telemetry:recovery-2026-09-19`, linux/amd64.
- Image manifest ID: `sha256:a0ea60afe68970d51d41522be85e276a226dfb2c6f08fa7e2275d7a1bf431fa1`.
- Previous image retained: `knight9/vuegraf-telemetry:2026-09-19`.
- Source snapshot retained in the server project's `releases/b520c1c` directory;
  source and image archives are in its `images` directory.

## Applied changes

Changed only the collector image, added `stop_signal: SIGINT` and
`stop_grace_period: 60s`, and mounted the stack's new named `recovery-state`
volume at `/opt/vuegraf/state`. The initialized ledger is owned by UID/GID 1012.

Added `telemetry.recovery` to the existing decoded `VUEGRAF_CONFIG_B64` payload:
enabled, statePath `/opt/vuegraf/state/coverage.sqlite3`, initialLookbackSecs
3600, maxRequestsPerCycle 12, pauseSecs 0.2. All other decoded settings and
credentials were preserved. Debug remains off. Portainer deployed without
re-pulling images.

InfluxDB and Grafana containers remained running, with their original storage
mounts unchanged. No historical data was cleared, and no seven-day catch-up was
requested. The local collector was stopped to avoid duplicate Emporia polling.

## Verification and follow-up

Portainer reported successful deployment. The new collector wrote fresh data,
then minute/hour/day recovery batches. Production Grafana visibly rendered
readings. Unsupported aggregate history requests were deferred as expected.
The first production second-data batch is due on the ordinary hourly schedule;
its completeness was not established by this initial deployment check.

The shared `collector-settings.json` still omits recovery; local acceptance uses
the separate ignored recovery-test inputs described in `RECOVERY.md`. Before
future production-parity testing, explicitly match the production recovery
settings above instead of the test's one-minute initial lookback.

## Rollback

Through Portainer, restore the previous image tag and remove/disable only the
new `telemetry.recovery` object in the configuration payload. Keep the database
and recovery volumes. Do not delete data or use `--resetdatabase`. If restoring
an older database backup later, reassess/reset the coverage ledger separately
because it may describe data newer than that backup.
