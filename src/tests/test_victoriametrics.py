# Copyright (c) Jason Ertel (jertel).
# This file is part of the Vuegraf project and is made available under the MIT License.

import copy
import datetime
import json
from unittest.mock import MagicMock, patch

# Local imports
from vuegraf import victoriametrics
from vuegraf.collect import Point
from vuegraf.time import getTimeNow

SAMPLE_CONFIG_VM = {
    'victoriaMetrics': {
        'url': 'http://localhost:8428',
        'ssl_verify': True,
        'tagName': 'resolution',
        'tagValue_second': '1s',
        'tagValue_minute': '1m',
        'tagValue_hour': '1h',
        'tagValue_day': '1d'
    },
    'addStationField': False,
    'detailedIntervalSecs': 3600,
    'args': MagicMock(debug=False, dryrun=False, resetdatabase=False)
}


def test_get_tags_defaults():
    """Test getTags falls back to the same defaults as the InfluxDB destination."""
    assert victoriametrics.getTags({'victoriaMetrics': {}}) == ('detailed', 'True', 'False', 'Hour', 'Day')


def test_get_tags_overrides():
    """Test getTags honours overrides from the victoriaMetrics section."""
    section = {'tagName': 'resolution', 'tagValue_second': '1s', 'tagValue_minute': '1m',
               'tagValue_hour': '1h', 'tagValue_day': '1d'}
    assert victoriametrics.getTags({'victoriaMetrics': section}) == ('resolution', '1s', '1m', '1h', '1d')


def test_get_naming_defaults():
    """Test getNaming defaults mirror the InfluxDB measurement name."""
    metricName, extraLabels = victoriametrics.getNaming({'victoriaMetrics': {}})
    assert metricName == 'energy_usage'
    assert extraLabels == {}


def test_get_naming_overrides():
    """Test getNaming honours overrides from the influxDb config block."""
    metricName, extraLabels = victoriametrics.getNaming(
        {'victoriaMetrics': {'metricName': 'energy_usage_usage', 'extraLabels': {'db': 'vuegraf'}}})
    assert metricName == 'energy_usage_usage'
    assert extraLabels == {'db': 'vuegraf'}


def test_create_data_point():
    """Test creating a data point for VictoriaMetrics."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    timestamp = getTimeNow(datetime.UTC)
    point = victoriametrics.createDataPoint(
        config, Point('account', 'device', 'channel', 100.5, timestamp, '1m')
    )
    assert point['metric']['__name__'] == 'energy_usage'
    assert point['metric']['account_name'] == 'account'
    assert point['metric']['device_name'] == 'channel'
    assert point['metric']['resolution'] == '1m'
    assert 'station_name' not in point['metric']
    # No static labels are emitted unless extraLabels is configured.
    assert set(point['metric']) == {'__name__', 'account_name', 'device_name', 'resolution'}
    assert point['values'] == [100.5]
    # VictoriaMetrics JSON import expects millisecond timestamps.
    assert point['timestamps'] == [int(timestamp.timestamp() * 1000)]


def test_create_data_point_with_station():
    """Test creating a data point for VictoriaMetrics with station field."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    config['addStationField'] = True
    timestamp = getTimeNow(datetime.UTC)
    point = victoriametrics.createDataPoint(
        config, Point('account', 'device', 'channel', 100.5, timestamp, '1m')
    )
    assert point['metric']['station_name'] == 'device'


def test_create_data_point_custom_naming():
    """Test the VictoriaMetrics metric name and static labels are overridable."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    config['victoriaMetrics'].update({
        'metricName': 'energy_usage_usage',
        'extraLabels': {'db': 'vuegraf', 'site': 'home'},
    })
    timestamp = getTimeNow(datetime.UTC)
    point = victoriametrics.createDataPoint(config, Point('account', 'device', 'channel', 1, timestamp, '1h'))
    assert point['metric']['__name__'] == 'energy_usage_usage'
    assert point['metric']['db'] == 'vuegraf'
    assert point['metric']['site'] == 'home'
    assert point['metric']['account_name'] == 'account'


def test_create_data_point_extra_labels_cannot_clobber():
    """Test an extraLabels entry cannot displace a structural label or the metric name."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    config['victoriaMetrics']['extraLabels'] = {'__name__': 'hijacked', 'device_name': 'hijacked', 'resolution': 'hijacked'}
    timestamp = getTimeNow(datetime.UTC)
    point = victoriametrics.createDataPoint(config, Point('account', 'device', 'channel', 1, timestamp, '1m'))
    assert point['metric']['__name__'] == 'energy_usage'
    assert point['metric']['device_name'] == 'channel'
    assert point['metric']['resolution'] == '1m'


