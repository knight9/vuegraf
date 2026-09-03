# Copyright (c) Jason Ertel (jertel).
# This file is part of the Vuegraf project and is made available under the MIT License.

import datetime
import pytest
from unittest.mock import patch

# Local imports
from vuegraf import destination
from vuegraf.collect import Point

# getInfluxTag's defaults, which a mocked VictoriaMetrics module must agree with when
# both databases are configured.
DEFAULT_TAGS = ('detailed', 'True', 'False', 'Hour', 'Day')


def influxConfig(version=2):
    return {'influxDb': {'version': version}}


def vmConfig(**section):
    return {'victoriaMetrics': dict({'url': 'http://localhost:8428'}, **section)}


def bothConfig():
    return dict(influxConfig(), **vmConfig())


def withMqtt(config):
    return dict(config, mqtt={'host': 'localhost'})


def point(chanName, timestamp, detailed='False', deviceName='device'):
    return Point('account', deviceName, chanName, 1.0, timestamp, detailed)


def utc(minute):
    return datetime.datetime(2026, 9, 1, 12, minute, 0, tzinfo=datetime.timezone.utc)


# --- Test database detection ---

def test_uses_influx_when_section_present():
    """Test the InfluxDB database is selected by the presence of its config section."""
    assert destination.usesInflux(influxConfig()) is True
    assert destination.usesInflux(vmConfig()) is False


def test_uses_victoria_metrics_when_section_present():
    """Test the VictoriaMetrics database is selected by the presence of its section."""
    assert destination.usesVictoriaMetrics(vmConfig()) is True
    assert destination.usesVictoriaMetrics(influxConfig()) is False


def test_uses_mqtt_when_section_present():
    """Test the MQTT output is selected the same way the databases are."""
    assert destination.usesMqtt(withMqtt(influxConfig())) is True
    assert destination.usesMqtt(influxConfig()) is False


def test_empty_section_does_not_select_a_destination():
    """Test an empty section is treated as absent, matching the MQTT output's behaviour."""
    assert destination.usesInflux({'influxDb': {}}) is False
    assert destination.usesVictoriaMetrics({'victoriaMetrics': {}}) is False
    assert destination.usesMqtt({'mqtt': {}}) is False


# --- Test validateDestination ---

def test_validate_accepts_a_single_database():
    """Test one configured database validates."""
    destination.validateDestination(influxConfig(1))
    destination.validateDestination(influxConfig(2))
    destination.validateDestination(vmConfig())


@patch('vuegraf.destination.victoriametrics')
def test_validate_accepts_both_databases(mock_vm):
    """Test configuring both databases is supported."""
    mock_vm.getTags.return_value = DEFAULT_TAGS
    destination.validateDestination(bothConfig())


def test_validate_rejects_no_database():
    """Test omitting both sections is rejected."""
    with pytest.raises(ValueError, match='No database configured'):
        destination.validateDestination({})


def test_validate_rejects_unsupported_influx_version():
    """Test an unsupported InfluxDB version is rejected.

    Previously a version such as 3 fell through to the v1 code path and failed later with
    a confusing error about a missing 'host' config field.
    """
    with pytest.raises(ValueError, match='Unsupported influxDb version'):
        destination.validateDestination(influxConfig(3))


@patch('vuegraf.destination.victoriametrics')
def test_validate_rejects_mismatched_tag_values_when_both_configured(mock_vm):
    """Test differing tag values are rejected when both databases are configured.

    Collection stamps the resolution value onto every point before either database sees
    it, so a single value has to satisfy both. A mismatch would also break the resume
    lookup, since each database would query for a value it never stored.
    """
    mock_vm.getTags.return_value = ('detailed', '1s', '1m', '1h', '1d')
    with pytest.raises(ValueError, match='settings must match'):
        destination.validateDestination(bothConfig())


@patch('vuegraf.destination.victoriametrics')
def test_validate_allows_a_differing_tag_name(mock_vm):
    """Test the tag name may differ between databases.

    Each reads its own tagName when writing and when querying, so only the values that
    are recorded on the data points have to agree.
    """
    mock_vm.getTags.return_value = ('resolution',) + DEFAULT_TAGS[1:]
    destination.validateDestination(bothConfig())


# --- Test getTags routing ---

