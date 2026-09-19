# Admin web page TODO

Planning only: these features and policies are not yet implemented or enabled
in production.

## Priority 1: Automatic telemetry gap recovery

Complete this before the admin UI/API work. The current telemetry minute path
records snapshots without automatically recovering minutes missed during long
collection cycles; legacy `energy_usage` recovery does not fill those telemetry
gaps.

- [ ] Track durable, successfully written collection boundaries per account,
  device/channel, metric, and resolution. Restore recovery state after restart;
  do not infer complete coverage solely from the newest sample timestamp.
- [ ] Fetch missing completed intervals in bounded batches, prioritizing minute
  gaps after long second-data jobs. Respect existing source-history limits
  (currently seven days for minutes and three hours for seconds); local retention
  windows do not extend what Emporia can return.
- [ ] Handle missed hourly/daily intervals as well as minutes and seconds;
  preserve timezone and daylight-saving boundaries.
- [ ] Advance successful boundaries only after database writes succeed. Track
  partial failures and unavailable source intervals explicitly, with bounded
  retries/backoff so missing source data cannot create an endless request loop.
- [ ] Preserve sequential Emporia access and idempotent writes, and expose
  recovery progress, remaining gaps, and failures through the later status API.
- [ ] Test slow collection cycles, outages/restarts, partial API/write failures,
  unavailable readings, and overlapping retries. Verify recovered telemetry in
  the dashboards without relying on legacy recording or a full history import.

## Collection controls and status

- [ ] Show separate status for second, minute, hourly, daily, and historical
  collection: idle/running, trigger source, progress, requested range, last
  start/completion, last result/error, last successful completion, data collected
  through, and next scheduled run where applicable.
- [ ] Add a one-shot "Fetch latest 1-second data" button, not continuous live
  polling. Bound requests to available history and allow circuit selection.
- [ ] Use a collector-owned mutex shared by manual, scheduled, and historical
  second-data collection. Return the active job status when busy; do not queue
  duplicate requests. Preserve sequential Emporia access.
- [ ] Share successful collection boundaries and schedule the next second-data
  batch from successful completion. Do not advance success markers on failure.
- [ ] Poll one combined status endpoint every second while the page is active.
  Pause after five minutes without user interaction; polling does not count as
  activity. Show active/paused and last-checked time. "Check now" immediately
  checks status and resumes the active window. Pausing UI polling must not stop
  collection or scheduling.

## Unified admin API and AI data access

- [ ] Make every admin UI function available through the same documented API;
  the UI is a client of that API. Include collection status/control, storage and
  retention visibility, and data discovery/export. Do not add a separate
  read-only role or separate AI credentials: the single authenticated user can
  access all supported admin functions. This does not imply arbitrary shell,
  Docker, or database-administration access.
- [ ] Configure the admin username and password through Docker Compose YAML
  environment settings (for example, `VUEGRAF_ADMIN_USERNAME` and
  `VUEGRAF_ADMIN_PASSWORD`). Require authentication for both the UI and API,
  fail closed when credentials are missing, and never log or return credentials.
  Do not commit real passwords or bake them into the image.
- [ ] Target personal home-network use only, with no public exposure. Document
  LAN binding/firewall requirements and that plain HTTP does not encrypt
  credentials or data; revisit transport/security before any external exposure.
  Protect browser-initiated state changes against CSRF, avoid permissive CORS,
  and use non-GET methods for actions that change state.
- [ ] Add a clearly linked "API / AI access" page and machine-readable OpenAPI
  specification. Document authentication, every UI-backed endpoint, parameters,
  response schemas, busy/error responses, and executable request examples with
  credential placeholders. AI clients should not need SSH or Docker access.
- [ ] Expose circuit IDs/display names, metrics/units, resolutions, available
  date coverage, timezone conventions, and missing-data semantics. Explain how
  to avoid double-counting across resolutions and merged circuits/mains.
- [ ] Provide parameterized queries and downloadable CSV exports for file-based
  analysis, with documented aggregation semantics. Stream bounded exports;
  use tracked asynchronous export jobs only if larger exports require them.
  Export jobs are separate from the no-queue collection mutex policy.
- [ ] Validate query parameters instead of accepting arbitrary Flux. Limit
  output size, query duration, date ranges by resolution, and concurrent exports.
  If exports use temporary files, cap their total storage and expire them.
- [ ] Test authentication, API/UI parity, busy-job behavior, export accuracy,
  limits, and documentation examples. MCP remains optional; HTTP and file
  exports are the initial AI integration.

## Legacy energy collection configuration

- [ ] Add a boolean `legacyEnergyEnabled` configuration option, defaulting to
  `true` for backward compatibility. Setting it to `false` must stop legacy
  `energy_usage` collection and writes for every resolution and historical
  backfills while preserving `electrical_telemetry` collection.
- [ ] Skip legacy-only API requests and resume queries when disabled; retain
  shared requests needed by telemetry. Do not delete existing legacy records.
- [ ] Show whether legacy collection is enabled on the admin status page.
- [ ] Test enabled, disabled, and omitted-option behavior across normal and
  historical collection, and verify both telemetry dashboards still work.
  Document the option and rebuild the image before configuring it locally or
  in production; the current image does not implement this switch.

## Space usage and retention

- [ ] Show InfluxDB disk usage, host filesystem capacity/used/free space, and
  measurement timestamp. Distinguish database usage from total host usage.
- [ ] Show any configured hard quota, remaining allowance, and utilization;
  explicitly show "No quota configured" when no enforced limit exists.
- [ ] Show the actual retention period for every bucket and its resolutions,
  including legacy `energy_usage`. Clearly flag unlimited retention.
- [ ] Display storage growth trends and estimated time to quota/disk exhaustion
  when enough observations exist; label estimates and their observation window.
- [ ] Design storage warning thresholds and notification delivery. Show whether
  alerts are configured and their destination; a UI banner alone is not an
  out-of-band notification. Monitor host free space as well as database usage.
- [ ] Implement resolution-specific retention using separate buckets as needed.
  The user-selected target windows below replace the earlier proposals; they
  are requirements for implementation, not settings already active in production:

  | Resolution | Target retention |
  | --- | --- |
  | Second | 30 days |
  | Minute | 2 years |
  | Hourly | 5 years |
  | Daily | 5 years |

- [ ] Plan collector routing, Grafana bucket selection, existing-data migration,
  and legacy-data handling together. Test locally and verify copied data before
  production cutover. Obtain explicit approval before deleting redundant data
  or applying retention changes that expire existing records.
- [ ] Explain that retention cleanup is asynchronous and is not a byte quota;
  quota exhaustion can cause writes to fail. Account separately for backups and
  container logs outside the database directory.

Storage status should be cached on the backend at a suitable interval: the
one-second UI status poll must not trigger recursive disk scans, expensive
database queries, or Emporia requests on every poll.
