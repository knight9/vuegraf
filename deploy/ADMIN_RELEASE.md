# Admin release deployment

## Retention and bounded-recovery update

Deployed through the Portainer UI on 2026-09-20 at approximately 22:24
America/Los_Angeles (2026-09-21 05:24 UTC). See `RETENTION_RELEASE.md` for the
complete migration and validation record.

- Image: `knight9/vuegraf-telemetry:admin-2026-09-20-r2`
- Image ID: `sha256:e344b9dfd9349569043a6a7a96c9413ddb430bf0215e203bca11818a70ab2982`
- Recovery requests per normal cycle: 3
- Empty/unsupported interval attempts before permanent-unavailable status: 5
- Resolution routing: seconds to `vuegraf-seconds`, minutes to
  `vuegraf-minutes`, and hour/day to `vuegraf-coarse`
- Grafana Resolution selector: Minute, Second, Hour, Day
- Original unlimited `emporia` bucket retained unchanged for rollback

## Initial admin release

Deployed through the Portainer UI on 2026-09-20 at approximately 20:35
America/Los_Angeles (2026-09-21 03:35 UTC).

- Source commit: `d841970` (`Add authenticated collector admin and retention tooling`)
- Image: `knight9/vuegraf-telemetry:admin-2026-09-20`
- Image ID: `sha256:04242ca4a799ec367597766b514254d236e56405b3efd87e75246f65f80d4baa`
- Admin URL: `http://192.168.80.150:3001`
- Admin username: `knight9` (password stored only as a private Portainer stack variable)
- Legacy `energy_usage` recording: disabled; existing records retained
- Recovery state volume: preserved
- InfluxDB and Grafana data mounts: unchanged

The new `storage-monitor` has the InfluxDB data bind mount read-only, no network,
a read-only root filesystem, all capabilities dropped except
`DAC_READ_SEARCH`, and a separate statistics volume. VueGraf remains UID/GID
1012 and mounts only that statistics volume read-only.

Post-deployment checks:

- Portainer reported successful stack deployment.
- `storage-monitor` healthy; VueGraf running the new image with host port 3001.
- Unauthenticated admin request returned HTTP 401.
- Authenticated status reported ready/idle, legacy recording disabled, 40
  discovered circuits, complete storage scan, and a successful minute job.
- Storage scan reported approximately 90 MB of InfluxDB files at deployment.
- Grafana retained its data volume and displayed fresh minute power series.
- Recovery was partial for historical intervals unavailable from Emporia; no
  values were fabricated and normal scheduled collection succeeded.

Resolution-specific retention buckets and historical migration were intentionally
not enabled by this release. The existing bucket remains intact and unlimited.
No source data was deleted or retention-shortened.

Rollback: restore the prior stack YAML/image
`knight9/vuegraf-telemetry:recovery-2026-09-19`, remove the admin environment,
port, storage-monitor service and statistics volume mount, and restore the prior
`VUEGRAF_CONFIG_B64`. The prior image and data volumes remain available.
