# Admin web page TODO

Recovery, the admin/controller/API, legacy switch, storage visibility, and
resolution-specific retention are deployed. See `ADMIN.md` for setup, tests,
constraints and the non-destructive migration record. The checklist below is
the acceptance record for the production release.

## Current implementation checkpoint

- [x] One collector worker, shared no-queue admission, and cached status API.
- [x] Authenticated UI/API, manual circuit-selectable seconds, inactivity polling.
- [x] Discovery/observed coverage, bounded CSV exports, API documentation/OpenAPI.
- [x] Configurable legacy-recording disable switch, including historical paths.
- [x] Cached disk/bucket retention status, growth estimates, low-space warning,
  optional rate-limited webhook (disabled until explicitly configured).
- [x] Resolution bucket routing and non-destructive provisioning/copy/hash verification.
- [x] Separate retention-aware dashboard generation, including variable queries.
- [x] Real-Emporia local acceptance: a one-circuit 60-second manual refresh
  succeeded, exported 60 power samples, and existing Grafana query checks passed
  with legacy recording disabled. Real local collector stopped afterward.
- [x] UI low-space warning deployed. No out-of-band destination was supplied, so
  the optional webhook remains disabled and no external service is contacted.
- [x] Production migration/cutover reviewed, approved and deployed; no source
  data was deleted or retention-shortened.
- [x] Hard-quota decision recorded: no filesystem quota is configured. Retention,
  host-free-space monitoring and bounded container logs are the active controls.
- [x] Isolated, network-free storage monitor with read-only InfluxDB access;
  collector UID 1012 receives only size statistics, not database files.

Job timestamps/status and growth observations currently reset with the process;
recovery coverage remains persistent. Quota detection is not implemented: the UI
explicitly reports no verified hard quota rather than inventing one.

## Priority 1: Automatic telemetry gap recovery

Implemented and deployed with persistent coverage, bounded repair, explicit
unavailable intervals and shared admin status. See `RECOVERY.md` and
`deploy/RECOVERY_RELEASE.md`. The production collector keeps normal Emporia
access sequential and routes recovered data to the resolution retention buckets.

- [x] Track durable, successfully written collection boundaries per account,
  device/channel, metric, and resolution. Restore recovery state after restart;
  do not infer complete coverage solely from the newest sample timestamp.
- [x] Fetch missing completed intervals in bounded batches, prioritizing minute
  gaps after long second-data jobs. Respect existing source-history limits
  (currently seven days for minutes and three hours for seconds); local retention
  windows do not extend what Emporia can return.
- [x] Handle missed hourly/daily intervals as well as minutes and seconds;
  preserve timezone and daylight-saving boundaries.
- [x] Advance successful boundaries only after database writes succeed. Track
  partial failures and unavailable source intervals explicitly, with bounded
  retries/backoff so missing source data cannot create an endless request loop.
- [x] Preserve sequential Emporia access and idempotent writes, and expose
  recovery progress, remaining gaps, and failures through the later status API.
- [x] Test slow collection cycles, outages/restarts, partial API/write failures,
  unavailable readings, and overlapping retries. Verify recovered telemetry in
  the dashboards without relying on legacy recording or a full history import.

## Collection controls and status

- [x] Show separate status for second, minute, hourly, daily, and historical
  collection: idle/running, trigger source, progress, requested range, last
  start/completion, last result/error, last successful completion, data collected
  through, and next scheduled run where applicable.
- [x] Add a one-shot "Fetch latest 1-second data" button, not continuous live
  polling. Bound requests to available history and allow circuit selection.
- [x] Use a collector-owned mutex shared by manual, scheduled, and historical
  second-data collection. Return the active job status when busy; do not queue
  duplicate requests. Preserve sequential Emporia access.
- [x] Share successful collection boundaries and schedule the next second-data
  batch from successful completion. Do not advance success markers on failure.
- [x] Poll one combined status endpoint every second while the page is active.
  Pause after five minutes without user interaction; polling does not count as
  activity. Show active/paused and last-checked time. "Check now" immediately
  checks status and resumes the active window. Pausing UI polling must not stop
  collection or scheduling.

## Unified admin API and AI data access

