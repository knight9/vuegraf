"""Opt-in admin collector adapter. Only the worker owns collection connections."""
import datetime as dt
import json
import os
import threading
import time

from vuegraf.collect import collectUsage, collectHistoryUsage
from vuegraf.controller import Controller
from vuegraf.destination import initConnection, closeConnection, writeDataPoints, getTags
from vuegraf.device import initDeviceAccount, lookupChannelName, lookupDeviceName
from vuegraf.storage import Storage
from vuegraf.telemetry import fetchChart, selectedMetrics
from vuegraf.telemetry_history import discoverChannels
from vuegraf.telemetry_recovery import recover, boundary
from vuegraf.time import getCurrentDayLocal


def circuit_id(account, channel):
    return json.dumps([account['name'], str(channel.device_gid), str(channel.channel_num)], separators=(',', ':'))


class Runtime:
    def __init__(self, config):
        self.config = config
        self.controller = Controller()
        self.storage = Storage(config)
        self.channels = []
        self.last_second_stop = None
        self.previous_hour = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
        self.previous_day = getCurrentDayLocal(config)
        self.thread = None
        self.next_coverage = 0

    def initialize(self):
        initConnection(self.config)
        self.config['_collectionProgress'] = self.controller.progress
        for account in self.config['accounts']:
            initDeviceAccount(self.config, account)
            for channel in discoverChannels(self.config, account, dt.datetime.now(dt.UTC)):
                self.channels.append((account, channel))
        catalog = [{'id': circuit_id(account, channel), 'account': account['name'],
                    'device': str(channel.device_gid), 'channel': str(channel.channel_num),
                    'name': lookupChannelName(account, channel), 'device_name': lookupDeviceName(account, channel.device_gid)}
                   for account, channel in self.channels]
        with self.controller.condition:
            self.controller.catalog = catalog
        storage = self.storage.refresh()
        with self.controller.condition:
            self.controller.storage = storage

    def cleanup(self):
        closeConnection(self.config)
        if 'influx' in self.config:
            self.config['influx'].close()

    def execute(self, job):
        kind = job['kind']
        now = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=self.config.get('lagSecs', 5))).replace(microsecond=0)
        stop_event = self.controller.stop
        points = []
        if kind == 'second':
            selected = job['parameters'].get('circuits', [])
            channels = [(a, c) for a, c in self.channels if not selected or circuit_id(a, c) in selected]
            lookback = job['parameters'].get('lookback_seconds', 3600)
            start = now - dt.timedelta(seconds=lookback)
            if job['source'] == 'scheduled' and self.last_second_stop:
                start = max(self.last_second_stop, now - dt.timedelta(hours=3))
            total = len(channels) * len(selectedMetrics(self.config))
            completed, unavailable, count = 0, 0, 0
            for account, channel in channels:
                for metric in selectedMetrics(self.config):
                    left = start
                    while left < now:
                        if stop_event.is_set():
                            return {'result': 'interrupted'}
                        right = min(left + dt.timedelta(hours=1), now)
                        batch = []
                        fetchChart(self.config, account, channel, metric, left, right, '1S',
                                   getTags(self.config)[1], batch, cacheEmpty=False)
                        present = len({p.timestamp for p in batch})
                        if present < int((right - left).total_seconds()):
                            unavailable += 1
                        if batch:
                            if self.config.get('legacyEnergyEnabled', True):
                                from vuegraf.collect import Point
                                batch += [Point(p.accountName, p.deviceName, p.chanName, p.value, p.timestamp, p.detailed)
                                          for p in batch if p.metric == 'power_watts']
                            writeDataPoints(self.config, batch)
                            count += len(batch)
                        left = right
                        if stop_event.wait(.2):
                            return {'result': 'interrupted'}
                    completed += 1
                    self.controller.progress(completed, total)
            # This is an acknowledged fetch boundary, NOT complete source coverage.
            # Missing source readings remain explicitly partial and eligible in the ledger.
            if job['source'] == 'scheduled' and unavailable == 0:
                self.last_second_stop = now
            if job['source'] == 'scheduled':
                self.repair(now, True)
            return {'result': 'partial' if unavailable else 'success', 'unavailable_requests': unavailable,
                    'points_written': count, 'start': start.isoformat(), 'stop': now.isoformat()}
        if kind == 'minute':
            for index, account in enumerate(self.config['accounts']):
                account.pop('_telemetryCycleError', None)
                collectUsage(self.config, account, None, now, False, points, None, '1MIN')
                self.controller.progress(index + 1, len(self.config['accounts']))
            writeDataPoints(self.config, points)
            failed = any(a.get('_telemetryCycleError') for a in self.config['accounts'])
            # Recovery is part of this same admitted job: manual requests cannot overlap it.
            self.repair(now, False)
            storage = self.storage.refresh()
            if time.monotonic() >= self.next_coverage:
                from vuegraf.admin_data import coverage
                self.next_coverage = time.monotonic() + 300
                try:
                    observed = coverage(self.config)
                except Exception as error:
                    observed = {'status': 'unavailable', 'error_type': type(error).__name__}
                with self.controller.condition:
                    self.controller.coverage = observed
            with self.controller.condition:
                self.controller.storage = storage
            return {'result': 'partial' if failed else 'success', 'points_written': len(points), 'through': now.isoformat()}
        if kind in ('hour', 'day'):
            scale = '1H' if kind == 'hour' else '1D'
            if kind == 'hour':
                start = now.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
            else:
                start = (getCurrentDayLocal(self.config) - dt.timedelta(days=1)).astimezone(dt.UTC)
            for account in self.config['accounts']:
                account.pop('_telemetryCycleError', None)
                collectUsage(self.config, account, start, start, False, points, None, scale)
            writeDataPoints(self.config, points)
            failed = any(a.get('_telemetryCycleError') for a in self.config['accounts'])
            return {'result': 'partial' if failed else 'success', 'points_written': len(points), 'start': start.isoformat()}
        if kind == 'history':
            days = job['parameters'].get('days', 1)
            for account in self.config['accounts']:
                account.pop('_telemetryCycleError', None)
                collectHistoryUsage(self.config, account, now - dt.timedelta(days=days), now, points, stop_event)
            writeDataPoints(self.config, points)
            failed = any(a.get('_telemetryCycleError') for a in self.config['accounts'])
            return {'result': 'partial' if failed else 'success', 'through': now.isoformat()}
        if kind == 'recovery':
            self.repair(now, False)
            return {}
        raise ValueError('Unknown job')

    def repair(self, instant, seconds_due):
        from vuegraf.controller import timestamp
        store = self.config.get('_telemetryRecovery')
        if not store:
            return
        with self.controller.condition:
            status = self.controller.jobs['recovery']
            status.update(state='running', last_start=timestamp(), trigger='scheduled')
            if self.controller.active:
                self.controller.active['phase'] = 'recovery'
        try:
            recover(self.config, self.config['accounts'], instant, seconds_due, self.controller.stop)
        finally:
            pending = store.db.execute('SELECT count(*) FROM deferred').fetchone()[0]
            cooldown = store.control('cooldown') > instant.timestamp()
            remaining = {}
            stream_count, expired, unavailable_count, unavailable_seconds = 0, 0, 0, 0
            for key, start, expired_seconds in store.db.execute('SELECT key, start, expired_seconds FROM streams'):
                identity = json.loads(key)
                if identity[0] != store.scope:
                    continue
                scale = identity[-1]
                upper = int(boundary(self.config, instant, scale).timestamp())
                if scale == '1S':
                    upper = min(upper, store.control('seconds_target'))
                holes = store.missing(key, start, upper, int(instant.timestamp()), False) if upper > start else []
                summary = remaining.setdefault(scale, {'streams_with_gaps': 0, 'missing_stream_seconds': 0,
                                                       'oldest_gap': None})
                if holes:
                    summary['streams_with_gaps'] += 1
                    summary['missing_stream_seconds'] += sum(right - left for left, right in holes)
                    oldest = dt.datetime.fromtimestamp(holes[0][0], dt.UTC).isoformat()
                    summary['oldest_gap'] = min(summary['oldest_gap'] or oldest, oldest)
                unavailable = store.db.execute(
                    'SELECT start, stop FROM unavailable WHERE key=? AND start<? AND stop>?',
                    (key, upper, start)).fetchall() if upper > start else []
                unavailable_count += len(unavailable)
                unavailable_seconds += sum(min(right, upper) - max(left, start)
                                           for left, right in unavailable)
                stream_count += 1
                expired += expired_seconds
            incomplete = (cooldown or unavailable_count > 0 or
                          any(s['streams_with_gaps'] for s in remaining.values()))
            with self.controller.condition:
                self.controller.recovery = {'streams': stream_count, 'deferred_intervals': pending,
                                            'permanently_unavailable_intervals': unavailable_count,
                                            'permanently_unavailable_stream_seconds': unavailable_seconds,
                                            'expired_stream_seconds': expired, 'remaining': remaining,
                                            'note': 'Durations sum across channel/metric streams; not wall-clock outage duration.'}
                status.update(state='idle', last_completion=timestamp(),
                              last_result='partial' if incomplete else 'success',
                              details={'deferred_intervals': pending,
                                       'permanently_unavailable_intervals': unavailable_count,
                                       'cooldown': cooldown})
                if not incomplete:
                    status['last_success'] = status['last_completion']

    def start(self):
        intervals = {'minute': max(10, self.config.get('updateIntervalSecs', 60))}
        if self.config.get('detailedDataEnabled', False):
            for flag, kind, seconds in [('detailedDataSecondsEnabled', 'second', self.config.get('detailedIntervalSecs', 3600)),
                                        ('detailedDataHoursEnabled', 'hour', 3600), ('detailedDataDaysEnabled', 'day', 86400)]:
                if self.config.get(flag, True) and seconds > 0:
                    intervals[kind] = seconds
        days = min(getattr(self.config.get('args'), 'historydays', 0), self.config.get('maxHistoryDays', 720))
        initial = {'kind': 'history', 'parameters': {'days': days}} if days > 0 else None
        self.thread = threading.Thread(target=self.controller.serve,
                                       args=(self.initialize, self.execute, intervals, self.cleanup, initial),
                                       name='vuegraf-collector', daemon=True)
        self.thread.start()

    def stop(self):
        self.controller.shutdown()
        if self.thread:
            self.thread.join(timeout=30)


def serve_admin(config, stop):
    from vuegraf.admin_web import Server
    if not config.get('telemetry', {}).get('enabled', False):
        raise ValueError('Admin requires telemetry.enabled')
    if config['args'].resetdatabase or config['args'].dryrun:
        raise ValueError('Admin cannot run with resetdatabase or dryrun')
    runtime = Runtime(config)
    server = Server((os.environ.get('VUEGRAF_ADMIN_BIND', '127.0.0.1'),
                     int(os.environ.get('VUEGRAF_ADMIN_PORT', '8080'))), runtime.controller, config)
    runtime.start()
    web = threading.Thread(target=server.serve_forever, name='vuegraf-http', daemon=True)
    web.start()
    try:
        while not stop.wait(.5):
            if runtime.controller.fatal:
                raise RuntimeError('Collector initialization failed; inspect authenticated status')
    finally:
        server.shutdown()
        server.server_close()
        runtime.stop()
