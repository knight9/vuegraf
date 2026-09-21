import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pyemvue.device import VueDeviceChannelUsage
from vuegraf.admin_runtime import Runtime
from vuegraf import telemetry_recovery


def runtime():
    channel = VueDeviceChannelUsage(gid=42, channelNum='1', usage=.001)
    account = {'name': 'test', 'vue': MagicMock(), 'deviceIdMap': {42: SimpleNamespace(device_name='Panel')}}
    config = {'timezone': 'UTC', 'accounts': [account], 'telemetry': {'enabled': True, 'metrics': ['energy']},
              'legacyEnergyEnabled': False, 'influxDb': {'version': 2, 'bucket': 'test'}}
    value = Runtime(config)
    value.channels = [(account, channel)]
    return value, account, channel


@patch('vuegraf.admin_runtime.initConnection')
@patch('vuegraf.admin_runtime.initDeviceAccount')
@patch('vuegraf.admin_runtime.discoverChannels')
@patch('vuegraf.admin_runtime.Storage.refresh', return_value={})
def test_initialization_uses_device_id_not_channel_object(storage, discover, account_init, connection):
    value, account, channel = runtime()
    value.channels = []
    discover.return_value = [channel]
    value.initialize()
    assert value.controller.snapshot()['circuits'][0]['device_name'] == 'Panel'
    account['vue'].get_devices.assert_not_called()


@patch('vuegraf.admin_runtime.writeDataPoints')
@patch('vuegraf.admin_runtime.fetchChart')
def test_sparse_seconds_are_partial_not_success(fetch, write):
    value, _, _ = runtime()
    result = value.execute({'kind': 'second', 'source': 'manual', 'parameters': {'lookback_seconds': 60}})
    assert result['result'] == 'partial'
    assert result['unavailable_requests'] == 1
    assert value.last_second_stop is None


@patch('vuegraf.admin_runtime.writeDataPoints', side_effect=RuntimeError('write failed'))
@patch('vuegraf.admin_runtime.fetchChart')
def test_failed_write_does_not_advance_boundary(fetch, write):
    import pytest
    value, _, _ = runtime()
    fetch.side_effect = lambda *args, **kwargs: args[8].append(SimpleNamespace(timestamp=dt.datetime.now(dt.UTC)))
    with pytest.raises(RuntimeError):
        value.execute({'kind': 'second', 'source': 'manual', 'parameters': {'lookback_seconds': 60}})
    assert value.last_second_stop is None


@patch('vuegraf.admin_runtime.writeDataPoints')
@patch('vuegraf.admin_runtime.fetchChart')
def test_manual_range_does_not_advance_full_schedule_or_activate_account_recovery(fetch, write):
    value, _, _ = runtime()
    previous = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    value.last_second_stop = previous
    value.repair = MagicMock()

    def complete(*args, **kwargs):
        start, stop, points = args[4], args[5], args[8]
        points.extend(SimpleNamespace(timestamp=start + dt.timedelta(seconds=i))
                      for i in range(int((stop - start).total_seconds())))

    fetch.side_effect = complete
    result = value.execute({'kind': 'second', 'source': 'manual', 'parameters': {'lookback_seconds': 60}})
    assert result['result'] == 'success'
    assert value.last_second_stop == previous
    value.repair.assert_not_called()


@patch('vuegraf.admin_runtime.writeDataPoints')
@patch('vuegraf.admin_runtime.collectUsage')
def test_manual_minute_uses_native_loop_and_recovery(collect, write):
    value, _, _ = runtime()
    value.repair = MagicMock()
    value.storage.refresh = MagicMock(return_value={'status': 'ok'})
    value.next_coverage = float('inf')

    result = value.execute({'kind': 'minute', 'source': 'manual', 'parameters': {}})

    assert result['result'] == 'success'
    assert collect.call_args.args[2] is None
    assert collect.call_args.args[7] == '1MIN'
    value.repair.assert_called_once()
    value.storage.refresh.assert_called_once()
    write.assert_called_once()


@patch('vuegraf.admin_runtime.writeDataPoints')
@patch('vuegraf.admin_runtime.collectUsage')
def test_manual_hour_uses_last_completed_hour(collect, write):
    value, _, _ = runtime()

    result = value.execute({'kind': 'hour', 'source': 'manual', 'parameters': {}})

    start, stop, scale = collect.call_args.args[2], collect.call_args.args[3], collect.call_args.args[7]
    assert result['result'] == 'success'
    assert scale == '1H'
    assert start == stop
    assert start.minute == start.second == start.microsecond == 0
    assert dt.timedelta(minutes=59) < dt.datetime.now(dt.UTC) - start < dt.timedelta(hours=2)
    write.assert_called_once()


@patch('vuegraf.admin_runtime.writeDataPoints')
@patch('vuegraf.admin_runtime.collectUsage')
def test_manual_day_uses_previous_local_day(collect, write):
    value, _, _ = runtime()
    expected = (value.previous_day - dt.timedelta(days=1)).astimezone(dt.UTC)

    result = value.execute({'kind': 'day', 'source': 'manual', 'parameters': {}})

    start, stop, scale = collect.call_args.args[2], collect.call_args.args[3], collect.call_args.args[7]
    assert result['result'] == 'success'
    assert scale == '1D'
    assert start == stop
    assert start == expected
    assert (start.hour, start.minute, start.second, start.microsecond) == (23, 59, 59, 0)
    write.assert_called_once()


@patch('vuegraf.admin_runtime.recover')
def test_repair_status_separates_permanently_unavailable_intervals(recover, tmp_path):
    value, account, _ = runtime()
    value.config['args'] = SimpleNamespace(dryrun=False, resetdatabase=False)
    value.config['telemetry']['recovery'] = {
        'enabled': True, 'statePath': str(tmp_path / 'coverage.sqlite3'),
        'initialLookbackSecs': 60}
    telemetry_recovery.initialize(value.config)
    store = value.config['_telemetryRecovery']
    now = dt.datetime(2026, 9, 20, 12, tzinfo=dt.UTC)
    key = store.key(account['name'], 42, '1', 'energy', '1MIN')
    start = int((now - dt.timedelta(minutes=2)).timestamp())
    stop = int((now - dt.timedelta(minutes=1)).timestamp())
    with store.db:
        store.db.execute('INSERT INTO streams(key, start) VALUES (?, ?)', (key, start))
    store.mark_unavailable(key, start, stop, int(now.timestamp()), 5)

    value.repair(now, False)

    status = value.controller.snapshot()
    assert status['recovery']['permanently_unavailable_intervals'] == 1
    assert status['recovery']['permanently_unavailable_stream_seconds'] == 60
    assert status['jobs']['recovery']['last_result'] == 'partial'