def test_get_last_timestamp_no_data_minute():
    """Test getLastDBTimeStamp for VictoriaMetrics when no minute data exists."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {'status': 'success', 'data': {'resultType': 'vector', 'result': []}}
    mock_session.get.return_value = mock_response
    config['victoriaMetricsSession'] = mock_session

    now = getTimeNow(datetime.UTC)
    start_time_initial = now - datetime.timedelta(hours=1)
    stop_time_initial = now
    fill_in_missing_data_initial = False

    start_time, stop_time, fill_in_missing_data = victoriametrics.getLastTimeStamp(
        config, 'device', 'channel', '1m', start_time_initial, stop_time_initial, fill_in_missing_data_initial
    )

    # Expect backfill for 7 days, batched to 12 hours
    expected_start_time = start_time_initial - datetime.timedelta(days=7)
    expected_stop_time = expected_start_time + datetime.timedelta(hours=12)
    assert start_time == expected_start_time
    assert stop_time == expected_stop_time
    assert fill_in_missing_data is True
    # No data in any window, so the lookback widens all the way out.
    assert mock_session.get.call_count == len(victoriametrics.LOOKBACK_WINDOWS)
    windows = [c[1]['params']['query'].rsplit('[', 1)[1] for c in mock_session.get.call_args_list]
    assert windows == ['10m])', '6h])', '3w])']
    args, kwargs = mock_session.get.call_args
    assert args[0] == 'http://localhost:8428/api/v1/query'
    assert kwargs['timeout'] == 60.0
    query_str = kwargs['params']['query']
    # tlast_over_time, not timestamp(last_over_time(..)); the latter returns the query
    # evaluation time rather than the last sample's timestamp, silently disabling backfill.
    assert query_str.startswith('tlast_over_time(energy_usage{')
    assert query_str.endswith('}[3w])')
    assert 'device_name="channel"' in query_str
    assert 'resolution="1m"' in query_str
    assert 'station_name=' not in query_str
    # Every attempt is status-checked, so a failing VictoriaMetrics surfaces rather than
    # being mistaken for "no data" and triggering a full rewind.
    assert mock_response.raise_for_status.call_count == len(victoriametrics.LOOKBACK_WINDOWS)


def test_get_last_timestamp_stops_at_first_matching_window():
    """Test the lookback stops widening as soon as a window returns a sample.

    Steady state must cost a single narrow query; widening on every cycle would rescan
    weeks of per-second data per channel.
    """
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    mock_session = MagicMock()
    now = getTimeNow(datetime.UTC)
    last_record_time = (now - datetime.timedelta(minutes=5)).replace(microsecond=0)
    epochSeconds = last_record_time.timestamp()
    mock_response = MagicMock()
    mock_response.json.return_value = {
        'status': 'success',
        'data': {'resultType': 'vector', 'result': [{'metric': {}, 'value': [epochSeconds, str(epochSeconds)]}]}
    }
    mock_session.get.return_value = mock_response
    config['victoriaMetricsSession'] = mock_session

    victoriametrics.getLastTimeStamp(config, 'device', 'channel', '1m', now, now, False)

    mock_session.get.assert_called_once()
    assert mock_session.get.call_args[1]['params']['query'].endswith('[10m])')


def test_get_last_timestamp_escapes_label_values():
    """Test getLastDBTimeStamp escapes quotes and backslashes in VictoriaMetrics label values."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    config['addStationField'] = True
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {'status': 'success', 'data': {'resultType': 'vector', 'result': []}}
    mock_session.get.return_value = mock_response
    config['victoriaMetricsSession'] = mock_session

    now = getTimeNow(datetime.UTC)
    victoriametrics.getLastTimeStamp(config, 'sta"tion', 'chan\\nel', '1m', now, now, False)

    query_str = mock_session.get.call_args[1]['params']['query']
    assert 'device_name="chan\\\\nel"' in query_str
    assert 'station_name="sta\\"tion"' in query_str


