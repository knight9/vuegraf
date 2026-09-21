#!/usr/bin/env ruby
require 'json'

root = File.expand_path('..', __dir__)
dashboard_path = File.join(root, 'grafana/dashboards/vuegraf-telemetry.json')
aliases = JSON.parse(File.read(File.join(root, 'grafana/channel-aliases.json')))
layout = JSON.parse(File.read(File.join(root, 'grafana/layout.json')))
dashboard = JSON.parse(File.read(dashboard_path))
datasource_uid = ENV.fetch('GRAFANA_DATASOURCE_UID', 'emporia-influxdb')

replace_datasource_uid = lambda do |node|
  case node
  when Hash
    node.each do |key, value|
      if key == 'datasource' && value.is_a?(Hash) && value['type'] == 'influxdb'
        value['uid'] = datasource_uid
      else
        replace_datasource_uid.call(value)
      end
    end
  when Array
    node.each { |value| replace_datasource_uid.call(value) }
  end
end
replace_datasource_uid.call(dashboard)

resolution_variable = {
  'current' => {'text' => 'Minute', 'value' => 'False'},
  'hide' => 0,
  'includeAll' => false,
  'label' => 'Resolution',
  'multi' => false,
  'name' => 'detail',
  'options' => [],
  'query' => 'Minute : False,Second : True,Hour : Hour,Day : Day',
  'type' => 'custom'
}
variables = dashboard.fetch('templating').fetch('list')
variables.reject! { |variable| variable['name'] == 'detail' }
variables.unshift(resolution_variable)

clauses = aliases.flat_map do |device, channels|
  channels.map do |channel, label|
    "if r.device_gid == #{device.to_json} and r.channel_num == #{channel.to_json} then #{label.to_json}"
  end
end
expression = "#{clauses.join(' else ')} else r.channel_num"
pattern = /channel_num: if r\.device_gid == "[^"]+"[\s\S]*?else r\.channel_num(?=\}\)\))/

dashboard.fetch('panels').each do |panel|
  (panel['targets'] || []).each do |target|
    target['query'] = target['query'].sub(pattern, "channel_num: #{expression}")
  end
end

metrics = {
  1 => ['power_watts', 'mean', 'power'],
  2 => ['voltage_volts', 'mean', 'voltage'],
  3 => ['current_amps', 'mean', 'current'],
  4 => ['energy_kwh', 'sum', 'energy']
}
metrics.each do |id, (metric, aggregate, result_name)|
  excluded = layout.fetch(metric == 'voltage_volts' ? 'merged_channels' : 'merged_legs')
  merge_filter = excluded.empty? ? 'true' :
    "r.device_gid != #{layout.fetch('right_device').to_json} or (#{excluded.map { |channel| "r.channel_num != #{channel.to_json}" }.join(' and ')})"
  target = dashboard.fetch('panels').find { |panel| panel['id'] == id }.fetch('targets').first
  target['query'] = <<~FLUX.chomp
    from(bucket: v.defaultBucket)
      |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
      |> filter(fn: (r) => r._measurement == "electrical_telemetry" and r._field == "#{metric}")
      |> filter(fn: (r) => r.account_name =~ /^${account:regex}$/ and r.device_gid =~ /^${device:regex}$/ and r.channel_num =~ /^${channel:regex}$/)
      |> filter(fn: (r) => r.detailed == "${detail}")
      |> filter(fn: (r) => #{merge_filter})
      |> map(fn: (r) => ({r with channel_num: #{expression}}))
      |> aggregateWindow(every: v.windowPeriod, fn: #{aggregate}, createEmpty: false)
      |> yield(name: "#{result_name}")
  FLUX
end

raw_target = dashboard.fetch('panels').find { |panel| panel['title'] == 'Raw Electrical Telemetry' }.fetch('targets').first
raw_target['query'] = <<~FLUX.chomp
  from(bucket: v.defaultBucket)
    |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
    |> filter(fn: (r) => r._measurement == "electrical_telemetry")
    |> filter(fn: (r) => r.account_name =~ /^${account:regex}$/ and r.device_gid =~ /^${device:regex}$/ and r.channel_num =~ /^${channel:regex}$/)
    |> filter(fn: (r) => r.detailed == "${detail}")
    |> pivot(rowKey: ["_time", "account_name", "device_gid", "channel_num", "detailed"], columnKey: ["_field"], valueColumn: "_value")
    |> map(fn: (r) => ({r with channel_num: #{expression}}))
    |> keep(columns: ["_time", "account_name", "device_gid", "channel_num", "channel_name", "station_name", "power_watts", "voltage_volts", "current_amps", "energy_kwh", "charge_ah", "detailed"])
    |> sort(columns: ["_time"], desc: true)
    |> limit(n: 200)
FLUX

dashboard['version'] += 1
File.write(dashboard_path, JSON.pretty_generate(dashboard) + "\n")
