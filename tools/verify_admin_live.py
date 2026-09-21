"""One bounded real-source LOCAL admin acceptance; never contacts production."""
import csv
import io
import json
import time
from pathlib import Path

import requests


def main():
    values = dict(line.split('=', 1) for line in Path('int/admin.env').read_text().splitlines()
                  if line and not line.startswith('#') and '=' in line)
    session = requests.Session()
    session.trust_env = False
    session.auth = (values['VUEGRAF_ADMIN_USERNAME'], values['VUEGRAF_ADMIN_PASSWORD'])
    base = 'http://127.0.0.1:3001'
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        status = session.get(base + '/api/status', timeout=5).json()
        assert not status['error'], status['error']
        if status['ready'] and not status['active']:
            break
        time.sleep(.5)
    assert status['ready'] and not status['active'], 'Collector did not become idle in 90 seconds'
    assert not status['legacy_energy_enabled']
    circuit = next(c for c in status['circuits'] if c['channel'] == '1')
    response = session.post(base + '/api/collect/second', json={'lookback_seconds': 60, 'circuits': [circuit['id']]},
                            headers={'X-Vuegraf-Request': '1'}, timeout=5)
    assert response.status_code == 202, response.status_code
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        status = session.get(base + '/api/status', timeout=5).json()
        if not status['active']:
            break
        time.sleep(.5)
    job = status['jobs']['second']
    assert job['last_result'] in ('success', 'partial'), job
    details = job['details']
    assert details['points_written'] > 0
    response = session.post(base + '/api/export', json={
        'start': details['start'], 'stop': details['stop'], 'resolution': 'second', 'field': 'power_watts',
        'account': circuit['account'], 'device': circuit['device'], 'channel': circuit['channel']},
        headers={'X-Vuegraf-Request': '1'}, timeout=20)
    assert response.status_code == 200, response.status_code
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert 0 < len(rows) <= 60
    print(json.dumps({'manual_second_result': job['last_result'], 'points_written': details['points_written'],
                      'power_samples_exported': len(rows), 'requested_seconds': 60,
                      'legacy_enabled': status['legacy_energy_enabled'], 'destination': 'local only'}))


if __name__ == '__main__':
    main()