def test_get_last_timestamp_no_data_second():
    """Test getLastDBTimeStamp for VictoriaMetrics when no second data exists."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {'status': 'success', 'data': {'resultType': 'vector', 'result': []}}
    mock_session.get.return_value = mock_response
    config['victoriaMetricsSession'] = mock_session

    now = getTimeNow(datetime.UTC)
    start_time_initial = now - datetime.timedelta(minutes=5)
    stop_time_initial = now
    fill_in_missing_data_initial = False

    start_time, stop_time, fill_in_missing_data = victoriametrics.getLastTimeStamp(
        config, 'device', 'channel', '1s', start_time_initial, stop_time_initial, fill_in_missing_data_initial
    )

    # Expect backfill for 3 hours, batched to 1 hour
    expected_start_time = start_time_initial - datetime.timedelta(hours=3)
    expected_stop_time = expected_start_time + datetime.timedelta(hours=1)
    assert start_time == expected_start_time
    assert stop_time == expected_stop_time
    assert fill_in_missing_data is True
    query_str = mock_session.get.call_args[1]['params']['query']
    assert 'resolution="1s"' in query_str


def test_get_last_timestamp_recent_data_minute():
    """Test getLastDBTimeStamp for VictoriaMetrics with recent minute data."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    mock_session = MagicMock()
    now = getTimeNow(datetime.UTC)
    # Zero out microseconds so the epoch-seconds round-trip through the VM API's
    # string-encoded float value is exact.
    last_record_time = (now - datetime.timedelta(minutes=5)).replace(microsecond=0)
    epochSeconds = last_record_time.timestamp()
    mock_response = MagicMock()
    mock_response.json.return_value = {
        'status': 'success',
        'data': {'resultType': 'vector', 'result': [{'metric': {}, 'value': [epochSeconds, str(epochSeconds)]}]}
    }
    mock_session.get.return_value = mock_response
    config['victoriaMetricsSession'] = mock_session

    start_time_initial = now - datetime.timedelta(minutes=2)
    stop_time_initial = now
    fill_in_missing_data_initial = False

    start_time, stop_time, fill_in_missing_data = victoriametrics.getLastTimeStamp(
        config, 'device', 'channel', '1m', start_time_initial, stop_time_initial, fill_in_missing_data_initial
    )

    # Expect start time to be 1 minute after the last record, stop time unchanged
    expected_start_time = (last_record_time.replace(microsecond=0) + datetime.timedelta(minutes=1))
    assert start_time == expected_start_time
    assert stop_time == stop_time_initial
    assert fill_in_missing_data is True  # Because db time < stopTime - 2 mins


def test_get_last_timestamp_with_station():
    """Test getLastDBTimeStamp for VictoriaMetrics with add_station_field enabled."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    config['addStationField'] = True
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {'status': 'success', 'data': {'resultType': 'vector', 'result': []}}
    mock_session.get.return_value = mock_response
    config['victoriaMetricsSession'] = mock_session

    now = getTimeNow(datetime.UTC)
    start_time_initial = now - datetime.timedelta(hours=1)
    stop_time_initial = now
    fill_in_missing_data_initial = False

    victoriametrics.getLastTimeStamp(
        config, 'device123', 'channel', '1m', start_time_initial, stop_time_initial, fill_in_missing_data_initial
    )

    query_str = mock_session.get.call_args[1]['params']['query']
    assert 'station_name="device123"' in query_str


def test_get_last_timestamp_unsupported_pointtype():
    """Test getLastDBTimeStamp for VictoriaMetrics with an unsupported pointType."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {'status': 'success', 'data': {'resultType': 'vector', 'result': []}}
    mock_session.get.return_value = mock_response
    config['victoriaMetricsSession'] = mock_session

    now = getTimeNow(datetime.UTC)
    start_time_initial = now - datetime.timedelta(hours=1)
    stop_time_initial = now
    fill_in_missing_data_initial = False
    unsupported_point_type = 'invalid_type'

    start_time, stop_time, fill_in_missing_data = victoriametrics.getLastTimeStamp(
        config, 'device', 'channel', unsupported_point_type, start_time_initial, stop_time_initial, fill_in_missing_data_initial
    )

    # Expect no changes as the pointType is not supported for backfill logic
    assert start_time == start_time_initial
    assert stop_time == stop_time_initial
    assert fill_in_missing_data == fill_in_missing_data_initial
    query_str = mock_session.get.call_args[1]['params']['query']
    assert f'resolution="{unsupported_point_type}"' in query_str


