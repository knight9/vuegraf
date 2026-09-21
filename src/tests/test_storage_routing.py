import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from vuegraf.influx import writeInfluxPoints
from vuegraf.storage import validate_routing, provision, RETENTION, Storage
from vuegraf.telemetry import TelemetryPoint


def config():
    return {'influxDb': {'version': 2, 'bucket': 'legacy', 'org': 'test', 'telemetryBuckets': {
        'second': 'seconds', 'minute': 'minutes', 'hour': 'coarse', 'day': 'coarse'}},
        'args': SimpleNamespace(debug=False, dryrun=False), 'influx': MagicMock()}


def test_routing_validation():
    cfg = config()
    validate_routing(cfg)
    cfg['influxDb']['telemetryBuckets']['second'] = 'coarse'
    with pytest.raises(ValueError):
        validate_routing(cfg)


def test_each_resolution_writes_to_correct_bucket():
    cfg = config()
    points = [TelemetryPoint('test', 'panel', 'circuit', 1, '1', 'power_watts', 10, dt.datetime.now(dt.UTC), tag)
              for tag in ['True', 'False', 'Hour', 'Day']]
    writeInfluxPoints(cfg, points)
    calls = cfg['influx'].write_api.return_value.write.call_args_list
    assert [(c.kwargs['bucket'], len(c.kwargs['record'])) for c in calls] == [('seconds', 1), ('minutes', 1), ('coarse', 2)]


@patch('vuegraf.storage.client_for')
def test_provision_never_shortens_existing_retention(client):
    api = client.return_value.__enter__.return_value.buckets_api.return_value
    api.find_bucket_by_name.return_value = SimpleNamespace(retention_rules=[])
    with pytest.raises(ValueError, match='refusing'):
        provision(config(), apply=True)
    api.create_bucket.assert_not_called()


@patch('vuegraf.storage.client_for')
def test_provision_plan_is_read_only(client):
    api = client.return_value.__enter__.return_value.buckets_api.return_value
    api.find_bucket_by_name.return_value = None
    plan = provision(config())
    assert len(plan) == 3
    assert {r['retention_seconds'] for r in plan} == set(RETENTION.values())
    api.create_bucket.assert_not_called()


@patch('vuegraf.storage.client_for')
@patch('vuegraf.disk_stats.os.walk', side_effect=PermissionError())
@patch('vuegraf.disk_stats.shutil.disk_usage', return_value=SimpleNamespace(total=100, used=50, free=50))
def test_unreadable_mount_is_not_reported_as_zero_usage(usage, walk, client):
    cfg = config()
    cfg['admin'] = {'storagePath': '/test'}
    result = Storage(cfg).refresh()
    assert result['status'] == 'partial'
    assert result['database_scan_complete'] is False
    assert 'database_bytes' not in result


@patch('vuegraf.storage.client_for')
def test_storage_status_is_cached(client):
    storage = Storage(config())
    first = storage.refresh()
    assert storage.refresh() is first
    assert client.call_count == 1


@patch('vuegraf.storage.requests.post')
@patch('vuegraf.storage.client_for')
@patch('vuegraf.disk_stats.os.walk', return_value=[])
@patch('vuegraf.disk_stats.shutil.disk_usage', return_value=SimpleNamespace(total=100, used=90, free=10))
def test_webhook_is_rate_limited_and_secret_url_not_returned(usage, walk, client, post, monkeypatch):
    monkeypatch.setenv('VUEGRAF_ALERT_WEBHOOK', 'https://alerts.test/private-token')
    post.return_value.status_code = 200
    cfg = config()
    cfg['admin'] = {'storagePath': '/test'}
    storage = Storage(cfg)
    first = storage.refresh()
    storage.next_check = 0
    second = storage.refresh()
    assert post.call_count == 1
    assert first['alerts']['last_result'] == second['alerts']['last_result'] == 'sent'
    assert second['alerts']['last_attempt_epoch']
    assert 'private-token' not in str(second)
