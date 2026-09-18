import datetime
import threading
from unittest.mock import MagicMock, patch

import pytest
from pyemvue.device import VueDevice, VueDeviceChannelUsage, VueUsageDevice
from pyemvue.enums import Scale
from requests import HTTPError, Response

from vuegraf import collect, telemetry, telemetry_history as history


UTC = datetime.UTC
NOW = datetime.datetime(2026, 9, 17, 12, tzinfo=UTC)


def fixture():
    device = VueDevice(gid=42)
    device.device_name = 'Panel'
    channel = VueDeviceChannelUsage(gid=42, channelNum='Mains_A', usage=1)
    usage = VueUsageDevice(gid=42)
    usage.channels = {'Mains_A': channel}
    api = MagicMock()
    api.get_device_list_usage.return_value = {42: usage}
    config = {'telemetry': {'enabled': True}, 'influxDb': {}, 'timezone': 'America/Los_Angeles'}
    account = {'name': 'Home', 'vue': api, 'deviceIdMap': {42: device},
               'channelIdMap': {'42-Mains_A': channel}}
    return config, account, channel


@pytest.mark.parametrize('day,hours', [
    (datetime.datetime(2026, 3, 8, 8, tzinfo=UTC), 23),
    (datetime.datetime(2026, 11, 1, 7, tzinfo=UTC), 25),
])
def test_daily_dst_conversion_and_contiguous_timestamps(day, hours):
    cfg, account, channel = fixture()
    end = day + datetime.timedelta(hours=hours)
    account['vue'].get_chart_usage.return_value = ([hours, hours], day)
    points = []
    telemetry.fetchChart(cfg, account, channel, 'energy', day, end, Scale.DAY.value, 'Day', points, cacheEmpty=False)
    assert [(point.metric, point.value) for point in points] == [('energy_kwh', hours), ('power_watts', 1000)]
    assert points[0].timestamp == day
    assert telemetry.sampleWindow(cfg, day, 1, Scale.DAY.value)[0] == end
    points = []
    telemetry.fetchChart(cfg, account, channel, 'current', day, end, Scale.DAY.value, 'Day', points, cacheEmpty=False)
    assert points[-1].value == 1


def test_retention_batch_sizes_and_complete_buckets():
    cfg, _, _ = fixture()
    windows = list(history.historyWindows(cfg, NOW - datetime.timedelta(days=45), NOW))
    for scale, _, start, stop in windows:
        assert start < stop <= NOW
        if scale == Scale.SECOND.value:
            assert start >= NOW - datetime.timedelta(hours=3)
            assert stop - start <= datetime.timedelta(hours=1)
        if scale == Scale.MINUTE.value:
            assert start >= NOW - datetime.timedelta(days=7)
            assert stop - start <= datetime.timedelta(hours=12)
    for scale in (Scale.SECOND.value, Scale.MINUTE.value, Scale.HOUR.value, Scale.DAY.value):
        intervals = [(start, stop) for kind, _, start, stop in windows if kind == scale]
        assert all(previous[1] == following[0] for previous, following in zip(intervals, intervals[1:]))
    assert list(history.historyWindows(cfg, NOW, NOW)) == []


def test_empty_old_period_does_not_suppress_new_data():
    cfg, account, channel = fixture()
    account['vue'].get_chart_usage.side_effect = [([None], NOW), ([1], NOW + datetime.timedelta(hours=1))]
    points = []
    for index in range(2):
        start = NOW + datetime.timedelta(hours=index)
        telemetry.fetchChart(cfg, account, channel, 'energy', start, start + datetime.timedelta(hours=1),
                             Scale.HOUR.value, 'Hour', points, cacheEmpty=False)
    assert points[-1].value == 1000
    assert account['vue'].get_chart_usage.call_count == 2


