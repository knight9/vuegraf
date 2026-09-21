import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pyemvue.device import VueDeviceChannelUsage
from vuegraf.admin_runtime import Runtime


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