- [x] Make every admin UI function available through the same documented API;
  the UI is a client of that API. Include collection status/control, storage and
  retention visibility, and data discovery/export. Do not add a separate
  read-only role or separate AI credentials: the single authenticated user can
  access all supported admin functions. This does not imply arbitrary shell,
  Docker, or database-administration access.
- [x] Configure the admin username and password through Docker Compose YAML
  environment settings (for example, `VUEGRAF_ADMIN_USERNAME` and
  `VUEGRAF_ADMIN_PASSWORD`). Require authentication for both the UI and API,
  fail closed when credentials are missing, and never log or return credentials.
  Do not commit real passwords or bake them into the image.
- [x] Target personal home-network use only, with no public exposure. Document
  LAN binding/firewall requirements and that plain HTTP does not encrypt
  credentials or data; revisit transport/security before any external exposure.
  Protect browser-initiated state changes against CSRF, avoid permissive CORS,
  and use non-GET methods for actions that change state.
- [x] Add a clearly linked "API / AI access" page and machine-readable OpenAPI
  specification. Document authentication, every UI-backed endpoint, parameters,
  response schemas, busy/error responses, and executable request examples with
  credential placeholders. AI clients should not need SSH or Docker access.
- [x] Expose circuit IDs/display names, metrics/units, resolutions, available
  date coverage, timezone conventions, and missing-data semantics. Explain how
  to avoid double-counting across resolutions and merged circuits/mains.
- [x] Provide parameterized queries and downloadable CSV exports for file-based
  analysis, with documented aggregation semantics. Stream bounded exports;
  use tracked asynchronous export jobs only if larger exports require them.
  Export jobs are separate from the no-queue collection mutex policy.
- [x] Validate query parameters instead of accepting arbitrary Flux. Limit
  output size, query duration, date ranges by resolution, and concurrent exports.
  If exports use temporary files, cap their total storage and expire them.
- [x] Test authentication, API/UI parity, busy-job behavior, export accuracy,
  limits, and documentation examples. MCP remains optional; HTTP and file
  exports are the initial AI integration.

## Legacy energy collection configuration

- [x] Add a boolean `legacyEnergyEnabled` configuration option, defaulting to
  `true` for backward compatibility. Setting it to `false` must stop legacy
  `energy_usage` collection and writes for every resolution and historical
  backfills while preserving `electrical_telemetry` collection.
- [x] Skip legacy-only API requests and resume queries when disabled; retain
  shared requests needed by telemetry. Do not delete existing legacy records.
- [x] Show whether legacy collection is enabled on the admin status page.
- [x] Test enabled, disabled, and omitted-option behavior across normal and
  historical collection, and verify both telemetry dashboards still work.
  Document the option and rebuild the image before configuring it locally or
  in production; the production image implements and disables this switch.

## Space usage and retention

- [x] Show InfluxDB disk usage, host filesystem capacity/used/free space, and
  measurement timestamp. Distinguish database usage from total host usage.
- [x] Show any configured hard quota, remaining allowance, and utilization;
  explicitly show "No quota configured" when no enforced limit exists.
- [x] Show the actual retention period for every bucket and its resolutions,
  including legacy `energy_usage`. Clearly flag unlimited retention.
- [x] Display storage growth trends and estimated time to quota/disk exhaustion
  when enough observations exist; label estimates and their observation window.
- [x] Design storage warning thresholds and notification delivery. Show whether
  alerts are configured and their destination; a UI banner alone is not an
  out-of-band notification. Monitor host free space as well as database usage.
- [x] Implement resolution-specific retention using separate buckets as needed.
  The user-selected target windows below replace the earlier proposals; they
  are active in production:

  | Resolution | Target retention |
  | --- | --- |
  | Second | 30 days |
  | Minute | 2 years |
  | Hourly | 5 years |
  | Daily | 5 years |

- [x] Plan collector routing, Grafana bucket selection, existing-data migration,
  and legacy-data handling together. Test locally and verify copied data before
  production cutover. Obtain explicit approval before deleting redundant data
  or applying retention changes that expire existing records.
- [x] Explain that retention cleanup is asynchronous and is not a byte quota;
  quota exhaustion can cause writes to fail. Account separately for backups and
  container logs outside the database directory.

Storage status should be cached on the backend at a suitable interval: the
one-second UI status poll must not trigger recursive disk scans, expensive
database queries, or Emporia requests on every poll.
