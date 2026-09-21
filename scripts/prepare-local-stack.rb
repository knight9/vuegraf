#!/usr/bin/env ruby
# Generate ignored, private Portainer-style inputs; never print secrets.
require 'json'
require 'yaml'
require 'base64'
require 'open3'
require 'optparse'

recovery_test = false
admin_test = false
OptionParser.new do |parser|
  parser.on('--recovery-test', 'Prepare separate private inputs for local gap-recovery testing') { recovery_test = true }
  parser.on('--admin', 'Prepare private local admin inputs; legacy recording off, one-hour recovery') { admin_test = true }
end.parse!
abort 'Choose --admin or --recovery-test, not both.' if recovery_test && admin_test

root = File.expand_path('..', __dir__)
Dir.chdir(root)
settings = JSON.parse(File.read('deploy/collector-settings.json'))
if recovery_test || admin_test
  settings.fetch('telemetry')['recovery'] = {
    'enabled' => true, 'statePath' => '/opt/vuegraf/state/coverage.sqlite3',
    'initialLookbackSecs' => admin_test ? 3600 : 60, 'maxRequestsPerCycle' => 3,
    'unavailableAfterAttempts' => 5, 'pauseSecs' => 0.2
  }
end
if admin_test
  settings['legacyEnergyEnabled'] = false
  settings['admin'] = {'storageSnapshotPath' => '/storage-stats/disk.json'}
end
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
output = admin_test ? 'int/admin.env' : recovery_test ? 'int/recovery.env' : 'int/stack.env'
File.open(output, 'w', 0o600) do |file|
  file.write(File.read('int/.env'))
  file.puts
  file.puts "VUEGRAF_CONFIG_B64=#{Base64.strict_encode64(JSON.generate(config))}"
  file.puts "GRAFANA_DATASOURCE_B64=#{Base64.strict_encode64(YAML.dump(datasource))}"
  if admin_test
    # Deliberately simple credentials for the loopback-only local test stack.
    file.puts 'VUEGRAF_ADMIN_USERNAME=admin'
    file.puts 'VUEGRAF_ADMIN_PASSWORD=admin'
    file.puts 'VUEGRAF_ADMIN_HOST_PORT=3001'
  end
end
File.chmod(0o600, output)
puts "Prepared #{output} (private). Telemetry enabled; debug off; datasource emporia-influxdb."