def test_get_last_timestamp_scopes_by_extra_labels():
    """Test configured extraLabels also scope the last-timestamp lookup."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    config['victoriaMetrics']['extraLabels'] = {'db': 'vuegraf'}
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {'status': 'success', 'data': {'resultType': 'vector', 'result': []}}
    mock_session.get.return_value = mock_response
    config['victoriaMetricsSession'] = mock_session

    now = getTimeNow(datetime.UTC)
    victoriametrics.getLastTimeStamp(config, 'device', 'channel', '1m', now, now, False)

    assert 'db="vuegraf"' in mock_session.get.call_args[1]['params']['query']


def test_get_last_timestamp_custom_timeout():
    """Test getLastDBTimeStamp for VictoriaMetrics converts the configured ms timeout to seconds."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    config['victoriaMetrics']['timeout'] = 120_000
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {'status': 'success', 'data': {'resultType': 'vector', 'result': []}}
    mock_session.get.return_value = mock_response
    config['victoriaMetricsSession'] = mock_session

    now = getTimeNow(datetime.UTC)
    victoriametrics.getLastTimeStamp(config, 'device', 'channel', '1m', now, now, False)

    assert mock_session.get.call_args[1]['timeout'] == 120.0


@patch('vuegraf.victoriametrics.requests.Session')
def test_init_connection(mock_session_class):
    """Test initInfluxConnection for VictoriaMetrics without authentication."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    mock_session = MagicMock()
    mock_session_class.return_value = mock_session

    victoriametrics.initConnection(config)

    mock_session_class.assert_called_once_with()
    assert mock_session.verify is True
    mock_session.post.assert_not_called()
    assert config['victoriaMetricsSession'] == mock_session


@patch('vuegraf.victoriametrics.requests.Session')
def test_init_connection_defaults_ssl_verify(mock_session_class):
    """Test TLS verification defaults to enabled when ssl_verify is not configured."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    del config['victoriaMetrics']['ssl_verify']
    mock_session = MagicMock()
    mock_session_class.return_value = mock_session

    victoriametrics.initConnection(config)

    assert mock_session.verify is True


@patch('vuegraf.victoriametrics.requests.Session')
def test_init_connection_with_basic_auth(mock_session_class):
    """Test initInfluxConnection for VictoriaMetrics with basic authentication."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    config['victoriaMetrics']['user'] = 'testuser'
    config['victoriaMetrics']['pass'] = 'testpass'
    mock_session = MagicMock()
    mock_session_class.return_value = mock_session

    victoriametrics.initConnection(config)

    assert mock_session.auth == ('testuser', 'testpass')


@patch('vuegraf.victoriametrics.requests.Session')
def test_init_connection_with_token(mock_session_class):
    """Test initInfluxConnection for VictoriaMetrics with bearer token authentication."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    config['victoriaMetrics']['token'] = 'my-vm-token'
    mock_session = MagicMock()
    mock_session.headers = {}
    mock_session_class.return_value = mock_session

    victoriametrics.initConnection(config)

    assert mock_session.headers['Authorization'] == 'Bearer my-vm-token'


@patch('vuegraf.victoriametrics.requests.Session')
def test_init_connection_reset(mock_session_class):
    """Test initInfluxConnection for VictoriaMetrics with resetdatabase flag."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    config['args'] = MagicMock(debug=False, dryrun=False, resetdatabase=True)
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_session.post.return_value = mock_response
    mock_session_class.return_value = mock_session

    victoriametrics.initConnection(config)

    mock_session.post.assert_called_once_with(
        'http://localhost:8428/api/v1/admin/tsdb/delete_series',
        params={'match[]': '{__name__="energy_usage"}'},
        timeout=60.0
    )
    mock_response.raise_for_status.assert_called_once()
    assert config['victoriaMetricsSession'] == mock_session


@patch('vuegraf.victoriametrics.requests.Session')
def test_init_connection_reset_scoped_by_extra_labels(mock_session_class):
    """Test resetdatabase stays scoped to extraLabels, so it cannot delete other sources."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    config['victoriaMetrics']['extraLabels'] = {'db': 'vuegraf'}
    config['args'] = MagicMock(debug=False, dryrun=False, resetdatabase=True)
    mock_session = MagicMock()
    mock_session_class.return_value = mock_session

    victoriametrics.initConnection(config)

    assert mock_session.post.call_args[1]['params']['match[]'] == '{__name__="energy_usage",db="vuegraf"}'


