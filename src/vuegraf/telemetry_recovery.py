"""Durable coverage of acknowledged telemetry writes and bounded gap repair.

The ledger stores covered intervals, not a latest-timestamp watermark. Missing
or failed source intervals remain holes even when newer samples are written.
It is local collector state: keep it on a persistent volume with the database.
"""
import datetime as dt
import hashlib
import json
import logging
import math
import os
import sqlite3
from collections import defaultdict, deque

from pyemvue.enums import Scale

from vuegraf.telemetry import METRICS, TelemetryPoint, fetchChart, sampleWindow, selectedMetrics

logger = logging.getLogger('vuegraf.telemetry_recovery')
UTC = dt.UTC
SCALES = [Scale.SECOND.value, Scale.MINUTE.value, Scale.HOUR.value, Scale.DAY.value]
EXCLUDED = {'Balance', 'TotalUsage', 'MainsFromGrid', 'MainsToGrid'}


def intervals(rows):
    """Union overlapping/adjacent half-open intervals."""
    result = []
    for start, stop in sorted(rows):
        if start >= stop:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(stop, result[-1][1]))
        else:
            result.append((start, stop))
    return result


def holes(start, stop, covered):
    for left, right in intervals(covered):
        if right <= start:
            continue
        if left >= stop:
            break
        if left > start:
            yield start, min(left, stop)
        start = max(start, right)
    if start < stop:
        yield start, stop


def boundary(config, instant, scale):
    if scale == Scale.SECOND.value:
        return instant.replace(microsecond=0)
    if scale == Scale.MINUTE.value:
        return instant.replace(second=0, microsecond=0)
    if scale == Scale.HOUR.value:
        return instant.replace(minute=0, second=0, microsecond=0)
    return sampleWindow(config, instant, 0, scale)[0]


def initialize(config):
    settings = config.get('telemetry', {}).get('recovery', {})
    if not isinstance(settings, dict) or not isinstance(settings.get('enabled', False), bool):
        raise ValueError('telemetry.recovery must be an object with a boolean enabled setting')
    if not settings.get('enabled', False):
        return
    if not config.get('telemetry', {}).get('enabled', False):
        raise ValueError('Telemetry recovery requires telemetry.enabled')
    path = settings.get('statePath')
    if not isinstance(path, str) or not os.path.isabs(path) or path == ':memory:':
        raise ValueError('Telemetry recovery requires an absolute persistent statePath')
    for name, default, minimum, maximum in (
            ('initialLookbackSecs', 3600, 60, 7 * 86400),
            ('maxRequestsPerCycle', 3, 1, 100),
            ('unavailableAfterAttempts', 5, 1, 100),
            ('pauseSecs', 0.2, 0, 60)):
        value = settings.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('Invalid telemetry recovery setting: ' + name)
        if not minimum <= value <= maximum or (name != 'pauseSecs' and int(value) != value):
            raise ValueError('Out-of-range telemetry recovery setting: ' + name)
    if getattr(config.get('args'), 'dryrun', False):
        return  # No durable state changes in dry-run mode.
    if getattr(config.get('args'), 'resetdatabase', False):
        raise ValueError('Disable recovery and remove its ledger explicitly before resetting the database')
    config['_telemetryRecovery'] = Coverage(config, path, settings)