def test_history_streams_all_metrics_and_respects_cancellation():
    cfg, account, _ = fixture()
    account['vue'].get_chart_usage.side_effect = lambda channel, start, stop, **kw: ([1], start)
    pause = threading.Event()
    with patch.object(history, 'historyWindows', return_value=[(Scale.HOUR.value, 'Hour', NOW, NOW + datetime.timedelta(hours=1))]), \
            patch('vuegraf.destination.writeDataPoints') as write, patch.object(pause, 'wait', return_value=False):
        history.collectHistory(cfg, account, NOW, NOW + datetime.timedelta(hours=1), pause)
    assert write.call_count == 3
    assert {point.metric for call in write.call_args_list for point in call.args[1]} == {
        'energy_kwh', 'power_watts', 'charge_ah', 'current_amps', 'voltage_volts'}
    assert max(len(call.args[1]) for call in write.call_args_list) == 2
    history.collectHistory({}, account, NOW, NOW, pause)
    pause.set()
    history.collectHistory(cfg, account, NOW, NOW, pause)
    with patch.object(pause, 'is_set', side_effect=[False, True]), patch('vuegraf.destination.writeDataPoints') as write:
        history.collectHistory(cfg, account, NOW - datetime.timedelta(seconds=1), NOW, pause)
        write.assert_not_called()
    pause.clear()
    with patch.object(pause, 'wait', return_value=True), patch('vuegraf.destination.writeDataPoints') as write:
        history.collectHistory(cfg, account, NOW - datetime.timedelta(seconds=1), NOW, pause)
        assert write.call_count == 1


def test_history_empty_and_unsupported_windows_do_not_write():
    cfg, account, _ = fixture()
    response = Response()
    response.status_code = 404
    account['vue'].get_chart_usage.side_effect = [HTTPError(response=response), ([], None), ([], NOW)] * 3
    with patch('vuegraf.destination.writeDataPoints') as write, patch.object(threading.Event, 'wait', return_value=False):
        history.collectHistory(cfg, account, NOW - datetime.timedelta(seconds=1), NOW, threading.Event())
        write.assert_not_called()
    assert account['_telemetryUnavailable'] == {}


def test_aggregate_and_historical_records_are_identical():
    cfg, account, channel = fixture()
    account['vue'].get_chart_usage.return_value = ([1], NOW)
    points = []
    history.collectAggregate(cfg, account, NOW, Scale.HOUR.value, points)
    expected = []
    for metric in telemetry.selectedMetrics(cfg):
        telemetry.fetchChart(cfg, account, channel, metric, NOW, NOW + datetime.timedelta(hours=1),
                             Scale.HOUR.value, 'Hour', expected, cacheEmpty=False)
    assert points == expected
    account['_telemetryChannels'][(42, 'Balance')] = VueDeviceChannelUsage(gid=42, channelNum='Balance')
    # Next aggregate reuses discovery, and excludes synthetic balance.
    account['vue'].get_device_list_usage.reset_mock()
    history.collectAggregate(cfg, account, NOW, Scale.DAY.value, [])
    account['vue'].get_device_list_usage.assert_not_called()
    history.collectAggregate({}, account, NOW, Scale.HOUR.value, [])


def test_history_and_aggregate_are_hooked_into_normal_collection():
    cfg, account, _ = fixture()
    account['vue'].get_device_list_usage.return_value = {}
    with patch('vuegraf.collect.collectAggregate') as aggregate:
        collect.collectUsage(cfg, account, NOW, NOW, False, [], None, Scale.HOUR.value)
        aggregate.assert_called_once()
    with patch('vuegraf.collect.collectAggregate', side_effect=RuntimeError('private details')), \
            patch.object(collect.logger, 'warning') as warning:
        collect.collectUsage(cfg, account, NOW, NOW, False, [], None, Scale.HOUR.value)
        assert warning.call_args.args[1] == 'RuntimeError'
        assert 'private details' not in str(warning.call_args)
    with patch('vuegraf.collect.collectHistory') as backfill, \
            patch('vuegraf.collect.calculateHistoryTimeRange', return_value=(NOW, NOW)):
        collect.collectHistoryUsage(cfg, account, NOW, NOW, [], threading.Event())
        backfill.assert_called_once()