@patch('vuegraf.victoriametrics.dumpPoints')
def test_write_points(mock_dump_points):
    """Test writeInfluxPoints for VictoriaMetrics."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_session.post.return_value = mock_response
    config['victoriaMetricsSession'] = mock_session
    timestamp = getTimeNow(datetime.UTC)
    points = [Point('account', 'device', 'channel', 1, timestamp, '1m')]
    influx_points = [victoriametrics.createDataPoint(config, pt) for pt in points]

    victoriametrics.writePoints(config, points)

    expected_body = '\n'.join(json.dumps(point) for point in influx_points)
    mock_session.post.assert_called_once_with(
        'http://localhost:8428/api/v1/import', data=expected_body, timeout=60.0
    )
    mock_response.raise_for_status.assert_called_once()
    mock_dump_points.assert_not_called()


@patch('vuegraf.victoriametrics.dumpPoints')
def test_write_points_batches(mock_dump_points):
    """Test writeInfluxPoints chunks large VictoriaMetrics writes into batches."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    mock_session = MagicMock()
    mock_session.post.return_value = MagicMock()
    config['victoriaMetricsSession'] = mock_session
    timestamp = getTimeNow(datetime.UTC)
    # One point more than a single batch, to force exactly two requests.
    points = [
        Point('account', 'device', 'channel{}'.format(i), i, timestamp, '1m')
        for i in range(victoriametrics.WRITE_BATCH_SIZE + 1)
    ]

    victoriametrics.writePoints(config, points)

    assert mock_session.post.call_count == 2
    firstBody = mock_session.post.call_args_list[0][1]['data']
    secondBody = mock_session.post.call_args_list[1][1]['data']
    assert len(firstBody.split('\n')) == victoriametrics.WRITE_BATCH_SIZE
    assert len(secondBody.split('\n')) == 1


@patch('vuegraf.victoriametrics.dumpPoints')
def test_write_points_no_points(mock_dump_points):
    """Test writeInfluxPoints issues no VictoriaMetrics request when there are no points."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    mock_session = MagicMock()
    config['victoriaMetricsSession'] = mock_session

    victoriametrics.writePoints(config, [])

    mock_session.post.assert_not_called()


@patch('vuegraf.victoriametrics.dumpPoints')
def test_write_points_dryrun(mock_dump_points):
    """Test writeInfluxPoints for VictoriaMetrics with dryrun enabled."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    config['args'] = MagicMock(debug=False, dryrun=True, resetdatabase=False)
    mock_session = MagicMock()
    config['victoriaMetricsSession'] = mock_session
    points = [Point('account', 'device', 'channel', 1, getTimeNow(datetime.UTC), '1m')]

    victoriametrics.writePoints(config, points)

    mock_session.post.assert_not_called()
    mock_dump_points.assert_not_called()


def test_write_points_debug():
    """Test writeInfluxPoints for VictoriaMetrics with debug enabled."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    config['args'] = MagicMock(debug=True, dryrun=False, resetdatabase=False)
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_session.post.return_value = mock_response
    config['victoriaMetricsSession'] = mock_session
    points = [Point('account', 'device', 'channel', 1, getTimeNow(datetime.UTC), '1m')]

    victoriametrics.writePoints(config, points)

    mock_session.post.assert_called_once()


@patch('vuegraf.victoriametrics.logger')
def test_dump_points(mock_logger):
    """Test dumpPoints for VictoriaMetrics."""
    config = copy.deepcopy(SAMPLE_CONFIG_VM)
    points = [
        {'metric': {'__name__': 'energy_usage'}, 'values': [1], 'timestamps': [123]},
        {'metric': {'__name__': 'energy_usage'}, 'values': [2.0], 'timestamps': [456]}
    ]

    victoriametrics.dumpPoints(config, "Test Label VM", points)

    mock_logger.debug.assert_any_call("Test Label VM")
    mock_logger.debug.assert_any_call('  {}'.format(json.dumps(points[0])))
    mock_logger.debug.assert_any_call('  {}'.format(json.dumps(points[1])))
    assert mock_logger.debug.call_count == 3
