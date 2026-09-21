"""Bounded end-to-end acceptance against the local disposable admin fixture."""
import csv
import io
import time

import requests


def main():
    base = 'http://127.0.0.1:18080'
    session = requests.Session()
    session.trust_env = False
    session.auth = ('admin', 'admin')
    headers = {'X-Vuegraf-Request': '1'}
    deadline = time.monotonic() + 45
    status = {}
    while time.monotonic() < deadline:
        try:
            status = session.get(base + '/api/status', timeout=5).json()
        except requests.ConnectionError:
            time.sleep(.5)
            continue
        assert not status.get('error'), status
        if status['ready'] and not status['active'] and status['jobs']['minute']['last_result']:
            break
        time.sleep(.5)
    assert status['ready'] and not status['active'], status
    assert not status['legacy_energy_enabled']
    assert session.get(base + '/api/status', auth=('wrong', 'wrong'), timeout=5).status_code == 401
    assert session.post(base + '/api/collect/second', json={}, timeout=5).status_code == 403
    request = {'lookback_seconds': 60, 'circuits': [status['circuits'][0]['id']]}
    job = session.post(base + '/api/collect/second', json=request, headers=headers, timeout=5)
    assert job.status_code == 202, job.text
    busy = session.post(base + '/api/collect/second', json=request, headers=headers, timeout=5)
    assert busy.status_code == 409, busy.text
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = session.get(base + '/api/status', timeout=5).json()
        if not status['active']:
            break
        time.sleep(.2)
    assert status['jobs']['second']['last_result'] == 'success', status['jobs']['second']
    details = status['jobs']['second']['details']
    response = session.post(base + '/api/export', headers=headers, timeout=20, json={
        'resolution': 'second', 'field': 'power_watts', 'start': details['start'], 'stop': details['stop']})
    assert response.status_code == 200, response.text
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 60, len(rows)
    assert all(abs(float(row['value']) - 3600) < .001 for row in rows)
    buckets = {b['name']: b['retention_seconds'] for b in status['storage']['buckets']}
    assert status['storage']['database_scan_complete'] is True
    assert status['storage']['database_bytes'] > 0
    assert buckets['seconds-test'] == [30 * 86400]
    assert buckets['minutes-test'] == [730 * 86400]
    assert buckets['coarse-test'] == [1825 * 86400]
    assert session.get(base + '/api/openapi.json', timeout=5).json()['openapi'] == '3.0.3'
    print('PASS: authentication, CSRF, mutex/no queue, successful manual seconds, exact CSV values, routed retention buckets, OpenAPI')
    print('Fixture uses fake data only; production and Emporia were not contacted.')


if __name__ == '__main__':
    main()
