import threading

import pytest
import requests

from vuegraf.admin_web import Server, credentials, specification
from vuegraf.controller import Controller


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv('VUEGRAF_ADMIN_USERNAME', 'tester')
    monkeypatch.setenv('VUEGRAF_ADMIN_PASSWORD', 'dummy-password')
    controller = Controller()
    controller.ready = True
    controller.catalog = [{'id': 'circuit1'}]
    server = Server(('127.0.0.1', 0), controller, {'legacyEnergyEnabled': False})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = requests.Session()
    session.trust_env = False
    session.auth = ('tester', 'dummy-password')
    yield session, f'http://127.0.0.1:{server.server_port}', controller
    server.shutdown()
    server.server_close()
    thread.join(2)
    session.close()


def test_auth_required_everywhere(server):
    session, url, _ = server
    session.auth = ('bad', 'bad')
    for path in ('/', '/api-docs', '/admin.js', '/api/status', '/api/openapi.json'):
        response = session.get(url + path, timeout=3)
        assert response.status_code == 401
        assert 'WWW-Authenticate' in response.headers


def test_status_static_and_openapi(server):
    session, url, _ = server
    status = session.get(url + '/api/status', timeout=3).json()
    assert not status['legacy_energy_enabled']
    page = session.get(url + '/', timeout=3)
    assert page.status_code == 200
    assert '<pre id="storage"' not in page.text
    assert '<pre id="recovery"' not in page.text
    assert session.get(url + '/admin.js', timeout=3).status_code == 200
    spec = session.get(url + '/api/openapi.json', timeout=3).json()
    assert spec == specification()
    for kind in ('minute', 'second', 'hour', 'day'):
        assert f'/api/collect/{kind}' in spec['paths']


@pytest.mark.parametrize('kind', ('minute', 'hour', 'day'))
def test_manual_native_collection_endpoints(server, kind):
    session, url, controller = server
    endpoint = url + f'/api/collect/{kind}'
    headers = {'X-Vuegraf-Request': '1'}
    assert session.post(endpoint, json={'unexpected': True}, headers=headers, timeout=3).status_code == 400
    response = session.post(endpoint, json={}, headers=headers, timeout=3)
    assert response.status_code == 202
    assert response.json()['kind'] == kind
    assert response.json()['source'] == 'manual'
    assert response.json()['parameters'] == {}
    assert controller.snapshot()['active']['kind'] == kind


def test_csrf_busy_and_unknown_circuit(server):
    session, url, controller = server
    endpoint = url + '/api/collect/second'
    assert session.post(endpoint, json={}, timeout=3).status_code == 403
    headers = {'X-Vuegraf-Request': '1'}
    assert session.post(endpoint, json={}, headers={**headers, 'Origin': 'http://evil.test'}, timeout=3).status_code == 403
    assert session.post(endpoint, json={'circuits': ['unknown']}, headers=headers, timeout=3).status_code == 400
    assert session.post(endpoint, json={'lookback_seconds': True}, headers=headers, timeout=3).status_code == 400
    assert session.post(endpoint, json={'circuits': ['circuit1']}, headers=headers, timeout=3).status_code == 202
    response = session.post(endpoint, json={}, headers=headers, timeout=3)
    assert response.status_code == 409
    assert response.json()['status']['active']['id'] == controller.active['id']


def test_missing_credentials_fail_closed(monkeypatch):
    monkeypatch.delenv('VUEGRAF_ADMIN_PASSWORD', raising=False)
    with pytest.raises(ValueError):
        credentials()
