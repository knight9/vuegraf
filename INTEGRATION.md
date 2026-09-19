# Local stack with production settings

Run commands from the repository directory. OrbStack stays isolated from the
production InfluxDB. Ports bind only to `127.0.0.1`.

## Configuration source

`deploy/collector-settings.json` holds shared, non-secret settings: telemetry
enabled, minute polling every 60 seconds, and second/hour/day history collection
every 3600 seconds. Debug is off in Compose.

`int/.env` supplies local Influx/Grafana credentials and the existing bucket/org.
`int/vuegraf.json` supplies only the Emporia `accounts` section. Both original
files are preserved; other settings in the old JSON are superseded by the shared
settings file. Create these private inputs from the sample config if needed.

`ruby scripts/prepare-local-stack.rb` generates ignored `int/stack.env`, mode
0600, with the original environment settings plus `VUEGRAF_CONFIG_B64` and
`GRAFANA_DATASOURCE_B64`. Base64 is encoding, not encryption; never share or commit
this file. Compose uses production's Alpine config-init decoding process,
owners/permissions and read-only config mounts. Missing payloads fail init.

## Run and verify

Household mappings, layout settings, and generated dashboards are ignored by Git.
The published examples use fictional device IDs and generic circuit names.
For a fresh checkout, copy `grafana/examples/channel-aliases.json` and
`grafana/examples/layout.json` into `grafana/`, and copy the example dashboard
JSON files from `grafana/examples/dashboards/` into `grafana/dashboards/`.
Do not overwrite existing private files. Customize device IDs, labels, and the
right-side merged-channel/leg lists in the private JSON files before rendering.
The breaker renderer uses `right_channels` for individual right-leg titles and
colors; aliases supply the overview labels and left-leg titles. Review both.
Keep private files backed up separately; cloning the fork will not restore them.

```sh
docker --context orbstack compose -f compose.local.yaml build
ruby scripts/prepare-local-stack.rb
ruby scripts/render-grafana-aliases.rb
ruby scripts/render-breaker-dashboard.rb
docker --context orbstack compose --env-file int/stack.env -f compose.integration.yaml --profile collector up -d
docker --context orbstack compose --env-file int/stack.env -f compose.integration.yaml logs -f vuegraf
```

After the first collection cycle, `ruby scripts/verify-local-stack.rb` checks
both provisioned datasource mappings and executes their real power, voltage,
current, energy and mains queries against the last five minutes of local data.
It fails on query errors or empty results and never prints credentials.

Grafana is at <http://localhost:3000>. Both dashboards use datasource UID
`emporia-influxdb`, matching production. New minute telemetry appears after a
successful collection cycle. Second history follows the hourly schedule;
enabling telemetry does not automatically import past minute data.

Enabling `addStationField` to match production changes the legacy series tags
relative to older local runs. Existing legacy resume logic may catch up those
series on the first cycle; allow that cycle to complete before verification.

For debug, stop the background collector, then run it interactively:

```sh
docker --context orbstack compose --env-file int/stack.env -f compose.integration.yaml stop vuegraf
docker --context orbstack compose --env-file int/stack.env -f compose.integration.yaml --profile collector run --rm vuegraf --debug /opt/vuegraf/conf/vuegraf.json
```

Stop the local stack without deleting data:

```sh
docker --context orbstack compose --env-file int/stack.env -f compose.integration.yaml --profile collector stop
```

## Parity and intentional differences

Both use `alpine:3.20`, `influxdb:2.7-alpine`, and
`grafana/grafana-oss:11.5.2`, identical dashboard JSON and datasource UID, and
generated read-only configuration. Local builds use `vuegraf-telemetry:local`;
production uses the reviewed release image.

Local credentials, bucket/org, loopback port bindings and named volumes remain
separate. The existing local Influx volume is reused. A new Grafana volume,
`integration-grafana-prod-version`, avoids downgrading the old 11.6.1 database.
The old `integration-grafana` volume is retained.

## Applying settings to Portainer

Production's `config-init.environment.VUEGRAF_CONFIG_B64` contains the complete
collector JSON. The minimal fix adds this object to its decoded JSON:

```json
"telemetry": {"enabled": true, "metrics": ["energy", "current", "voltage"]}
```

For full parity, merge `deploy/collector-settings.json` into that decoded JSON,
preserving production `accounts` and `influxDb`; base64-encode the result and
replace the payload in the Portainer stack editor. Review the decoded settings
diff before updating the stack. Never copy `int/stack.env` to production.

Update through Portainer and restart VueGraf after config-init completes. An
init payload change may not recreate an otherwise unchanged collector; restart
ensures it reads the new config. Verify telemetry points and dashboard results.
Do not edit generated JSON directly: the next init run overwrites it.

Local settings are the proposed production target; production still needs a
separate reviewed update and verification.
