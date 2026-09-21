"""The legacy switch must avoid requests as well as writes."""
import datetime as dt
import threading
from unittest.mock import MagicMock, patch

from vuegraf.collect import collectUsage, collectHistoryUsage, extractDataPoints
from vuegraf.destination import writeDataPoints


def test_disabled_extract_never_queries_resume():
    extractDataPoints({'legacyEnergyEnabled': False}, {}, None, None, True, [], None)


@patch('vuegraf.collect.collectTelemetry')
def test_disabled_minute_keeps_shared_telemetry_request(telemetry):
    account = {'vue': MagicMock(), 'deviceIdMap': {1: None}}
    config = {'legacyEnergyEnabled': False, 'telemetry': {'enabled': True}, 'influxDb': {}}
    collectUsage(config, account, None, dt.datetime.now(dt.UTC), False, [], None, '1MIN')
    account['vue'].get_device_list_usage.assert_called_once()
    telemetry.assert_called_once()


@patch('vuegraf.collect.collectAggregate')
def test_disabled_aggregate_skips_legacy_bulk(aggregate):
    account = {'vue': MagicMock(), 'deviceIdMap': {1: None}}
    config = {'legacyEnergyEnabled': False, 'telemetry': {'enabled': True}, 'influxDb': {}}
    collectUsage(config, account, None, None, False, [], None, '1H')
    account['vue'].get_device_list_usage.assert_not_called()
    aggregate.assert_called_once()


@patch('vuegraf.collect.collectHistory')
def test_disabled_history_skips_legacy_requests(history):
    account = {'vue': MagicMock()}
    collectHistoryUsage({'legacyEnergyEnabled': False}, account, None, None, [], threading.Event())
    history.assert_called_once()
    account['vue'].get_device_list_usage.assert_not_called()


@patch('vuegraf.destination.influx.writeInfluxPoints')
def test_disabled_legacy_write_guard(write):
    writeDataPoints({'legacyEnergyEnabled': False}, [object()])
    write.assert_not_called()
