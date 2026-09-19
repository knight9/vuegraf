#!/usr/bin/env ruby
# Generate ignored, private Portainer-style inputs; never print secrets.
require 'json'
require 'yaml'
require 'base64'
require 'open3'

root = File.expand_path('..', __dir__)
Dir.chdir(root)
settings = JSON.parse(File.read('deploy/collector-settings.json'))
account_config = JSON.parse(File.read('int/vuegraf.json'))
raw, errors, status = Open3.capture3('docker', '--context', 'orbstack', 'compose',
  '--env-file', 'int/.env', '-f', 'compose.integration.yaml', 'config', '--format', 'json')
abort 'Compose config validation failed; check int/.env and compose.integration.yaml.' unless status.success?
services = JSON.parse(raw).fetch('services')
db = services.fetch('influxdb').fetch('environment')
influx = {
  'version' => 2, 'url' => 'http://influxdb:8086',
  'org' => db.fetch('DOCKER_INFLUXDB_INIT_ORG'),
  'bucket' => db.fetch('DOCKER_INFLUXDB_INIT_BUCKET'),
  'token' => db.fetch('DOCKER_INFLUXDB_INIT_ADMIN_TOKEN')
}
abort 'Missing local Influx token.' if influx['token'].to_s.empty?
config = settings.merge('influxDb' => influx, 'accounts' => account_config.fetch('accounts'))
datasource = YAML.load_file('grafana/provisioning/datasources/vuegraf.yaml')
ds = datasource.fetch('datasources').fetch(0)
ds['jsonData']['organization'] = influx['org']
ds['jsonData']['defaultBucket'] = influx['bucket']
ds['secureJsonData']['token'] = influx['token']
File.umask(0o077)
# Keep the user's original .env and vuegraf.json untouched.
File.open('int/stack.env', 'w', 0o600) do |file|
  file.write(File.read('int/.env'))
  file.puts
  file.puts "VUEGRAF_CONFIG_B64=#{Base64.strict_encode64(JSON.generate(config))}"
  file.puts "GRAFANA_DATASOURCE_B64=#{Base64.strict_encode64(YAML.dump(datasource))}"
end
File.chmod(0o600, 'int/stack.env')
puts 'Prepared int/stack.env (private). Telemetry enabled; debug off; datasource emporia-influxdb.'