@patch('vuegraf.destination.victoriametrics')
def test_get_tags_routes_to_victoria_metrics(mock_vm):
    """Test tag naming comes from the VictoriaMetrics section when it is configured."""
    mock_vm.getTags.return_value = ('resolution', '1s', '1m', '1h', '1d')
    config = vmConfig()
    assert destination.getTags(config) == ('resolution', '1s', '1m', '1h', '1d')


def test_get_tags_routes_to_influx():
    """Test tag naming comes from the influxDb section when it is configured."""
    config = {'influxDb': {'version': 2, 'tagName': 'granularity', 'tagValue_second': 'sec'}}
    tagName, tagValue_second, _, _, _ = destination.getTags(config)
    assert tagName == 'granularity'
    assert tagValue_second == 'sec'


# --- Test getLastDBTimeStamp ---

@patch('vuegraf.destination.influx')
def test_get_last_db_timestamp_uses_influx_only(mock_influx):
    """Test only the configured database is consulted, and its window is recorded."""
    window = (utc(0), utc(30), False)
    mock_influx.getLastDBTimeStamp.return_value = window
    config = influxConfig()
    assert destination.getLastDBTimeStamp(
        config, 'device', 'channel', 'False', utc(0), utc(30), False) == window
    assert destination.getResumeState(config) == {
        ('influxDb', 'device', 'channel', 'False'): (window[0], window[2])}


@patch('vuegraf.destination.victoriametrics')
def test_get_last_db_timestamp_uses_victoria_metrics_only(mock_vm):
    """Test only the configured database is queried."""
    mock_vm.getTags.return_value = DEFAULT_TAGS
    mock_vm.getLastTimeStamp.return_value = (utc(0), utc(30), False)
    config = vmConfig()
    destination.getLastDBTimeStamp(config, 'device', 'channel', 'False', utc(0), utc(30), False)
    mock_vm.getLastTimeStamp.assert_called_once_with(
        config, 'device', 'channel', 'False', utc(0), utc(30), False)


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_get_last_db_timestamp_uses_earliest_of_both(mock_influx, mock_vm):
    """Test both databases are queried and the earliest record drives the fetch window.

    A single Emporia request then covers whichever database is furthest behind.
    """
    mock_vm.getTags.return_value = DEFAULT_TAGS
    influxWindow = (utc(20), utc(30), True)
    vmWindow = (utc(5), utc(30), True)
    mock_influx.getLastDBTimeStamp.return_value = influxWindow
    mock_vm.getLastTimeStamp.return_value = vmWindow
    config = bothConfig()

    result = destination.getLastDBTimeStamp(config, 'device', 'channel', 'False',
                                            utc(30), utc(30), False)

    # The VictoriaMetrics window starts earlier, so it is the one fetched.
    assert result == vmWindow
    state = destination.getResumeState(config)
    # Both are told the series is backfilling, so each can trim its own share.
    assert state[('influxDb', 'device', 'channel', 'False')] == (influxWindow[0], True)
    assert state[('victoriaMetrics', 'device', 'channel', 'False')] == (vmWindow[0], True)


# --- Test pointsMissingFrom ---

def test_points_missing_from_passes_everything_for_a_single_database():
    """Test filtering is a no-op with one database, preserving existing behaviour."""
    config = influxConfig()
    points = [point('channel', utc(1)), point('channel', utc(2))]
    assert destination.pointsMissingFrom(config, destination.INFLUX, points) == points


@patch('vuegraf.destination.victoriametrics')
def test_points_missing_from_filters_per_database(mock_vm):
    """Test each database only receives points newer than its own last record."""
    mock_vm.getTags.return_value = DEFAULT_TAGS
    config = bothConfig()
    destination.getResumeState(config).update({
        ('influxDb', 'device', 'channel', 'False'): (utc(3), True),
        ('victoriaMetrics', 'device', 'channel', 'False'): (utc(1), True),
    })
    points = [point('channel', utc(1)), point('channel', utc(2)), point('channel', utc(3))]

    influxPoints = destination.pointsMissingFrom(config, destination.INFLUX, points)
    vmPoints = destination.pointsMissingFrom(config, destination.VICTORIA_METRICS, points)

    assert [p.timestamp for p in influxPoints] == [utc(3)]
    assert [p.timestamp for p in vmPoints] == [utc(1), utc(2), utc(3)]


