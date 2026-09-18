import datetime
import json
from unittest.mock import MagicMock, patch

import pytest
from pyemvue.device import VueDevice, VueDeviceChannelUsage, VueUsageDevice
from pyemvue.enums import Scale
from requests import HTTPError, Response

from vuegraf import influx, mqtt, telemetry, victoriametrics
from vuegraf.collect import Point, collectUsage
from vuegraf.destination import pointsMissingFrom, validateDestination


NOW = datetime.datetime(2026, 9, 17, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def account():
    device = VueDevice(gid=42)
    device.device_name = 'Panel'
    return {'name': 'Home', 'vue': MagicMock(), 'deviceIdMap': {42: device}}


def channel(number='Mains_A', value=0.1):
    return VueDeviceChannelUsage(gid=42, channelNum=number, usage=value)


def usage(*channels):
    device = VueUsageDevice(gid=42)
    device.channels = {item.channel_num: item for item in channels}
    return {42: device}


def config(version=2):
    return {'telemetry': {'enabled': True}, 'influxDb': {'version': version}, 'addStationField': False}


@pytest.mark.parametrize('metric,value,seconds,field,expected', [
    ('energy', 0.001, 1, 'power_watts', 3600),
    ('energy', 0.001, 60, 'power_watts', 60),
    ('current', 0.001, 1, 'current_amps', 3.6),
    ('current', 0.1, 60, 'current_amps', 6),
    ('voltage', 117.2, 1, 'voltage_volts', 117.2),
    ('voltage', 117.2, 60, 'voltage_volts', 117.2),
    ('energy', -0.001, 60, 'power_watts', -60),
    ('energy', 0, 60, 'power_watts', 0),
])
def test_conversion(account, metric, value, seconds, field, expected):
    points = []
    telemetry.appendSample(account, channel(), metric, value, NOW, seconds, 'False', points)
    assert next(point.value for point in points if point.metric == field) == pytest.approx(expected)
    assert points[0].value == value


@pytest.mark.parametrize('value', [None, 'No CT', True, float('nan'), float('inf')])
def test_unavailable_is_not_zero(account, value):
    points = []
    assert not telemetry.appendSample(account, channel(), 'voltage', value, NOW, 60, 'False', points)
    assert not points


def test_discovery_union_and_mains_power(account):
    power = usage(channel('1,2,3', 0.002), channel('1', 0.001))
    account['vue'].get_device_list_usage.side_effect = [usage(channel('Mains_A', 0.1)), usage(channel('Mains_A', 120))]
    account['vue'].get_chart_usage.return_value = ([0.001, None], NOW - datetime.timedelta(minutes=1))
    points = []
    telemetry.collectTelemetry(config(), account, NOW, False, points, None, power)
    assert any(point.channelNum == 'Mains_A' and point.metric == 'power_watts' and point.value == 60 for point in points)
    assert any(point.channelNum == 'Mains_A' and point.metric == 'current_amps' and point.value == 6 for point in points)
    assert account['vue'].get_device_list_usage.call_count == 2
    assert all(call.kwargs['max_retry_attempts'] == 1 for call in account['vue'].get_device_list_usage.call_args_list)


def test_second_history_sparse_and_bounded(account):
    account['vue'].get_device_list_usage.return_value = usage(channel())
    start = NOW - datetime.timedelta(hours=3)
    account['vue'].get_chart_usage.return_value = ([None, 0, 0.001], start)
    points = []
    telemetry.collectTelemetry(config(), account, NOW, True, points, NOW - datetime.timedelta(days=2))
    assert all(call.args[1] == start for call in account['vue'].get_chart_usage.call_args_list)
    detailed = [point for point in points if point.detailed == 'True']
    assert {point.timestamp for point in detailed} == {start + datetime.timedelta(seconds=1), start + datetime.timedelta(seconds=2)}
    assert any(point.metric == 'power_watts' and point.value == 3600 for point in detailed)


def test_disabled_and_invalid_config(account):
    telemetry.collectTelemetry({}, account, NOW, True, [], None)
    account['vue'].get_device_list_usage.assert_not_called()
    with pytest.raises(ValueError):
        telemetry.validateConfig({'telemetry': {'enabled': True, 'metrics': ['bogus']}})
    with pytest.raises(ValueError):
        telemetry.validateConfig({'telemetry': []})
    assert telemetry.selectedMetrics({'telemetry': {'metrics': ['all']}}) == list(telemetry.METRICS)
    assert telemetry.selectedMetrics({'telemetry': {'metrics': ['energy', 'energy']}}) == ['energy']


def test_nested_deduplication():
    root = channel('1')
    root.nested_devices = usage(channel('Mains_A'))
    assert set(telemetry.unpackDevices(usage(root))) == {(42, '1'), (42, 'Mains_A')}


@pytest.mark.parametrize('status', [400, 404, 422, None])
def test_unavailable_chart_is_cached(account, status):
    if status:
        response = Response()
        response.status_code = status
        account['vue'].get_chart_usage.side_effect = HTTPError(response=response)
    else:
        account['vue'].get_chart_usage.return_value = ([None], NOW)
    for _ in range(2):
        telemetry.fetchChart(config(), account, channel(), 'voltage', NOW, NOW + datetime.timedelta(minutes=1),
                             Scale.MINUTE.value, 'False', [])
    assert account['vue'].get_chart_usage.call_count == 1


def test_rate_limit_stops_cycle_without_secret_logging(account):
    response = Response()
    response.status_code = 429
    account['vue'].get_device_list_usage.side_effect = HTTPError('private-token', response=response)
    with patch.object(telemetry.logger, 'warning') as warning:
        telemetry.collectTelemetry(config(), account, NOW, True, [], NOW)
    assert warning.call_args.args[1:] == ('HTTPError', 429)
    assert 'private-token' not in str(warning.call_args)
    assert account['vue'].get_device_list_usage.call_count == 1


def test_writers_keep_metrics_and_identity_separate(account):
    points = []
    telemetry.appendSample(account, channel(), 'current', 0.1, NOW, 60, 'False', points)
    current = points[-1]
    record = influx.createDataPoint(config(1), current)
    assert record['measurement'] == 'electrical_telemetry'
    assert record['fields']['current_amps'] == 6
    assert record['tags']['channel_num'] == 'Mains_A'
    assert 'device_name' not in record['tags']
    assert 'current_amps=6' in influx.createDataPoint(config(), current).to_line_protocol()
    cfg = {**config(), 'victoriaMetrics': {'url': 'http://example.test'}}
    assert victoriametrics.createDataPoint(cfg, current)['metric']['__name__'] == 'electrical_telemetry_current_amps'
    cfg['_resumeState'] = {('influxDb', 'Panel', current.chanName, 'False'): (NOW + datetime.timedelta(days=1), True)}
    assert pointsMissingFrom(cfg, 'influxDb', points) == points
    validateDestination(cfg)


def test_mqtt_preserves_legacy_and_each_metric(account):
    points = [Point('Home', 'Panel', 'Panel-Mains_A', 123, NOW, 'False')]
    telemetry.appendSample(account, channel(), 'current', 0.1, NOW, 60, 'False', points)
    client = MagicMock()
    cfg = {**config(), 'mqtt': {'client': client, 'topic': 'vuegraf/energy_usage'}}
    mqtt.publishMqttMessagesIfConnected(cfg, points)
    messages = [json.loads(call.args[1]) for call in client.publish.call_args_list]
    assert len(messages) == 3
    assert messages[0]['usage_watts'] == 123
    assert {message['metric'] for message in messages[1:]} == {'charge_ah', 'current_amps'}


def test_collection_hook_only_runs_on_minute(account):
    cfg = config()
    account['vue'].get_device_list_usage.return_value = {}
    with patch('vuegraf.collect.collectTelemetry') as collect:
        collectUsage(cfg, account, None, NOW, False, [], None, Scale.MINUTE.value)
        collect.assert_called_once()
        collectUsage(cfg, account, NOW, NOW, False, [], None, Scale.HOUR.value)
        assert collect.call_count == 1


def test_detail_batches_share_exact_boundary(account):
    account['vue'].get_device_list_usage.return_value = usage(channel('Balance'), channel())
    account['vue'].get_chart_usage.return_value = ([], None)
    firstStop = NOW + datetime.timedelta(seconds=23)
    telemetry.collectTelemetry(config(), account, firstStop, True, [], NOW)
    assert account['_telemetrySecondStop'] == firstStop
    account['vue'].get_chart_usage.reset_mock()
    # Unsupported combinations would normally be skipped for an hour. Simulate
    # expiry so the next cycle's range can be checked directly.
    account['_telemetryUnavailable'].clear()
    telemetry.collectTelemetry(config(), account, firstStop + datetime.timedelta(hours=1), True, [],
                               firstStop + datetime.timedelta(seconds=1))
    for call in account['vue'].get_chart_usage.call_args_list:
        assert call.args[0].channel_num == 'Mains_A'
        assert call.args[1] == firstStop


def test_chart_auth_error_is_not_cached(account):
    response = Response()
    response.status_code = 401
    account['vue'].get_chart_usage.side_effect = HTTPError(response=response)
    with pytest.raises(HTTPError):
        telemetry.fetchChart(config(), account, channel(), 'energy', NOW, NOW + datetime.timedelta(minutes=1),
                             Scale.MINUTE.value, 'False', [])
    assert not account['_telemetryUnavailable']
