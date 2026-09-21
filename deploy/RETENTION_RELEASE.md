# Production retention release

Deployed through the Portainer UI on 2026-09-20 at approximately 22:24
America/Los_Angeles (2026-09-21 05:24 UTC).

## Runtime and routing

- Image: `knight9/vuegraf-telemetry:admin-2026-09-20-r2`
- Image ID: `sha256:e344b9dfd9349569043a6a7a96c9413ddb430bf0215e203bca11818a70ab2982`
- `vuegraf` and `storage-monitor` run the new image; `config-init` exited 0.
- `legacyEnergyEnabled` is false; existing legacy records were not deleted.
- Recovery is persistent, sequential and bounded to three extra requests per
  minute cycle. Five empty/unsupported attempts mark only the affected interval
  permanently unavailable; successful overlapping data supersedes that marker.
- Admin remains on port 3001 with credentials stored only in Portainer.

Production writes are routed by resolution:

| Resolution | Bucket | Retention |
| --- | --- | --- |
| Second | `vuegraf-seconds` | 30 days (2,592,000 seconds) |
| Minute | `vuegraf-minutes` | 2 years (63,072,000 seconds) |
| Hour | `vuegraf-coarse` | 5 years (157,680,000 seconds) |
| Day | `vuegraf-coarse` | 5 years (157,680,000 seconds) |

The original `emporia` bucket remains unlimited and intact as the rollback
source. No source data was deleted and no existing retention policy was
shortened. Retention is not a byte quota.

## Historical copy verification

The first clean pre-cutover pass copied and hash-verified the historical range.
After routing changed, the source bucket became stationary and the mutable
source windows were copied again. The verifier requires every source identity
and value to exist in the destination while permitting valid destination-only
recovery points.

- Second: the final potentially mutable 01:00–04:36 UTC interval verified
  3,263,400 field rows in four windows. Older windows had already passed the
  clean pre-cutover hash verification and were older than Emporia's three-hour
  second-recovery window.
- Minute: 1,527,264 source field rows verified in nine windows; 345,096 valid
  destination-only recovered rows were retained.
- Hour: 25,293 source field rows verified; 34 destination-only recovered rows
  were retained.
- Day: 756 source field rows verified.

The management regression verifies idempotent replay, destination supersets,
and failure when a source identity exists with a changed destination value.

## Dashboard and runtime acceptance

Grafana is provisioned from:

`/home/knight9/vuegraf/releases/admin-retention-2026-09-20/grafana/dashboards`

The prior directory contents were backed up beside it as
`dashboards.before-hour-day-20260921`. Both production dashboards expose
Minute, Second, Hour and Day. Queries select the correct bucket based on the
resolution value. All 160 generated panel/variable queries executed against the
disposable local InfluxDB, and production browser checks rendered Minute, Hour
and Day data. A manual one-circuit 60-second production refresh succeeded with
300 field values and rendered in the Second view.

The authenticated admin status reported 40 circuits, correct bucket policies,
legacy recording disabled, a complete storage scan, no low-space warning and
approximately 343 GiB host free space. An unauthenticated status request returned
HTTP 401. Normal minute writes continued across cutover: the legacy stream ended
at 05:23 UTC and the retained minute stream contained 05:24 UTC.

Rollback keeps all data: restore the previous image/config and the backed-up
dashboard directory. Do not delete the original bucket without a separate,
explicitly approved cleanup plan.
