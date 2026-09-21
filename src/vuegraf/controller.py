"""Single-worker admission and cached job status; no waiting collection queue."""
import copy
import datetime as dt
import threading
import time
import uuid


KINDS = ('minute', 'second', 'hour', 'day', 'history', 'recovery')


def timestamp():
    return dt.datetime.now(dt.UTC).isoformat()


class Busy(Exception):
    pass


class Controller:
    def __init__(self):
        self.condition = threading.Condition()
        self.stop = threading.Event()
        self.active = None
        self.ready = False
        self.fatal = None
        self.catalog = []
        self.storage = {'status': 'not_checked'}
        self.recovery = {}
        self.coverage = {'status': 'not_checked'}
        self.jobs = {kind: {'state': 'idle', 'last_success': None, 'last_result': None,
                            'next_run': None} for kind in KINDS}

    def snapshot(self):
        with self.condition:
            return copy.deepcopy({'checked_at': timestamp(), 'ready': self.ready,
                                  'error': self.fatal, 'active': self.active, 'jobs': self.jobs,
                                  'circuits': self.catalog, 'storage': self.storage,
                                  'recovery': self.recovery})

    def admit(self, kind, source, parameters=None):
        if kind not in KINDS:
            raise ValueError('Unknown collection type')
        with self.condition:
            if not self.ready or self.stop.is_set():
                raise Busy('Collector is not ready')
            if self.active is not None:
                raise Busy('Collector is running')
            self.active = {'id': uuid.uuid4().hex, 'kind': kind, 'source': source,
                           'started': timestamp(), 'parameters': parameters or {},
                           'progress': {'completed': 0, 'total': None}}
            self.jobs[kind].update(state='running', last_start=self.active['started'], trigger=source)
            self.condition.notify_all()
            return copy.deepcopy(self.active)

    def progress(self, completed, total=None):
        with self.condition:
            if self.active:
                self.active['progress'] = {'completed': completed, 'total': total}

    def finish(self, result, details=None):
        with self.condition:
            job = self.active
            if job is None:
                raise RuntimeError('No active collection')
            status = self.jobs[job['kind']]
            status.update(state='idle', last_completion=timestamp(), last_result=result,
                          details=details or {}, requested=job['parameters'])
            if result == 'success':
                status['last_success'] = status['last_completion']
            self.active = None
            self.condition.notify_all()

    def shutdown(self):
        self.stop.set()
        with self.condition:
            self.ready = False
            self.condition.notify_all()

    def serve(self, initialize, execute, intervals, cleanup=lambda: None, initial=None):
        """All initialization, Emporia access, writes and SQLite work stay here."""
        due = {kind: time.monotonic() + seconds for kind, seconds in intervals.items()}
        if 'minute' in due:
            due['minute'] = time.monotonic()
        try:
            initialize()
            with self.condition:
                self.ready = True
                if initial:
                    self.admit(initial['kind'], 'startup', initial.get('parameters'))
            while not self.stop.is_set():
                with self.condition:
                    now = time.monotonic()
                    for kind, deadline in due.items():
                        next_run = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=max(0, deadline - now))
                        self.jobs[kind]['next_run'] = next_run.isoformat()
                    if self.active is None:
                        eligible = [kind for kind in due if due[kind] <= now]
                        if eligible:
                            self.admit(min(eligible, key=due.get), 'scheduled')
                        else:
                            self.condition.wait(min(1, max(0, min(due.values(), default=now + 1) - now)))
                            continue
                    job = copy.deepcopy(self.active)
                result, details = 'failed', {}
                try:
                    details = execute(job) or {}
                    result = details.pop('result', 'success')
                    if self.stop.is_set():
                        result = 'interrupted'
                except Exception as error:
                    # Exception text may contain URLs or credentials.
                    details = {'error_type': type(error).__name__}
                finally:
                    if job['kind'] in due and job['source'] == 'scheduled':
                        # Even failed jobs wait before retrying. A failure is not a success marker.
                        due[job['kind']] = time.monotonic() + intervals[job['kind']]
                    self.finish(result, details)
        except Exception as error:
            with self.condition:
                self.fatal = type(error).__name__
        finally:
            self.shutdown()
            cleanup()