@patch('vuegraf.destination.victoriametrics')
def test_points_missing_from_trims_an_up_to_date_database_during_a_backfill(mock_vm):
    """Test a current database is trimmed while another backfills the same series.

    The window was widened for the database that is behind; the current one already holds
    that history and should receive none of it.
    """
    mock_vm.getTags.return_value = DEFAULT_TAGS
    config = bothConfig()
    destination.getResumeState(config).update({
        # InfluxDB is current, so its resume point is the collection instant.
        ('influxDb', 'device', 'channel', 'False'): (utc(30), True),
        ('victoriaMetrics', 'device', 'channel', 'False'): (utc(1), True),
    })
    points = [point('channel', utc(m)) for m in (1, 2, 3)]

    assert destination.pointsMissingFrom(config, destination.INFLUX, points) == []
    assert destination.pointsMissingFrom(config, destination.VICTORIA_METRICS, points) == points


@patch('vuegraf.destination.victoriametrics')
def test_points_missing_from_keeps_points_with_no_recorded_state(mock_vm):
    """Test a series with no recorded last record is sent in full.

    Hourly and daily points never consult the resume state, so they must not be filtered.
    """
    mock_vm.getTags.return_value = DEFAULT_TAGS
    config = bothConfig()
    points = [point('channel', utc(1), detailed='Hour')]
    assert destination.pointsMissingFrom(config, destination.INFLUX, points) == points


@patch('vuegraf.destination.victoriametrics')
def test_points_missing_from_keeps_everything_for_an_up_to_date_database(mock_vm):
    """Test no trimming happens while no database is backfilling this series.

    Collection emits a single current sample timestamped to the minute, earlier than the
    recorded resume point, so trimming would drop it.
    """
    mock_vm.getTags.return_value = DEFAULT_TAGS
    config = bothConfig()
    destination.getResumeState(config)[('influxDb', 'device', 'channel', 'False')] = \
        (utc(30), False)
    points = [point('channel', utc(29))]
    assert destination.pointsMissingFrom(config, destination.INFLUX, points) == points


# --- Test initConnection ---

@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_init_connects_influx_only(mock_influx, mock_vm):
    """Test only the configured database is connected."""
    config = influxConfig()
    destination.initConnection(config)
    mock_influx.initInfluxConnection.assert_called_once_with(config)
    mock_vm.initConnection.assert_not_called()


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_init_connects_victoria_metrics_only(mock_influx, mock_vm):
    """Test only the configured database is connected."""
    mock_vm.getTags.return_value = DEFAULT_TAGS
    config = vmConfig()
    destination.initConnection(config)
    mock_vm.initConnection.assert_called_once_with(config)
    mock_influx.initInfluxConnection.assert_not_called()


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_init_connects_both(mock_influx, mock_vm):
    """Test both databases are connected when both are configured."""
    mock_vm.getTags.return_value = DEFAULT_TAGS
    config = bothConfig()
    destination.initConnection(config)
    mock_influx.initInfluxConnection.assert_called_once_with(config)
    mock_vm.initConnection.assert_called_once_with(config)


# --- Test writeDataPoints ---

@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_write_routes_to_influx(mock_influx, mock_vm):
    """Test writeDataPoints routes to InfluxDB."""
    config = influxConfig()
    points = [point('channel', utc(1))]
    destination.writeDataPoints(config, points)
    mock_influx.writeInfluxPoints.assert_called_once_with(config, points)
    mock_vm.writePoints.assert_not_called()


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_write_routes_to_victoria_metrics(mock_influx, mock_vm):
    """Test writeDataPoints routes to VictoriaMetrics."""
    mock_vm.getTags.return_value = DEFAULT_TAGS
    config = vmConfig()
    points = [point('channel', utc(1))]
    destination.writeDataPoints(config, points)
    mock_vm.writePoints.assert_called_once_with(config, points)
    mock_influx.writeInfluxPoints.assert_not_called()


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_write_fans_out_and_filters(mock_influx, mock_vm):
    """Test both databases are written, each receiving only what it is missing."""
    mock_vm.getTags.return_value = DEFAULT_TAGS
    config = bothConfig()
    destination.getResumeState(config).update({
        ('influxDb', 'device', 'channel', 'False'): (utc(3), True),
        ('victoriaMetrics', 'device', 'channel', 'False'): (utc(1), True),
    })
    points = [point('channel', utc(1)), point('channel', utc(2)), point('channel', utc(3))]

    destination.writeDataPoints(config, points)

    assert [p.timestamp for p in mock_influx.writeInfluxPoints.call_args[0][1]] == [utc(3)]
    assert [p.timestamp for p in mock_vm.writePoints.call_args[0][1]] == [utc(1), utc(2), utc(3)]


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_write_clears_resume_state(mock_influx, mock_vm):
    """Test the resume state is cleared after writing.

    A stale entry from an earlier cycle must not suppress a later point.
    """
    mock_vm.getTags.return_value = DEFAULT_TAGS
    config = bothConfig()
    destination.getResumeState(config)[('influxDb', 'device', 'channel', 'False')] = \
        (utc(3), True)
    destination.writeDataPoints(config, [])
    assert destination.getResumeState(config) == {}


