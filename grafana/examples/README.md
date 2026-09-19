# Generic dashboard examples

These files use fictional device IDs `100001` and `100002`. They demonstrate
two 15-channel panels, individual breaker graphs, and merged-circuit filtering;
the channel relationships are examples, not automatic wiring discovery.

Copy the aliases and layout files into `grafana/` and the dashboard JSON files
into `grafana/dashboards/` on a fresh checkout. Those destination paths are
ignored. Never overwrite an existing household configuration with the examples.
Replace example IDs, names, channel relationships, and colors to match your
devices, then run both scripts:

```sh
ruby scripts/render-grafana-aliases.rb
ruby scripts/render-breaker-dashboard.rb
```

The scripts update the private dashboard files, which Compose provisions into
Grafana. The overview uses aliases; individual right-side breaker titles/colors
come from `layout.json`. `merged_channels` identifies synthesized channels to
exclude from voltage graphs; `merged_legs` identifies individual legs to exclude
when showing the synthesized power/current/energy series. Use empty lists when
no such filtering is needed. These example filters apply to `right_device`.

The templates use datasource UID `emporia-influxdb`. Render with
`GRAFANA_DATASOURCE_UID` to override it. No database or Emporia credentials
belong in these files.
