"""Bounded, parameterized InfluxDB exports. No arbitrary Flux endpoint."""
import csv
import datetime as dt
import io
import json
import tempfile
import threading
import time

from influxdb_client import InfluxDBClient

from vuegraf.config import getInfluxTag

FIELDS = {'power_watts': 'W', 'voltage_volts': 'V', 'current_amps': 'A', 'energy_kwh': 'kWh', 'charge_ah': 'Ah'}
RESOLUTIONS = {'second': 30, 'minute': 730, 'hour': 1825, 'day': 1825}


def bucket_for(config, resolution):
    return config['influxDb'].get('telemetryBuckets', {}).get(resolution, config['influxDb']['bucket'])


def client_for(config):
    db = config['influxDb']
    if db.get('version', 1) != 2:
        raise ValueError('Admin data access requires InfluxDB 2')
    return InfluxDBClient(url=db['url'], token=db['token'], org=db['org'],
                          verify_ssl=db.get('ssl_verify', True), timeout=15000)


def parse_time(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('Expected an ISO timestamp string')
    instant = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if instant.tzinfo is None:
        raise ValueError('Timestamps must include a timezone')
    return instant.astimezone(dt.UTC)


def export_query(config, request):
    if set(request) - {'start', 'stop', 'resolution', 'field', 'account', 'device', 'channel', 'aggregate', 'window'}:
        raise ValueError('Unknown query parameter')
    resolution, field = request.get('resolution', 'minute'), request.get('field', 'power_watts')
    if resolution not in RESOLUTIONS or field not in FIELDS:
        raise ValueError('Invalid resolution or field')
    start, stop = parse_time(request['start']), parse_time(request['stop'])
    if not 0 < (stop - start).total_seconds() <= RESOLUTIONS[resolution] * 86400:
        raise ValueError('Invalid or excessive date range')
    tag, *tags = getInfluxTag(config)
    detail = dict(zip(RESOLUTIONS, tags))[resolution]
    quote = json.dumps
    query = (f'from(bucket: {quote(bucket_for(config, resolution))})'
             f' |> range(start: time(v: {quote(start.isoformat())}), stop: time(v: {quote(stop.isoformat())}))'
             f' |> filter(fn: (r) => r._measurement == "electrical_telemetry"'
             f' and r._field == {quote(field)} and r[{quote(tag)}] == {quote(detail)})')
    for parameter, column in [('account', 'account_name'), ('device', 'device_gid'), ('channel', 'channel_num')]:
        if parameter in request:
            value = request[parameter]
            if not isinstance(value, str) or len(value) > 200:
                raise ValueError('Invalid series filter')
            query += f' |> filter(fn: (r) => r[{quote(column)}] == {quote(value)})'
    aggregate = request.get('aggregate', 'raw')
    if aggregate not in ('raw', 'mean', 'sum', 'min', 'max'):
        raise ValueError('Invalid aggregation')
    if aggregate == 'sum' and field not in ('energy_kwh', 'charge_ah'):
        raise ValueError('Only energy and charge may be summed over time')
    if aggregate != 'raw':
        window = request.get('window', '1h')
        if window not in ('1m', '5m', '15m', '1h', '1d'):
            raise ValueError('Invalid aggregation window')
        durations = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '1d': 86400}
        native = {'second': 1, 'minute': 60, 'hour': 3600, 'day': 86400}[resolution]
        if durations[window] < native:
            raise ValueError('Aggregation cannot be finer than source resolution')
        query += f' |> aggregateWindow(every: {window}, fn: {aggregate}, createEmpty: false)'
    # Limit each table too, but also enforce a GLOBAL row/byte/time bound below.
    return query + ' |> limit(n: 100001)', field


class Exports:
    def __init__(self, config):
        self.config = config
        self.slots = threading.BoundedSemaphore(2)

    def create(self, request):
        query, field = export_query(self.config, request)
        if not self.slots.acquire(blocking=False):
            raise BlockingIOError('Two exports are already running')
        output = None
        try:
            output = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode='w+b')
            started = time.monotonic()
            with client_for(self.config) as client:
                header = ['time', 'account', 'device', 'channel', 'metric', 'unit', 'value']
                output.write((','.join(header) + '\n').encode())
                for index, row in enumerate(client.query_api().query_stream(query)):
                    if index >= 100000 or time.monotonic() - started > 15:
                        raise ValueError('Export exceeds row/time limit; narrow the range or aggregate')
                    values = row.values
                    record = [row.get_time().isoformat(), values.get('account_name', ''),
                              values.get('device_gid', ''), values.get('channel_num', ''), field, FIELDS[field], row.get_value()]
                    # Neutralize spreadsheet formulas in untrusted string tags.
                    record = ["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v
                              for v in record]
                    buffer = io.StringIO()
                    csv.writer(buffer).writerow(record)
                    output.write(buffer.getvalue().encode())
                    if output.tell() > 32 * 1024 * 1024:
                        raise ValueError('Export exceeds 32 MiB; narrow the query')
            output.seek(0)
            return output
        except Exception:
            if output is not None:
                output.close()
            raise
        finally:
            self.slots.release()


def coverage(config):
    """Observed first/last samples, not a claim that intervening intervals are complete."""
    result = []
    tag, *tags = getInfluxTag(config)
    with client_for(config) as client:
        for resolution, detail in zip(RESOLUTIONS, tags):
            query = (f'base = from(bucket: {json.dumps(bucket_for(config, resolution))})'
                     f' |> range(start: -{RESOLUTIONS[resolution]}d)'
                     f' |> filter(fn: (r) => r._measurement == "electrical_telemetry" and r[{json.dumps(tag)}] == {json.dumps(detail)})'
                     ' |> filter(fn: (r) => contains(value: r._field, set: ' + json.dumps(list(FIELDS)) + '))\n'
                     'union(tables: [base |> first(), base |> last()])')
            entries = {}
            for row in client.query_api().query_stream(query):
                values = row.values
                key = (values['account_name'], values['device_gid'], values['channel_num'], row.get_field())
                item = entries.setdefault(key, {'account': key[0], 'device': key[1], 'channel': key[2],
                                                'field': key[3], 'resolution': resolution,
                                                'first': row.get_time().isoformat(), 'last': row.get_time().isoformat()})
                stamp = row.get_time().isoformat()
                item['first'], item['last'] = min(item['first'], stamp), max(item['last'], stamp)
                if len(entries) > 10000:
                    raise ValueError('Coverage series limit exceeded')
            result.extend(entries.values())
    return {'checked_at': dt.datetime.now(dt.UTC).isoformat(), 'series': result,
            'semantics': 'Observed bounds within retention query limits; gaps may exist between first and last.'}
