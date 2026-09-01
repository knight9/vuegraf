# Copyright (c) Jason Ertel (jertel).
# This file is part of the Vuegraf project and is made available under the MIT License.

import pytest
from unittest.mock import MagicMock, patch

# Local imports
from vuegraf import destination


def influxConfig(version=2):
    return {'influxDb': {'version': version}}


def vmConfig(**section):
    return {'victoriaMetrics': dict({'url': 'http://localhost:8428'}, **section)}


# --- Test destination detection ---

def test_uses_influx_when_section_present():
    """Test the InfluxDB destination is selected by the presence of its config section."""
    assert destination.usesInflux(influxConfig()) is True
    assert destination.usesInflux(vmConfig()) is False


def test_uses_victoria_metrics_when_section_present():
    """Test the VictoriaMetrics destination is selected by the presence of its section."""
    assert destination.usesVictoriaMetrics(vmConfig()) is True
    assert destination.usesVictoriaMetrics(influxConfig()) is False


def test_empty_section_does_not_select_a_destination():
    """Test an empty section is treated as absent, matching the MQTT output's behaviour."""
    assert destination.usesInflux({'influxDb': {}}) is False
    assert destination.usesVictoriaMetrics({'victoriaMetrics': {}}) is False


# --- Test validateDestination ---

def test_validate_accepts_a_single_destination():
    """Test exactly one configured destination validates."""
    destination.validateDestination(influxConfig(1))
    destination.validateDestination(influxConfig(2))
    destination.validateDestination(vmConfig())


def test_validate_rejects_both_destinations():
    """Test configuring both sections is rejected rather than silently preferring one."""
    config = dict(influxConfig(), **vmConfig())
    with pytest.raises(ValueError, match='only one of the two'):
        destination.validateDestination(config)


def test_validate_rejects_no_destination():
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


# --- Test getTags routing ---

@patch('vuegraf.destination.victoriametrics')
def test_get_tags_routes_to_victoria_metrics(mock_vm):
    """Test tag naming comes from the VictoriaMetrics section when it is configured."""
    mock_vm.getTags.return_value = ('resolution', '1s', '1m', '1h', '1d')
    config = vmConfig()
    assert destination.getTags(config) == ('resolution', '1s', '1m', '1h', '1d')
    mock_vm.getTags.assert_called_once_with(config)


def test_get_tags_routes_to_influx():
    """Test tag naming comes from the influxDb section when it is configured."""
    config = {'influxDb': {'version': 2, 'tagName': 'granularity', 'tagValue_second': 'sec'}}
    tagName, tagValue_second, _, _, _ = destination.getTags(config)
    assert tagName == 'granularity'
    assert tagValue_second == 'sec'


def test_get_tags_rejects_no_destination():
    """Test tag lookup fails when no destination is configured."""
    with pytest.raises(ValueError, match='No database configured'):
        destination.getTags({})


# --- Test operation routing ---

@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_init_routes_to_influx(mock_influx, mock_vm):
    """Test initConnection routes to InfluxDB."""
    config = influxConfig()
    destination.initConnection(config)
    mock_influx.initInfluxConnection.assert_called_once_with(config)
    mock_vm.initConnection.assert_not_called()


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_init_routes_to_victoria_metrics(mock_influx, mock_vm):
    """Test initConnection routes to VictoriaMetrics."""
    config = vmConfig()
    destination.initConnection(config)
    mock_vm.initConnection.assert_called_once_with(config)
    mock_influx.initInfluxConnection.assert_not_called()


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_write_routes_to_influx(mock_influx, mock_vm):
    """Test writeDataPoints routes to InfluxDB."""
    config = influxConfig()
    points = [MagicMock()]
    destination.writeDataPoints(config, points)
    mock_influx.writeInfluxPoints.assert_called_once_with(config, points)
    mock_vm.writePoints.assert_not_called()


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_write_routes_to_victoria_metrics(mock_influx, mock_vm):
    """Test writeDataPoints routes to VictoriaMetrics."""
    config = vmConfig()
    points = [MagicMock()]
    destination.writeDataPoints(config, points)
    mock_vm.writePoints.assert_called_once_with(config, points)
    mock_influx.writeInfluxPoints.assert_not_called()


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_get_last_db_timestamp_routes_to_influx(mock_influx, mock_vm):
    """Test getLastDBTimeStamp routes to InfluxDB and returns its result."""
    mock_influx.getLastDBTimeStamp.return_value = ('start', 'stop', True)
    config = influxConfig()
    result = destination.getLastDBTimeStamp(config, 'device', 'channel', '1m', 'a', 'b', False)
    assert result == ('start', 'stop', True)
    mock_influx.getLastDBTimeStamp.assert_called_once_with(config, 'device', 'channel', '1m', 'a', 'b', False)
    mock_vm.getLastTimeStamp.assert_not_called()


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_get_last_db_timestamp_routes_to_victoria_metrics(mock_influx, mock_vm):
    """Test getLastDBTimeStamp routes to VictoriaMetrics and returns its result."""
    mock_vm.getLastTimeStamp.return_value = ('start', 'stop', True)
    config = vmConfig()
    result = destination.getLastDBTimeStamp(config, 'device', 'channel', '1m', 'a', 'b', False)
    assert result == ('start', 'stop', True)
    mock_vm.getLastTimeStamp.assert_called_once_with(config, 'device', 'channel', '1m', 'a', 'b', False)
    mock_influx.getLastDBTimeStamp.assert_not_called()


# --- Test every entry point validates ---

@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_init_rejects_misconfiguration(mock_influx, mock_vm):
    """Test initConnection validates before reaching a destination."""
    with pytest.raises(ValueError, match='No database configured'):
        destination.initConnection({})
    mock_influx.initInfluxConnection.assert_not_called()
    mock_vm.initConnection.assert_not_called()


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_write_rejects_misconfiguration(mock_influx, mock_vm):
    """Test writeDataPoints validates before writing anywhere."""
    with pytest.raises(ValueError, match='only one of the two'):
        destination.writeDataPoints(dict(influxConfig(), **vmConfig()), [])
    mock_influx.writeInfluxPoints.assert_not_called()
    mock_vm.writePoints.assert_not_called()


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_get_last_db_timestamp_rejects_misconfiguration(mock_influx, mock_vm):
    """Test getLastDBTimeStamp validates before querying a destination."""
    with pytest.raises(ValueError, match='Unsupported influxDb version'):
        destination.getLastDBTimeStamp(influxConfig(3), 'd', 'c', '1m', 'a', 'b', False)
    mock_influx.getLastDBTimeStamp.assert_not_called()
    mock_vm.getLastTimeStamp.assert_not_called()
