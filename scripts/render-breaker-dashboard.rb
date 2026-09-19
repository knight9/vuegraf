#!/usr/bin/env ruby
require 'json'

root = File.expand_path('..', __dir__)
aliases = JSON.parse(File.read(File.join(root, 'grafana/channel-aliases.json')))
layout = JSON.parse(File.read(File.join(root, 'grafana/layout.json')))
output = File.join(root, 'grafana/dashboards/vuegraf-breaker-layout.json')
DATASOURCE_UID = ENV.fetch('GRAFANA_DATASOURCE_UID', 'emporia-influxdb')
next_version = File.exist?(output) ? JSON.parse(File.read(output)).fetch('version', 0) + 1 : 1

left = (1..15).map { |channel| [channel.to_s, aliases.fetch(layout.fetch('left_device')).fetch(channel.to_s)] }
right = layout.fetch('right_channels')

def query(device, channel)
  <<~FLUX.chomp
    from(bucket: v.defaultBucket)
      |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
      |> filter(fn: (r) => r._measurement == "electrical_telemetry" and r._field == "${metric}")
      |> filter(fn: (r) => r.device_gid == "#{device}" and r.channel_num == "#{channel}" and r.detailed == "${detail}")
  FLUX
end

def panel(id, title, x, y, device, channel, color = '#5794F2')
  {
    'datasource' => {'type' => 'influxdb', 'uid' => DATASOURCE_UID},
    'fieldConfig' => {
      'defaults' => {
        'color' => {'mode' => 'fixed', 'fixedColor' => color}
      },
      'overrides' => []
    },
    'gridPos' => {'h' => 4, 'w' => 12, 'x' => x, 'y' => y},
    'id' => id,
    'options' => {
      'legend' => {'displayMode' => 'hidden', 'placement' => 'bottom', 'showLegend' => false},
      'tooltip' => {'mode' => 'single'}
    },
    'targets' => [{'query' => query(device, channel), 'refId' => 'A'}],
    'title' => title,
    'type' => 'timeseries'
  }
end

panels = [
  panel(1, 'Mains A - Primary', 0, 0, layout.fetch('mains_device'), 'Mains_A', '#FADE2A'),
  panel(2, 'Mains B - Primary', 12, 0, layout.fetch('mains_device'), 'Mains_B', '#FADE2A')
]

left.each_with_index { |(channel, title), index| panels << panel(10 + index, title, 0, 4 + index * 4, layout.fetch('left_device'), channel) }
right.each_with_index { |(channel, title, color), index| panels << panel(30 + index, title, 12, 4 + index * 4, layout.fetch('right_device'), channel, color) }

dashboard = {
  'annotations' => {'list' => []},
  'description' => 'Compact breaker layout. Matching trace colors identify right-side breaker legs that belong to the same merged circuit.',
  'editable' => false,
  'fiscalYearStartMonth' => 0,
  'graphTooltip' => 1,
  'id' => nil,
  'links' => [],
  'liveNow' => false,
  'panels' => panels,
  'refresh' => '30s',
  'schemaVersion' => 40,
  'tags' => ['vuegraf', 'telemetry', 'breaker-layout'],
  'templating' => {
    'list' => [
      {
        'current' => {'text' => 'Minute', 'value' => 'False'},
        'hide' => 0,
        'includeAll' => false,
        'label' => 'Resolution',
        'multi' => false,
        'name' => 'detail',
        'options' => [],
        'query' => 'Minute : False,Second : True',
        'type' => 'custom'
      },
      {
        'current' => {'text' => 'Real Power', 'value' => 'power_watts'},
        'hide' => 0,
        'includeAll' => false,
        'label' => 'Measurement',
        'multi' => false,
        'name' => 'metric',
        'options' => [],
        'query' => 'Real Power (W) : power_watts,Voltage (V) : voltage_volts,Current (A) : current_amps,Energy (kWh) : energy_kwh',
        'type' => 'custom'
      }
    ]
  },
  'time' => {'from' => 'now-6h', 'to' => 'now'},
  'timezone' => 'browser',
  'title' => 'VueGraf Breaker Layout',
  'uid' => 'vuegraf-breaker-layout',
  'version' => next_version
}

File.write(output, JSON.pretty_generate(dashboard) + "\n")
