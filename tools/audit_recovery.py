"""Read-only timestamp/coverage audit; run inside the LOCAL collector container."""
import argparse
import datetime as dt
import json
import sqlite3
from collections import defaultdict

from influxdb_client import InfluxDBClient

parser = argparse.ArgumentParser()
parser.add_argument('--start', required=True)
parser.add_argument('--stop', required=True)
parser.add_argument('--detail', default='False', choices=['False', 'True', 'Hour', 'Day'])
parser.add_argument('--baseline-start', help='Include earlier series identities when checking an otherwise empty range')
parser.add_argument('--show-missing', action='store_true', help='Print individual series with missing timestamps')
parser.add_argument('--compare-minute-series', action='store_true', help='Include minute-series identities when auditing seconds')
args = parser.parse_args()
start = dt.datetime.fromisoformat(args.start.replace('Z', '+00:00'))
stop = dt.datetime.fromisoformat(args.stop.replace('Z', '+00:00'))
with open('/opt/vuegraf/conf/vuegraf.json') as stream:
    config = json.load(stream)
db = config['influxDb']
query = (f'from(bucket: {json.dumps(db["bucket"])})'
         f' |> range(start: time(v: {json.dumps(args.baseline_start or args.start)}), stop: time(v: {json.dumps(args.stop)}))'
         ' |> filter(fn: (r) => r._measurement == "electrical_telemetry")'
         f' |> filter(fn: (r) => r.detailed == {json.dumps(args.detail)}'
         + (' or r.detailed == "False"' if args.compare_minute_series else '') + ')'
         ' |> filter(fn: (r) => r._field == "power_watts" or r._field == "energy_kwh"'
         ' or r._field == "current_amps" or r._field == "voltage_volts")')
series = defaultdict(set)
with InfluxDBClient(url=db['url'], token=db['token'], org=db['org'], timeout=120000) as client:
    for row in client.query_api().query_stream(query):
        times = series[(row.values['account_name'], row.values['device_gid'], row.values['channel_num'], row.get_field())]
        if row.values['detailed'] == args.detail and start <= row.get_time() < stop:
            times.add(row.get_time())
seconds = {'False': 60, 'True': 1, 'Hour': 3600, 'Day': 86400}[args.detail]
expected = {start + dt.timedelta(seconds=i * seconds) for i in range(int((stop - start).total_seconds() / seconds))}
summary = defaultdict(lambda: {'series': 0, 'samples': 0, 'complete_series': 0, 'missing_samples': 0})
missing = []
for key, times in sorted(series.items()):
    item = summary[key[-1]]
    item['series'] += 1
    item['samples'] += len(times)
    absent = expected - times
    item['missing_samples'] += len(absent)
    item['complete_series'] += int(not absent)
    if absent:
        missing.append({'device': key[1], 'channel': key[2], 'field': key[3],
                        'missing': [stamp.isoformat() for stamp in sorted(absent)[:12]], 'count': len(absent)})
result = {'range': [args.start, args.stop], 'resolution': args.detail,
          'note': 'Series identities are drawn from the requested range plus the optional baseline window.',
          'fields': dict(summary), 'incomplete_series': len(missing)}
if args.show_missing:
    result['missing'] = missing
path = config.get('telemetry', {}).get('recovery', {}).get('statePath')
if path:
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as ledger:
        result['ledger'] = {table: ledger.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                            for table in ['streams', 'coverage', 'deferred']}
        result['ledger']['expired_seconds'] = ledger.execute('SELECT sum(expired_seconds) FROM streams').fetchone()[0]
        result['ledger']['resolutions'] = sorted({json.loads(row[0])[-1] for row in ledger.execute('SELECT key FROM streams')})
print(json.dumps(result, indent=2))
