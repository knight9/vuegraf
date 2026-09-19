#!/usr/bin/env ruby
# Read-only integration check of provisioned dashboards and their actual queries.
require 'json'
require 'net/http'
require 'open3'

Dir.chdir(File.expand_path('..', __dir__))
raw, _, status = Open3.capture3('docker', '--context', 'orbstack', 'compose',
  '--env-file', 'int/stack.env', '-f', 'compose.integration.yaml', 'config', '--format', 'json')
abort 'Cannot resolve local Compose configuration.' unless status.success?
env = JSON.parse(raw).fetch('services').fetch('grafana').fetch('environment')
request = lambda do |path, body = nil|
  uri = URI("http://127.0.0.1:3000#{path}")
  req = body ? Net::HTTP::Post.new(uri) : Net::HTTP::Get.new(uri)
  req.basic_auth(env.fetch('GF_SECURITY_ADMIN_USER'), env.fetch('GF_SECURITY_ADMIN_PASSWORD'))
  if body
    req['Content-Type'] = 'application/json'
    req.body = JSON.generate(body)
  end
  response = Net::HTTP.start(uri.host, uri.port, open_timeout: 5, read_timeout: 60) { |http| http.request(req) }
  abort "Grafana request failed: HTTP #{response.code}" unless response.is_a?(Net::HTTPSuccess)
  JSON.parse(response.body)
end
%w[vuegraf-electrical-telemetry vuegraf-breaker-layout].each do |uid|
  dashboard = request.call("/api/dashboards/uid/#{uid}").fetch('dashboard')
  panels = dashboard.fetch('panels').select { |p| p['targets'] }
  abort 'Wrong provisioned datasource UID.' unless panels.all? { |p| p.dig('datasource', 'uid') == 'emporia-influxdb' }
  selected = uid == 'vuegraf-breaker-layout' ? panels.first(2) : panels.select { |p| (1..4).include?(p['id']) }
  selected.each do |panel|
    query = panel.fetch('targets').first.fetch('query')
      .gsub('${detail}', 'False').gsub('${metric}', 'power_watts')
      .gsub(/\$\{(?:account|device|channel):regex\}/, '.*')
    result = request.call('/api/ds/query', {
      'from' => ((Time.now.to_f - 300) * 1000).to_i.to_s,
      'to' => (Time.now.to_f * 1000).to_i.to_s,
      'queries' => [{'refId' => 'A', 'datasource' => {'type' => 'influxdb', 'uid' => 'emporia-influxdb'},
        'query' => query, 'intervalMs' => 60000, 'maxDataPoints' => 300}]
    }).fetch('results').fetch('A')
    abort "Query failed: #{panel['title']}" if result['error']
    rows = result.fetch('frames', []).sum { |f| f.dig('data', 'values', 0)&.length || 0 }
    abort "No recent data: #{panel['title']}" if rows.zero?
    puts "PASS #{panel['title']}: #{rows} recent rows"
  end
end