# --- Test startup validation ---

@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_init_rejects_no_database(mock_influx, mock_vm):
    """Test initConnection validates before connecting anything."""
    with pytest.raises(ValueError, match='No database configured'):
        destination.initConnection({})
    mock_influx.initInfluxConnection.assert_not_called()
    mock_vm.initConnection.assert_not_called()


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_init_rejects_unsupported_influx_version(mock_influx, mock_vm):
    """Test a bad version is reported at startup, not on the first write."""
    with pytest.raises(ValueError, match='Unsupported influxDb version'):
        destination.initConnection(influxConfig(3))
    mock_influx.initInfluxConnection.assert_not_called()


def test_init_rejects_an_unusable_victoria_metrics_section():
    """Test the VictoriaMetrics module validates its own section at startup.

    Left unmocked so the real check runs; the router only delegates.
    """
    with pytest.raises(ValueError, match='url entry is required'):
        destination.initConnection({'victoriaMetrics': {'timeout': 1000}})


# --- Test MQTT, which records no resume point ---

@patch('vuegraf.destination.initMqttConnectionIfConfigured')
@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_init_also_connects_mqtt(mock_influx, mock_vm, mock_initMqtt):
    """Test the MQTT output is set up alongside the databases."""
    config = withMqtt(influxConfig())
    destination.initConnection(config)
    mock_initMqtt.assert_called_once_with(config)


@patch('vuegraf.destination.publishMqttMessagesIfConnected')
@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_write_publishes_every_point_to_mqtt(mock_influx, mock_vm, mock_publish):
    """Test MQTT receives the full batch, not a per-database trimmed share.

    It keeps no resume point, so there is nothing to trim against; it does its own
    filtering when publishing.
    """
    mock_vm.getTags.return_value = DEFAULT_TAGS
    config = withMqtt(bothConfig())
    destination.getResumeState(config).update({
        ('influxDb', 'device', 'channel', 'False'): (utc(3), True),
        ('victoriaMetrics', 'device', 'channel', 'False'): (utc(1), True),
    })
    points = [point('channel', utc(1)), point('channel', utc(2)), point('channel', utc(3))]

    destination.writeDataPoints(config, points)

    # InfluxDB is trimmed to what it is missing, while MQTT is offered everything.
    assert mock_influx.writeInfluxPoints.call_args[0][1] == [points[2]]
    mock_publish.assert_called_once_with(config, points)


@patch('vuegraf.destination.stopMqttIfConnected')
def test_close_disconnects_mqtt(mock_stopMqtt):
    """Test shutdown is routed here too, so the caller has a single entry point."""
    config = withMqtt(influxConfig())
    destination.closeConnection(config)
    mock_stopMqtt.assert_called_once_with(config)


@patch('vuegraf.destination.stopMqttIfConnected')
@patch('vuegraf.destination.publishMqttMessagesIfConnected')
@patch('vuegraf.destination.initMqttConnectionIfConfigured')
@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_mqtt_is_skipped_when_not_configured(mock_influx, mock_vm, mock_initMqtt, mock_publish, mock_stopMqtt):
    """Test an unconfigured MQTT output is not called at all, as for the databases."""
    config = influxConfig()
    destination.initConnection(config)
    destination.writeDataPoints(config, [])
    destination.closeConnection(config)
    mock_initMqtt.assert_not_called()
    mock_publish.assert_not_called()
    mock_stopMqtt.assert_not_called()
