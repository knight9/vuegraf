"""Read-only first/latest timestamps for a source and retention target."""
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
    args = parser.parse_args()
    with open(args.config) as stream:
        config = json.load(stream)
    start, stop = parse_time(args.start), parse_time(args.stop)
    db = config['influxDb']
    tag, *details = getInfluxTag(config)
    detail = dict(zip(('second', 'minute', 'hour', 'day'), details))[args.resolution]
    quote = json.dumps
    result = {}
    with client_for(config) as client:
        query = client.query_api()
        for bucket in (db['bucket'], db['telemetryBuckets'][args.resolution]):
            base = (f'from(bucket: {quote(bucket)})'
                    f' |> range(start: time(v: {quote(start.isoformat())}), '
                    f'stop: time(v: {quote(stop.isoformat())}))'
                    f' |> filter(fn: (r) => r._measurement == "electrical_telemetry" '
                    f'and r[{quote(tag)}] == {quote(detail)})')
            times = {}
            for name, selector in (('first', 'first()'), ('last', 'last()')):
                flux = (base + f' |> {selector} |> group()'
                        f' |> sort(columns: ["_time"], desc: {str(name == "last").lower()})'
                        ' |> limit(n: 1)')
                times[name] = next((row.get_time().isoformat()
                                    for row in query.query_stream(flux)), None)
            result[bucket] = times
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