class Coverage:
    def __init__(self, config, path, settings):
        from vuegraf.destination import getTags
        self.config = config
        self.settings = dict(settings)
        self.tags = dict(zip(getTags(config)[1:], SCALES))
        if len(self.tags) != 4:
            raise ValueError('Recovery requires distinct resolution tag values')
        # Changing destinations invalidates coverage; credential rotation does not.
        identity = {name: {key: section.get(key) for key in
                    ('version', 'url', 'host', 'port', 'database', 'org', 'bucket')}
                    for name in ('influxDb', 'victoriaMetrics')
                    if (section := config.get(name))}
        identity.update(tags=getTags(config), timezone=config.get('timezone'))
        if config.get('influxDb', {}).get('telemetryBuckets'):
            identity['influxDb']['telemetryBuckets'] = config['influxDb']['telemetryBuckets']
        self.scope = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        # Refuse missing/unwritable parents instead of silently using ephemeral storage.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS streams (
                key TEXT PRIMARY KEY, start INTEGER NOT NULL, checked INTEGER NOT NULL DEFAULT 0,
                expired_seconds INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS coverage (key TEXT, start INTEGER, stop INTEGER);
            CREATE INDEX IF NOT EXISTS coverage_key ON coverage(key, start);
            CREATE TABLE IF NOT EXISTS deferred (
                key TEXT, start INTEGER, stop INTEGER, due INTEGER, attempts INTEGER);
            CREATE INDEX IF NOT EXISTS deferred_key ON deferred(key, start);
            CREATE TABLE IF NOT EXISTS unavailable (
                key TEXT, start INTEGER, stop INTEGER, since INTEGER,
                attempts INTEGER, reason TEXT);
            CREATE INDEX IF NOT EXISTS unavailable_key ON unavailable(key, start);
            CREATE TABLE IF NOT EXISTS control (
                scope TEXT, name TEXT, value INTEGER, PRIMARY KEY(scope, name));
        ''')

    def key(self, account, gid, channel, metric, scale):
        return json.dumps([self.scope, account, str(gid), str(channel), metric, scale])

    def record(self, points):
        if getattr(self.config.get('args'), 'dryrun', False):
            return
        samples = defaultdict(set)
        for point in points:
            if isinstance(point, TelemetryPoint) and point.detailed in self.tags:
                samples[(point.accountName, point.deviceGid, point.channelNum,
                         self.tags[point.detailed], point.timestamp)].add(point.metric)
        groups = defaultdict(list)
        for (account, gid, channel, scale, stamp), fields in samples.items():
            start, seconds = sampleWindow(self.config, stamp, 0, scale)
            for metric, (_, field) in METRICS.items():
                required = {field}
                if metric == 'energy':
                    required.add('power_watts')
                elif metric == 'current':
                    required.add('current_amps')
                if required <= fields:
                    groups[self.key(account, gid, channel, metric, scale)].append(
                        (int(start.timestamp()), int(start.timestamp()) + seconds))
        with self.db:
            for key, new in groups.items():
                old = self.db.execute('SELECT start, stop FROM coverage WHERE key=?', (key,)).fetchall()
                merged = intervals(old + new)
                self.db.execute('DELETE FROM coverage WHERE key=?', (key,))
                self.db.executemany('INSERT INTO coverage VALUES (?, ?, ?)',
                                    [(key, start, stop) for start, stop in merged])
                pending = self.db.execute('SELECT start, stop, due, attempts FROM deferred WHERE key=?',
                                          (key,)).fetchall()
                self.db.execute('DELETE FROM deferred WHERE key=?', (key,))
                for start, stop, due, attempts in pending:
                    self.db.executemany('INSERT INTO deferred VALUES (?, ?, ?, ?, ?)',
                                        [(key, left, right, due, attempts) for left, right in holes(start, stop, merged)])
                unavailable = self.db.execute(
                    'SELECT start, stop, since, attempts, reason FROM unavailable WHERE key=?',
                    (key,)).fetchall()
                self.db.execute('DELETE FROM unavailable WHERE key=?', (key,))
                for start, stop, since, attempts, reason in unavailable:
                    self.db.executemany('INSERT INTO unavailable VALUES (?, ?, ?, ?, ?, ?)',
                                        [(key, left, right, since, attempts, reason)
                                         for left, right in holes(start, stop, merged)])

    def missing(self, key, start, stop, now, honor_backoff=True):
        covered = self.db.execute('SELECT start, stop FROM coverage WHERE key=?', (key,)).fetchall()
        covered += self.db.execute('SELECT start, stop FROM unavailable WHERE key=?', (key,)).fetchall()
        if honor_backoff:
            covered += self.db.execute('SELECT start, stop FROM deferred WHERE key=? AND due>?',
                                       (key, now)).fetchall()
        return list(holes(start, stop, covered))

    def window(self, key, scale, stop, now):
        upper = int(stop.timestamp())
        lookback = int(self.settings.get('initialLookbackSecs', 3600))
        initial = boundary(self.config, stop - dt.timedelta(seconds=lookback), scale)
        lower = int(initial.timestamp())
        limit = {Scale.SECOND.value: 3 * 3600, Scale.MINUTE.value: 7 * 86400}.get(scale)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO streams(key, start) VALUES (?, ?)', (key, lower))
            lower, checked = self.db.execute('SELECT start, checked FROM streams WHERE key=?', (key,)).fetchone()
            if limit is not None and lower < now - limit:
                new_start = int(boundary(self.config, dt.datetime.fromtimestamp(now - limit, UTC), scale).timestamp())
                expired = sum(b - a for a, b in self.missing(key, lower, new_start, now, False))
                self.db.execute('UPDATE streams SET start=?, expired_seconds=expired_seconds+? WHERE key=?',
                                (new_start, expired, key))
                self.db.execute('DELETE FROM coverage WHERE key=? AND stop<=?', (key, new_start))
                self.db.execute('DELETE FROM deferred WHERE key=? AND stop<=?', (key, new_start))
                self.db.execute('DELETE FROM unavailable WHERE key=? AND stop<=?', (key, new_start))
                lower = new_start
                if expired:
                    logger.warning('Telemetry recovery: %s seconds of missing coverage expired at source', expired)
        missing = self.missing(key, lower, upper, now)
        if not missing:
            return None
        start = missing[0][0]
        # One request can repair several holes separated by already written
        # snapshots. Otherwise sparse polling can create holes faster than the
        # bounded per-stream repair budget can clear them. Never cross a window
        # still under source backoff.
        end = missing[-1][1]
        barrier = self.db.execute('SELECT MIN(start) FROM deferred WHERE key=? AND due>? AND start>?',
                                  (key, now, start)).fetchone()[0]
        if barrier is not None:
            end = min(end, barrier)
        batch = {Scale.SECOND.value: 3600, Scale.MINUTE.value: 12 * 3600,
                 Scale.HOUR.value: 20 * 86400, Scale.DAY.value: 20 * 86400}[scale]
        end = min(end, start + batch)
        if scale == Scale.DAY.value:
            end = int(boundary(self.config, dt.datetime.fromtimestamp(end, UTC), scale).timestamp())
        return checked, start, end

    def defer(self, key, start, stop, now):
        overlapping = self.db.execute(
            'SELECT start, stop, due, attempts FROM deferred WHERE key=? AND start<? AND stop>?',
            (key, stop, start)).fetchall()
        previous = max((row[3] for row in overlapping), default=0)
        attempts = previous + 1
        with self.db:
            self.db.execute('DELETE FROM deferred WHERE key=? AND start<? AND stop>?', (key, stop, start))
            for left, right, due, tries in overlapping:
                for a, b in ((left, min(right, start)), (max(left, stop), right)):
                    if a < b:
                        self.db.execute('INSERT INTO deferred VALUES (?, ?, ?, ?, ?)', (key, a, b, due, tries))
            self.db.execute('INSERT INTO deferred VALUES (?, ?, ?, ?, ?)',
                            (key, start, stop, now + min(3600, 300 * 2 ** min(previous, 4)), attempts))
        return attempts

    def mark_unavailable(self, key, start, stop, now, attempts, reason='empty_after_retries'):
        """Persist a source interval that should no longer consume recovery requests."""
        overlapping = self.db.execute(
            'SELECT start, stop, since, attempts, reason FROM unavailable '
            'WHERE key=? AND start<=? AND stop>=?', (key, stop, start)).fetchall()
        merged_start = min([start] + [row[0] for row in overlapping])
        merged_stop = max([stop] + [row[1] for row in overlapping])
        since = min([now] + [row[2] for row in overlapping])
        attempts = max([attempts] + [row[3] for row in overlapping])
        with self.db:
            pending = self.db.execute(
                'SELECT start, stop, due, attempts FROM deferred WHERE key=? AND start<? AND stop>?',
                (key, merged_stop, merged_start)).fetchall()
            self.db.execute('DELETE FROM deferred WHERE key=? AND start<? AND stop>?',
                            (key, merged_stop, merged_start))
            for left, right, due, tries in pending:
                for a, b in ((left, min(right, merged_start)), (max(left, merged_stop), right)):
                    if a < b:
                        self.db.execute('INSERT INTO deferred VALUES (?, ?, ?, ?, ?)',
                                        (key, a, b, due, tries))
            self.db.execute('DELETE FROM unavailable WHERE key=? AND start<=? AND stop>=?',
                            (key, merged_stop, merged_start))
            self.db.execute('INSERT INTO unavailable VALUES (?, ?, ?, ?, ?, ?)',
                            (key, merged_start, merged_stop, since, attempts, reason))

    def close(self):
        self.db.close()

    def control(self, name, value=None):
        if value is not None:
            with self.db:
                self.db.execute('INSERT INTO control VALUES (?, ?, ?) ON CONFLICT(scope, name) '
                                'DO UPDATE SET value=MAX(value, excluded.value)', (self.scope, name, value))
        row = self.db.execute('SELECT value FROM control WHERE scope=? AND name=?', (self.scope, name)).fetchone()
        return row[0] if row else 0

    def set_control(self, name, value):
        with self.db:
            self.db.execute('INSERT INTO control VALUES (?, ?, ?) ON CONFLICT(scope, name) '
                            'DO UPDATE SET value=excluded.value', (self.scope, name, value))


def recover(config, accounts, instant, seconds_due, pause):
    store = config.get('_telemetryRecovery')
    if store is None or pause.is_set() or getattr(config.get('args'), 'dryrun', False):
        return
    from vuegraf.destination import getTags, writeDataPoints
    tags = dict(zip(SCALES, getTags(config)[1:]))
    now = int(instant.timestamp())
    if now < store.control('cooldown'):
        return
    scales = [Scale.MINUTE.value]
    if config.get('detailedDataEnabled', False):
        for enabled, scale in [('detailedDataHoursEnabled', Scale.HOUR.value),
                               ('detailedDataDaysEnabled', Scale.DAY.value)]:
            if config.get(enabled, True):
                scales.append(scale)
        if config.get('detailedDataSecondsEnabled', True):
            if seconds_due:
                store.control('seconds_target', now)
            if store.control('seconds_target'):
                scales.insert(0, Scale.SECOND.value)
    jobs = []
    for account in accounts:
        if 'vue' not in account:
            continue
        channels = dict(account.get('_telemetryChannels', {}))
        for channel in account.get('channelIdMap', {}).values():
            channels[(channel.device_gid, str(channel.channel_num))] = channel
        for channel in channels.values():
            if str(channel.channel_num) in EXCLUDED:
                continue
            for scale in scales:
                stop = boundary(config, instant, scale)
                if scale == Scale.SECOND.value:
                    stop = dt.datetime.fromtimestamp(min(now, store.control('seconds_target')), UTC)
                for metric in selectedMetrics(config):
                    key = store.key(account['name'], channel.device_gid, channel.channel_num, metric, scale)
                    window = store.window(key, scale, stop, now)
                    if window:
                        jobs.append((window, key, account, channel, metric, scale))
    # Weighted round-robin across resolutions, oldest-attempted within each.
    # Minute repair must not wait behind every startup hour/day stream, but
    # coarse and second repairs also need guaranteed turns under minute load.
    queues = {
        scale: deque(sorted((job for job in jobs if job[-1] == scale),
                            key=lambda job: (job[0][0], job[0][1])))
        for scale in SCALES
    }
    slots = [Scale.MINUTE.value] * 6 + [Scale.SECOND.value] * 3 + [Scale.HOUR.value] * 2 + [Scale.DAY.value]
    turn = store.control('schedule_turn')
    selected = []
    while len(selected) < int(store.settings.get('maxRequestsPerCycle', 3)) and any(queues.values()):
        queue = queues[slots[turn % len(slots)]]
        turn += 1
        if queue:
            selected.append(queue.popleft())
    store.control('schedule_turn', turn)
    for (checked, start, stop), key, account, channel, metric, scale in selected:
        if pause.is_set():
            return
        with store.db:
            store.db.execute('UPDATE streams SET checked=? WHERE key=?', (now, key))
        points = []
        try:
            logger.info('Recovering telemetry: scale=%s metric=%s start=%s stop=%s',
                        scale, metric, dt.datetime.fromtimestamp(start, UTC), dt.datetime.fromtimestamp(stop, UTC))
            fetchChart(config, account, channel, metric, dt.datetime.fromtimestamp(start, UTC),
                       dt.datetime.fromtimestamp(stop, UTC), scale, tags[scale], points, cacheEmpty=False)
            if pause.is_set():
                return
            if points:
                writeDataPoints(config, points)
            for left, right in store.missing(key, start, stop, now, False):
                attempts = store.defer(key, left, right, now)
                threshold = int(store.settings.get('unavailableAfterAttempts', 5))
                if attempts >= threshold:
                    store.mark_unavailable(key, left, right, now, attempts)
                    logger.warning('Telemetry recovery marked source interval unavailable: '
                                   'scale=%s metric=%s missing_seconds=%s attempts=%s',
                                   scale, metric, right - left, attempts)
                else:
                    logger.info('Telemetry recovery incomplete: scale=%s metric=%s missing_seconds=%s; '
                                'retry deferred (attempt %s of %s)',
                                scale, metric, right - left, attempts, threshold)
            store.set_control('failure_attempts', 0)
        except Exception as error:
            store.defer(key, start, stop, now)
            failures = store.control('failure_attempts') + 1
            store.set_control('failure_attempts', failures)
            cooldown = min(3600, 300 * 2 ** min(failures - 1, 4))
            store.control('cooldown', now + cooldown)
            # Never log exception messages or URLs; they can contain credentials.
            status = getattr(getattr(error, 'response', None), 'status_code', None)
            logger.warning('Telemetry recovery interrupted (%s, status=%s); '
                           'coverage retained for retry after %s seconds',
                           type(error).__name__, status, cooldown)
            return  # Stop this cycle on authentication, rate-limit, transport or write errors.
        if pause.wait(store.settings.get('pauseSecs', 0.2)):
            return
