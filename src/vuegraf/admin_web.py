"""Authenticated LAN-only admin UI/API. No shell or arbitrary database queries."""
import base64
import copy
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import urlsplit

from vuegraf.admin_data import Exports, FIELDS, RESOLUTIONS
from vuegraf.controller import Busy


def credentials():
    username = os.environ.get('VUEGRAF_ADMIN_USERNAME', '')
    password = os.environ.get('VUEGRAF_ADMIN_PASSWORD', '')
    if not username or not password or ':' in username:
        raise ValueError('Admin requires VUEGRAF_ADMIN_USERNAME and VUEGRAF_ADMIN_PASSWORD')
    return 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()


def specification():
    def operation(summary, properties=None, required=None, responses=None):
        result = {'summary': summary, 'responses': responses or {'200': {'description': 'JSON result'}}}
        result['responses'].update({'401': {'description': 'Authentication required'}})
        if properties is not None:
            schema = {'type': 'object', 'additionalProperties': False, 'properties': properties}
            if required:
                schema['required'] = required
            result['requestBody'] = {'required': True, 'content': {'application/json': {'schema': schema}}}
            result['parameters'] = [{'in': 'header', 'name': 'X-Vuegraf-Request', 'required': True,
                                     'schema': {'type': 'string', 'enum': ['1']}}]
            result['responses'].update({'400': {'description': 'Invalid request or exceeded export limits'},
                                        '403': {'description': 'Cross-origin or missing action header'}})
        return result

    paths = {
        '/api/status': {'get': operation('Cached collection/storage status; never triggers Emporia')},
        '/api/discovery': {'get': operation('Circuit IDs, units and resolutions')},
        '/api/coverage': {'get': operation('Cached first/last timestamps by circuit, metric and resolution; gaps may exist')},
        '/api/collect/minute': {'post': operation('Admit the native minute collection loop if idle; no queue', {},
            responses={'202': {'description': 'Admitted job'}, '409': {'description': 'Busy/not ready with current status'}})},
        '/api/collect/second': {'post': operation('Admit one collection if idle; no queue', {
            'lookback_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 10800, 'default': 3600},
            'circuits': {'type': 'array', 'maxItems': 64, 'items': {'type': 'string'}}
        }, responses={'202': {'description': 'Admitted job'}, '409': {'description': 'Busy/not ready with current status'}})},
        '/api/collect/hour': {'post': operation('Admit the native completed-hour collection loop if idle; no queue', {},
            responses={'202': {'description': 'Admitted job'}, '409': {'description': 'Busy/not ready with current status'}})},
        '/api/collect/day': {'post': operation('Admit the native previous-local-day collection loop if idle; no queue', {},
            responses={'202': {'description': 'Admitted job'}, '409': {'description': 'Busy/not ready with current status'}})},
        '/api/export': {'post': operation('CSV: maximum 100000 rows, 32 MiB, 15 seconds, two concurrent exports', {
            'start': {'type': 'string', 'format': 'date-time'}, 'stop': {'type': 'string', 'format': 'date-time'},
            'resolution': {'type': 'string', 'enum': list(RESOLUTIONS), 'default': 'minute'},
            'field': {'type': 'string', 'enum': list(FIELDS), 'default': 'power_watts'},
            'account': {'type': 'string'}, 'device': {'type': 'string'}, 'channel': {'type': 'string'},
            'aggregate': {'type': 'string', 'enum': ['raw', 'mean', 'sum', 'min', 'max']},
            'window': {'type': 'string', 'enum': ['1m', '5m', '15m', '1h', '1d']}
        }, ['start', 'stop'], {
            '200': {'description': 'CSV file', 'content': {'text/csv': {'schema': {'type': 'string'}}}},
            '429': {'description': 'Exports busy'}, '502': {'description': 'Database unavailable'}
        })}
    }
    time_schema = {'type': 'string', 'format': 'date-time', 'nullable': True}
    job_schema = {'type': 'object', 'properties': {
        'state': {'type': 'string', 'enum': ['idle', 'running']},
        'last_start': time_schema, 'last_completion': time_schema, 'last_success': time_schema,
        'next_run': time_schema, 'trigger': {'type': 'string'},
        'last_result': {'type': 'string', 'nullable': True, 'enum': ['success', 'partial', 'failed', 'interrupted', None]},
        'details': {'type': 'object', 'additionalProperties': True},
        'requested': {'type': 'object', 'additionalProperties': True}}}
    circuit_schema = {'type': 'object', 'required': ['id', 'account', 'device', 'channel', 'name'],
                      'properties': {key: {'type': 'string'} for key in ('id', 'account', 'device', 'channel', 'name', 'device_name')}}
    active_schema = {'type': 'object', 'nullable': True, 'properties': {
        'id': {'type': 'string'}, 'kind': {'type': 'string'}, 'source': {'type': 'string'},
        'started': time_schema, 'phase': {'type': 'string'}, 'parameters': {'type': 'object'},
        'progress': {'type': 'object', 'properties': {
            'completed': {'type': 'integer'}, 'total': {'type': 'integer', 'nullable': True}
        }}
    }}
    status_schema = {'type': 'object', 'properties': {
        'checked_at': time_schema, 'ready': {'type': 'boolean'}, 'error': {'type': 'string', 'nullable': True},
        'active': active_schema, 'jobs': {'type': 'object', 'additionalProperties': job_schema},
        'circuits': {'type': 'array', 'items': circuit_schema}, 'storage': {'type': 'object', 'additionalProperties': True},
        'recovery': {'type': 'object', 'additionalProperties': True}, 'legacy_energy_enabled': {'type': 'boolean'}}}
    schemas = {'Status': status_schema, 'Circuit': circuit_schema, 'Job': active_schema,
               'Discovery': {'type': 'object', 'properties': {'circuits': {'type': 'array', 'items': circuit_schema},
                             'fields': {'type': 'object', 'additionalProperties': {'type': 'string'}},
                             'max_range_days': {'type': 'object', 'additionalProperties': {'type': 'integer'}},
                             'timezone': {'type': 'string', 'nullable': True}, 'semantics': {'type': 'string'}}},
               'Coverage': {'type': 'object', 'properties': {'checked_at': time_schema, 'semantics': {'type': 'string'},
                            'status': {'type': 'string'}, 'error_type': {'type': 'string'},
                            'series': {'type': 'array', 'items': {'type': 'object', 'properties': {
                                **{key: {'type': 'string'} for key in ('account', 'device', 'channel', 'field', 'resolution')},
                                'first': time_schema, 'last': time_schema}}}}}}
    for path, schema in [('/api/status', 'Status'), ('/api/discovery', 'Discovery'), ('/api/coverage', 'Coverage')]:
        paths[path]['get']['responses']['200']['content'] = {'application/json': {'schema': {'$ref': '#/components/schemas/' + schema}}}
    for kind in ('minute', 'second', 'hour', 'day'):
        paths[f'/api/collect/{kind}']['post']['responses']['202']['content'] = {
            'application/json': {'schema': {'$ref': '#/components/schemas/Job'}}}
    return {'openapi': '3.0.3', 'info': {'title': 'VueGraf admin API', 'version': '1.1'},
            'security': [{'basicAuth': []}],
            'components': {'securitySchemes': {'basicAuth': {'type': 'http', 'scheme': 'basic'}}, 'schemas': schemas}, 'paths': paths}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, controller, config):
        self.auth = credentials()  # Fail before binding or starting collection.
        self.controller = controller
        self.config = config
        self.exports = Exports(config)
        self.downloads = threading.BoundedSemaphore(2)
        self.connections = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'VueGrafAdmin'

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *args):
        pass  # Paths, credentials, query filters and HTTP headers are never logged.

    def send(self, code, body, content_type='application/json'):
        if content_type == 'application/json':
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
        if code == 401:
            self.send_header('WWW-Authenticate', 'Basic realm="VueGraf", charset="UTF-8"')
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self):
        actual = self.headers.get('Authorization', '')
        if not hmac.compare_digest(actual.encode(), self.server.auth.encode()):
            self.send(401, {'error': 'Authentication required'})
            return False
        return True

    def do_GET(self):
        if not self.authenticated():
            return
        path = urlsplit(self.path).path
        if path == '/api/status':
            state = self.server.controller.snapshot()
            state['legacy_energy_enabled'] = self.server.config.get('legacyEnergyEnabled', True)
            self.send(200, state)
        elif path == '/api/discovery':
            self.send(200, {'circuits': self.server.controller.snapshot()['circuits'], 'fields': FIELDS,
                            'max_range_days': RESOLUTIONS, 'timezone': self.server.config.get('timezone'),
                            'semantics': 'UTC timestamps; stop exclusive; missing is not zero. Never sum mains and branches '
                                         'or merged circuits and their legs. Query one resolution at a time.'})
        elif path == '/api/openapi.json':
            self.send(200, specification())
        elif path == '/api/coverage':
            with self.server.controller.condition:
                observed = copy.deepcopy(self.server.controller.coverage)
            self.send(200, observed)
        elif path in ('/', '/admin.js', '/admin.css', '/api-docs'):
            asset, content = {'/': ('admin.html', 'text/html; charset=utf-8'),
                              '/admin.js': ('admin.js', 'text/javascript; charset=utf-8'),
                              '/admin.css': ('admin.css', 'text/css; charset=utf-8'),
                              '/api-docs': ('api.html', 'text/html; charset=utf-8')}[path]
            self.send(200, files('vuegraf').joinpath('web', asset).read_bytes(), content)
        else:
            self.send(404, {'error': 'Not found'})

    def do_POST(self):
        if not self.authenticated():
            return
        origin = self.headers.get('Origin')
        expected = 'http://' + self.headers.get('Host', '')
        if self.headers.get('X-Vuegraf-Request') != '1' or (origin is not None and origin != expected):
            self.send(403, {'error': 'Same-origin request and X-Vuegraf-Request: 1 required'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('Expected a JSON body of 1–16384 bytes')
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError('Expected an object')
            path = urlsplit(self.path).path
            if path == '/api/collect/second':
                if set(request) - {'circuits', 'lookback_seconds'}:
                    raise ValueError('Unknown collection parameter')
                seconds = request.get('lookback_seconds', 3600)
                if type(seconds) is not int or not 1 <= seconds <= 10800:
                    raise ValueError('lookback_seconds must be 1–10800')
                circuits = request.get('circuits', [])
                known = {item['id'] for item in self.server.controller.snapshot()['circuits']}
                if not isinstance(circuits, list) or len(circuits) > 64 or any(not isinstance(c, str) or c not in known for c in circuits):
                    raise ValueError('Unknown circuit IDs')
                job = self.server.controller.admit('second', 'manual', {'circuits': circuits, 'lookback_seconds': seconds})
                self.send(202, job)
            elif path in ('/api/collect/minute', '/api/collect/hour', '/api/collect/day'):
                if request:
                    raise ValueError('This collection type does not accept parameters')
                kind = path.rsplit('/', 1)[-1]
                self.send(202, self.server.controller.admit(kind, 'manual'))
            elif path == '/api/export':
                if not self.server.downloads.acquire(blocking=False):
                    raise BlockingIOError()
                try:
                    with self.server.exports.create(request) as data:
                        data.seek(0, 2)
                        length = data.tell()
                        data.seek(0)
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/csv; charset=utf-8')
                        self.send_header('Content-Disposition', 'attachment; filename="vuegraf.csv"')
                        self.send_header('Content-Length', str(length))
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        while chunk := data.read(65536):
                            self.wfile.write(chunk)
                finally:
                    self.server.downloads.release()
            else:
                self.send(404, {'error': 'Not found'})
        except Busy:
            self.send(409, {'error': 'Collector busy or not ready', 'status': self.server.controller.snapshot()})
        except BlockingIOError:
            self.send(429, {'error': 'Two exports are already running'})
        except (ValueError, KeyError, TypeError):
            self.send(400, {'error': 'Invalid request or export limit exceeded; see /api-docs'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send(502, {'error': 'Operation failed; source details are withheld'})
