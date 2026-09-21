#!/usr/bin/env ruby
# Generate a separate dashboard directory; never overwrite the source dashboards.
require 'json'
require 'fileutils'
abort 'Usage: route-grafana-buckets.rb CONFIG_JSON INPUT_DIR NEW_OUTPUT_DIR' unless ARGV.length == 3
config_path, input, output = ARGV
abort 'Output must not already exist' if File.exist?(output)
config = JSON.parse(File.read(config_path))
buckets = config.fetch('influxDb').fetch('telemetryBuckets')
tags = %w[second minute hour day].map { |r| config['influxDb'].fetch("tagValue_#{r}", {'second'=>'True','minute'=>'False','hour'=>'Hour','day'=>'Day'}[r]) }
parts = %w[second minute hour].each_with_index.map do |r, i|
  "if \"${detail}\" == #{JSON.generate(tags[i])} then #{JSON.generate(buckets.fetch(r))} else "
end.join + JSON.generate(buckets.fetch('day'))
FileUtils.mkdir_p(output)
Dir.glob(File.join(input, '*.json')).each do |path|
  dashboard = JSON.parse(File.read(path))
  visit = lambda do |value|
    case value
    when Hash
      value.each do |key, child|
        if %w[query definition].include?(key) && child.is_a?(String) && child.include?('bucket: v.defaultBucket')
          value[key] = child.gsub('bucket: v.defaultBucket', "bucket: #{parts}")
        else
          visit.call(child)
        end
      end
    when Array
      value.each { |child| visit.call(child) }
    end
  end
  visit.call(dashboard)
  File.write(File.join(output, File.basename(path)), JSON.pretty_generate(dashboard) + "\n")
end
puts 'Generated separate retention-aware dashboards. Review/test before replacing provisioned dashboards.'
