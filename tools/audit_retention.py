"""Read-only source/target schema and row counts for one retention window."""
import argparse
import json

from vuegraf.admin_data import client_for, parse_time
from vuegraf.config import getInfluxTag


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config')
    parser.add_argument('--resolution', choices=('second', 'minute', 'hour', 'day'), required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--stop', required=True)
    parser.add_argument('--compare-field')
    args = parser.parse_args()
    with open(args.config) as stream:
        config = json.load(stream)
    start, stop = parse_time(args.start), parse_time(args.stop)
    db = config['influxDb']
    source, target = db['bucket'], db['telemetryBuckets'][args.resolution]
    tag, *details = getInfluxTag(config)
    detail = dict(zip(('second', 'minute', 'hour', 'day'), details))[args.resolution]
    quote = json.dumps
    report = {}
    with client_for(config) as client:
        query = client.query_api()
        for bucket in (source, target):
            tags = (f'import "influxdata/influxdb/schema"\n'
                    f'schema.measurementTagKeys(bucket: {quote(bucket)}, '
                    f'measurement: "electrical_telemetry", start: {start.isoformat()}, '
                    f'stop: {stop.isoformat()})')
            tag_names = sorted(str(row.get_value()) for row in query.query_stream(tags))
            counts = (f'from(bucket: {quote(bucket)})'
                      f' |> range(start: time(v: {quote(start.isoformat())}), '
                      f'stop: time(v: {quote(stop.isoformat())}))'
                      f' |> filter(fn: (r) => r._measurement == "electrical_telemetry" '
                      f'and r[{quote(tag)}] == {quote(detail)})'
                      ' |> group(columns: ["_field"]) |> count(column: "_value")')
            field_counts = {row.get_field(): row.get_value() for row in query.query_stream(counts)}
            report[bucket] = {'tag_keys': tag_names, 'field_counts': field_counts,
                              'rows': sum(field_counts.values())}
        if args.compare_field:
            identities = {}
            for bucket in (source, target):
                rows = (f'from(bucket: {quote(bucket)})'
                        f' |> range(start: time(v: {quote(start.isoformat())}), '
                        f'stop: time(v: {quote(stop.isoformat())}))'
                        f' |> filter(fn: (r) => r._measurement == "electrical_telemetry" '
                        f'and r[{quote(tag)}] == {quote(detail)} '
                        f'and r._field == {quote(args.compare_field)})')
                identities[bucket] = {
                    (row.get_time().isoformat(), row.values.get('account_name'),
                     row.values.get('device_gid'), row.values.get('channel_num'))
                    for row in query.query_stream(rows)}
            extra = sorted(identities[target] - identities[source])
            missing = sorted(identities[source] - identities[target])
            report['identity_comparison'] = {
                'field': args.compare_field, 'extra_target_count': len(extra),
                'missing_target_count': len(missing), 'extra_target_sample': extra[:10],
                'missing_target_sample': missing[:10]}
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
