# Electrical telemetry

Enable in `vuegraf.json` (requires PyEmVue 0.18.9 or newer):

```json
"telemetry": {
  "enabled": true,
  "metrics": ["energy", "current", "voltage"]
}
```

These are the three electrical units exposed by the existing Emporia API.
The collector retains the original values and produces the following fields:

| Selection | Stored fields | Meaning |
| --- | --- | --- |
| energy | energy_kwh, power_watts | Energy per sample and average real power |
| current | charge_ah, current_amps | Amp-hours per sample and average current |
| voltage | voltage_volts | Voltage returned by Emporia, without energy scaling |

Watts = kWh × 3,600,000 / sample seconds. Amps = Ah × 3,600 / sample seconds.
Negative values and zero are retained; missing values, nonnumeric sentinels,
NaN and infinity are omitted. No synthetic power factor, frequency, reactive
power or apparent power is produced: these are not exposed as units by this API.
Voltage on a circuit is the value returned by Emporia; it is not proof of a
separate voltage sensor at that branch circuit or outlet.

For all API units, use `"metrics": ["all"]`. Additional individual selections
are `cost`, `trees`, `gas`, `distance`, and `carbon`, stored as `cost_dollars`,
`trees`, `gas_gallons`, `distance_miles`, and `carbon`. These are Emporia's
calculated equivalents, not electrical measurements. Carbon is kept in the
API's native unit without assuming a mass unit. Availability depends on the
account/device and server. More units increase API requests.

## Discovery and collection

The collector combines channel identifiers from all selected unit responses.
In particular, current/voltage responses can expose `Mains_A`, `Mains_B`, and
physical circuits omitted from the energy response. Missing combinations are
queried through the chart endpoint, including individual mains power. Numeric
branch channels are never guessed to be mains. Only channels returned by the
account's API are requested.

Minute snapshots run alongside existing collection. Enable
`detailedDataEnabled` and `detailedDataSecondsEnabled` for second-resolution
history at the existing `detailedIntervalSecs` cadence (normally hourly).
Do not lower this cadence: full-resolution chart requests are expensive.
Second history is bounded to the most recent three hours; successful batches
share an exact boundary to avoid gaps. The first batch follows the existing
detail schedule, rather than triggering an immediate historical import.

This extension does not yet backfill telemetry from `--historydays` or recover
minute gaps across an outage. That flag continues to operate on legacy energy
history. Existing hourly/daily power collection is unchanged. Graphs should
choose one resolution; summing minute and second energy counts the same usage
twice. No database retention settings are changed.

Unavailable chart combinations are retried after one hour. Bulk requests use
one attempt so a Vue without mains CTs does not cause repeated retries for null
readings. Authentication, rate-limit and other request errors stop the extra
collection for that cycle and are logged without credentials or request URLs.
Existing power collection still proceeds. No readings are fabricated.

## Storage and Grafana

InfluxDB uses the new measurement `electrical_telemetry`. Existing
`energy_usage` records and dashboards remain compatible. Tags are
`account_name`, `device_gid`, `channel_num`, and the configured detail tag
(`detailed` by default). Names are string fields `channel_name` and
`station_name`, so renaming does not change the numeric series identity.

For example, a Grafana Flux voltage query for the two mains:

```flux
from(bucket: "vuegraf")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "electrical_telemetry")
  |> filter(fn: (r) => r._field == "voltage_volts" and r.detailed == "True")
  |> filter(fn: (r) => r.channel_num == "Mains_A" or r.channel_num == "Mains_B")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
```

Add an account/device filter if multiple panels have mains. Use
`current_amps` or `power_watts` for the other graphs and `False` for minute
data. Keep raw second data when investigating brief changes; broad averages
can hide them. Even second data cannot resolve sub-second electrical events.

VictoriaMetrics writes `electrical_telemetry_<field>` with the same stable
identity labels. MQTT publishes each metric separately under the existing
topic plus `/telemetry`, including its value, field name, channel IDs, display
names, timestamp and resolution. Legacy MQTT messages retain their format.

## Read-only live verification

Run from a development checkout with dependencies installed:

```sh
PYTHONPATH=src python tools/probe_telemetry.py
```

The probe prompts for credentials without saving them, reports metric counts
and discovered mains samples, and requests a five-second sample of each mains
leg. It does not write to a database or change devices. Output includes usage
readings; do not commit account-specific probe output.
