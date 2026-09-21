"""Execute generated example-dashboard Flux against the local disposable fixture."""
import json
import sys
from pathlib import Path

from influxdb_client import InfluxDBClient


with InfluxDBClient(url='http://influxdb:8086', org='admin-test', token='admin-test-only-token') as client:
    queries = 0
    for path in Path(sys.argv[1]).glob('*.json'):
        dashboard = json.loads(path.read_text())
        targets = [t['query'] for panel in dashboard['panels'] for t in panel.get('targets', [])]
        targets += [v['query'] for v in dashboard['templating']['list'] if v.get('type') == 'query']
        for detail in ['True', 'False', 'Hour', 'Day']:
            for query in targets:
                for key, replacement in {'${detail}': detail, '${metric}': 'power_watts',
                                         '${account:regex}': '.*', '${device:regex}': '.*', '${channel:regex}': '.*',
                                         'v.timeRangeStart': '-2d', 'v.timeRangeStop': 'now()', 'v.windowPeriod': '1m'}.items():
                    query = query.replace(key, replacement)
                assert 'v.defaultBucket' not in query
                client.query_api().query(query)
                queries += 1
    assert queries > 0
    print(f'PASS: {queries} generated Grafana panel/variable queries execute across all four resolutions')
