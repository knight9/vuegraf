"""Explicit, non-destructive provisioning/copy verification for InfluxDB 2.

Never modifies existing bucket policies or deletes source data. Run with a
private collector config; the config/token is never printed.
"""
import argparse
import datetime as dt
import hashlib
import json
from collections import defaultdict

from influxdb_client import Point
from influxdb_client.client.write_api import SYNCHRONOUS

from vuegraf.admin_data import client_for, parse_time
from vuegraf.config import getInfluxTag
from vuegraf.storage import RETENTION, provision, validate_routing


def migrate(config, resolution, start, stop, apply=False):
    validate_routing(config)
    db = config['influxDb']
    source = db['bucket']
    target = db['telemetryBuckets'][resolution]
    now = dt.datetime.now(dt.UTC)
    if not now - dt.timedelta(seconds=RETENTION[resolution] - 3600) <= start < stop <= now - dt.timedelta(minutes=5):
        raise ValueError('Copy range must fit destination retention with a one-hour margin, and stop at least five minutes ago')
    if (stop - start).total_seconds() > 31 * 86400:
        raise ValueError('At most 31 days per invocation; migrate longer ranges in separate runs')
    tag, *tags = getInfluxTag(config)
    detail = dict(zip(RETENTION, tags))[resolution]
    # The scanner already caps write memory at 1,000 points. Use source-safe
    # history windows here rather than one native sample window per query;
    # otherwise even a few days of second data needs thousands of Influx round
    # trips during a verified migration.
    chunk = {'second': 3600, 'minute': 12 * 3600,
             'hour': 20 * 86400, 'day': 20 * 86400}[resolution]
    keys = ['account_name', 'device_gid', 'channel_num', tag]

    def query(bucket, left, right):
        return (f'from(bucket: {json.dumps(bucket)}) |> range(start: time(v: {json.dumps(left.isoformat())}), '
                f'stop: time(v: {json.dumps(right.isoformat())}))'
                f' |> filter(fn: (r) => r._measurement == "electrical_telemetry" and r[{json.dumps(tag)}] == {json.dumps(detail)})'
                ' |> group(columns: ["_field"]) |> sort(columns: ' + json.dumps(['_time', *keys]) + ')')

    def scan(client, bucket, left, right, writer=None):
        digests, count, batch = defaultdict(hashlib.sha256), 0, []
        for row in client.query_api().query_stream(query(bucket, left, right)):
            values = row.values
            identity = [row.get_time().isoformat(), *[values[key] for key in keys], row.get_field(), row.get_value()]
            digests[row.get_field()].update((json.dumps(identity, separators=(',', ':'), ensure_ascii=True) + '\n').encode())
            count += 1
            if writer:
                point = Point('electrical_telemetry').time(row.get_time()).field(row.get_field(), row.get_value())
                for key in keys:
                    point.tag(key, values[key])
                batch.append(point)
                if len(batch) >= 1000:
                    writer.write(bucket=target, record=batch)
                    batch = []
        if batch:
            writer.write(bucket=target, record=batch)
        return count, {field: digest.hexdigest() for field, digest in digests.items()}

    report = {'source': source, 'target': target, 'applied': apply, 'verified_rows': 0, 'windows': 0}
    with client_for(config) as client:
        if client.buckets_api().find_bucket_by_name(target) is None:
            raise ValueError('Provision destination first')
        with client.write_api(write_options=SYNCHRONOUS) as writer:
            left = start
            while left < stop:
                right = min(left + dt.timedelta(seconds=chunk), stop)
                expected = scan(client, source, left, right, writer if apply else None)
                actual = scan(client, target, left, right)
                if expected != actual:
                    fields = sorted(name for name in set(expected[1]) | set(actual[1])
                                    if expected[1].get(name) != actual[1].get(name))
                    raise ValueError(
                        'Verification mismatch; source preserved. Do not cut over or delete data. '
                        f'window={left.isoformat()}..{right.isoformat()} '
                        f'source_rows={expected[0]} target_rows={actual[0]} fields={fields}')
                report['verified_rows'] += expected[0]
                report['windows'] += 1
                left = right
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config')
    parser.add_argument('action', choices=['provision', 'migrate'])
    parser.add_argument('--apply', action='store_true', help='Create new buckets or copy points; never deletes source data')
    parser.add_argument('--resolution', choices=list(RETENTION))
    parser.add_argument('--start')
    parser.add_argument('--stop')
    args = parser.parse_args()
    with open(args.config) as stream:
        config = json.load(stream)
    if args.action == 'provision':
        result = provision(config, args.apply)
    else:
        if not all([args.resolution, args.start, args.stop]):
            parser.error('migrate requires --resolution, --start and --stop')
        result = migrate(config, args.resolution, parse_time(args.start), parse_time(args.stop), args.apply)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
