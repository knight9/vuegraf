"""Cached storage observations and non-destructive retention provisioning."""
import datetime as dt
import os
import time
from urllib.parse import urlsplit
from collections import deque

import requests

from vuegraf.admin_data import client_for
from vuegraf.disk_stats import measure, read_snapshot

RETENTION = {'second': 30 * 86400, 'minute': 730 * 86400, 'hour': 1825 * 86400, 'day': 1825 * 86400}


def validate_routing(config):
    db = config.get('influxDb', {})
    mapping = db.get('telemetryBuckets', {})
    if not mapping:
        return
    if db.get('version', 1) != 2 or set(mapping) != set(RETENTION):
        raise ValueError('telemetryBuckets requires InfluxDB 2 and all four resolutions')
    if any(not isinstance(value, str) or not value.strip() for value in mapping.values()):
        raise ValueError('Invalid telemetry bucket name')
    for name in set(mapping.values()):
        policies = {RETENTION[resolution] for resolution in mapping if mapping[resolution] == name}
        if len(policies) != 1 or name == db['bucket']:
            raise ValueError('Different retention periods and legacy data require separate buckets')


def provision(config, apply=False):
    """Create NEW buckets only; never shorten existing retention or delete records."""
    validate_routing(config)
    mapping = config['influxDb'].get('telemetryBuckets', {})
    if not mapping:
        raise ValueError('Configure telemetryBuckets first')
    plan = []
    with client_for(config) as client:
        api = client.buckets_api()
        for name in sorted(set(mapping.values())):
            duration = next(RETENTION[key] for key in mapping if mapping[key] == name)
            bucket = api.find_bucket_by_name(name)
            actual = [rule.every_seconds for rule in bucket.retention_rules] if bucket else None
            if bucket and actual != [duration]:
                raise ValueError('Existing bucket retention differs; refusing to expire existing data: ' + name)
            plan.append({'bucket': name, 'retention_seconds': duration, 'action': 'exists' if bucket else 'create'})
        if apply:
            for item in plan:
                if item['action'] == 'create':
                    api.create_bucket(bucket_name=item['bucket'], org=config['influxDb']['org'],
                                      retention_rules=[{'type': 'expire', 'everySeconds': item['retention_seconds']}])
    return plan


class Storage:
    def __init__(self, config):
        self.config = config
        self.next_check = 0
        self.samples = deque(maxlen=288)
        self.cached = {'status': 'not_checked'}
        self.last_alert = 0
        self.alert_status = {}

    def refresh(self):
        if time.monotonic() < self.next_check:
            return self.cached
        self.next_check = time.monotonic() + 300
        result = {'checked_at': dt.datetime.now(dt.UTC).isoformat(), 'status': 'ok',
                  'quota': {'status': 'No verified hard quota configured'},
                  'alerts': {'delivery': 'UI only; no out-of-band destination configured'},
                  'retention_targets_seconds': RETENTION, 'buckets': []}
        path = self.config.get('admin', {}).get('storagePath')
        snapshot = self.config.get('admin', {}).get('storageSnapshotPath')
        if path or snapshot:
            try:
                measured = read_snapshot(snapshot) if snapshot else measure(path)
                result.update(measured)
                if result.get('database_scan_complete'):
                    stamp = dt.datetime.fromisoformat(measured['checked_at']).timestamp()
                    size = result['database_bytes']
                    if not self.samples or stamp > self.samples[-1][0]:
                        self.samples.append((stamp, size))
                if result.get('database_scan_complete') and len(self.samples) > 1 and self.samples[-1][0] - self.samples[0][0] >= 3600:
                    seconds = self.samples[-1][0] - self.samples[0][0]
                    rate = (self.samples[-1][1] - self.samples[0][1]) / seconds
                    result['growth_estimate'] = {'bytes_per_day': rate * 86400, 'observation_seconds': seconds,
                                                 'days_to_disk_full': result['filesystem']['free'] / rate / 86400 if rate > 0 else None}
            except (OSError, ValueError, KeyError, TypeError) as error:
                result['status'] = 'partial'
                result['filesystem_error'] = type(error).__name__
                result['database_scan_complete'] = False
        else:
            result['filesystem'] = {'status': 'Unavailable: configure the storage-monitor snapshot'}
        try:
            with client_for(self.config) as client:
                db = self.config['influxDb']
                mapping = db.get('telemetryBuckets', {})
                for name in sorted({db['bucket'], *mapping.values()}):
                    bucket = client.buckets_api().find_bucket_by_name(name)
                    rules = [rule.every_seconds for rule in bucket.retention_rules] if bucket else None
                    result['buckets'].append({'name': name, 'retention_seconds': rules,
                                              'unlimited': bucket is not None and (not rules or 0 in rules),
                                              'resolutions': [key for key in RETENTION if mapping.get(key, db['bucket']) == name],
                                              'contains_legacy': name == db['bucket']})
        except Exception as error:
            result['status'] = 'partial'
            result['retention_error'] = type(error).__name__
        webhook = os.environ.get('VUEGRAF_ALERT_WEBHOOK')
        if webhook:
            address = urlsplit(webhook)
            if address.scheme not in ('http', 'https') or not address.hostname:
                result['alerts'] = {'delivery': 'invalid webhook configuration'}
            else:
                result['alerts'] = {'delivery': 'webhook', 'destination_host': address.hostname,
                                    'last_attempt_epoch': self.last_alert or None, **self.alert_status}
                if result.get('warning') and time.time() - self.last_alert >= 3600:
                    self.last_alert = time.time()
                    try:
                        response = requests.post(webhook, json={'service': 'vuegraf', 'warning': 'Low disk space',
                                                                'filesystem': result['filesystem']}, timeout=3,
                                                 allow_redirects=False)
                        result['alerts']['last_result'] = 'sent' if 200 <= response.status_code < 300 else 'failed'
                    except requests.RequestException:
                        result['alerts']['last_result'] = 'failed'
                    self.alert_status = {'last_attempt_epoch': self.last_alert,
                                         'last_result': result['alerts']['last_result']}
                    result['alerts'].update(self.alert_status)
        self.cached = result
        return result
