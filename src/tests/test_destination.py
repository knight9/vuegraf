# Copyright (c) Jason Ertel (jertel).
# This file is part of the Vuegraf project and is made available under the MIT License.

import pytest
from unittest.mock import MagicMock, patch

# Local imports
from vuegraf import destination


def influxConfig(version=2):
    return {'influxDb': {'version': version}}


def vmConfig(version='victoriametrics'):
    return {'influxDb': {'version': version}}


# --- Test usesVictoriaMetrics ---

def test_uses_victoria_metrics_when_configured():
    """Test the VictoriaMetrics destination is selected for its version string."""
    assert destination.usesVictoriaMetrics(vmConfig()) is True


def test_uses_victoria_metrics_normalizes_casing_and_whitespace():
    """Test the version string is matched regardless of casing or surrounding whitespace."""
    assert destination.usesVictoriaMetrics(vmConfig('  VictoriaMetrics ')) is True


def test_uses_victoria_metrics_false_for_influx_versions():
    """Test numeric InfluxDB versions do not select VictoriaMetrics."""
    assert destination.usesVictoriaMetrics(influxConfig(1)) is False
    assert destination.usesVictoriaMetrics(influxConfig(2)) is False


# --- Test usesInflux ---

def test_uses_influx_for_supported_versions():
    """Test both InfluxDB versions are matched explicitly."""
    assert destination.usesInflux(influxConfig(1)) is True
    assert destination.usesInflux(influxConfig(2)) is True


def test_uses_influx_defaults_when_version_absent():
    """Test an absent version selects the InfluxDB destination."""
    assert destination.usesInflux({'influxDb': {}}) is True


def test_uses_influx_false_for_unsupported_version():
    """Test an unsupported numeric version is not claimed by InfluxDB.

    Previously a version such as 3 fell through to the v1 code path and failed later
    with a confusing error about a missing 'host' config field.
    """
    assert destination.usesInflux(influxConfig(3)) is False


def test_uses_influx_false_for_victoria_metrics():
    """Test the VictoriaMetrics version is not claimed by InfluxDB."""
    assert destination.usesInflux(vmConfig()) is False


# --- Test routing ---

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
    mock_vm.VICTORIA_METRICS_VERSION = 'victoriametrics'
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
    mock_vm.VICTORIA_METRICS_VERSION = 'victoriametrics'
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
    mock_vm.VICTORIA_METRICS_VERSION = 'victoriametrics'
    mock_vm.getLastTimeStamp.return_value = ('start', 'stop', True)
    config = vmConfig()
    result = destination.getLastDBTimeStamp(config, 'device', 'channel', '1m', 'a', 'b', False)
    assert result == ('start', 'stop', True)
    mock_vm.getLastTimeStamp.assert_called_once_with(config, 'device', 'channel', '1m', 'a', 'b', False)
    mock_influx.getLastDBTimeStamp.assert_not_called()


# --- Test unsupported versions are rejected by every entry point ---

@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_init_rejects_unsupported_version(mock_influx, mock_vm):
    """Test an unrecognized version raises rather than reaching a destination."""
    mock_vm.VICTORIA_METRICS_VERSION = 'victoriametrics'
    with pytest.raises(ValueError, match='Unsupported influxDb version'):
        destination.initConnection(influxConfig(3))
    mock_influx.initInfluxConnection.assert_not_called()
    mock_vm.initConnection.assert_not_called()


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_write_rejects_unsupported_version(mock_influx, mock_vm):
    """Test an unrecognized version raises rather than writing anywhere."""
    mock_vm.VICTORIA_METRICS_VERSION = 'victoriametrics'
    with pytest.raises(ValueError, match='Unsupported influxDb version'):
        destination.writeDataPoints(vmConfig('victoria'), [])
    mock_influx.writeInfluxPoints.assert_not_called()
    mock_vm.writePoints.assert_not_called()


@patch('vuegraf.destination.victoriametrics')
@patch('vuegraf.destination.influx')
def test_get_last_db_timestamp_rejects_unsupported_version(mock_influx, mock_vm):
    """Test an unrecognized version raises rather than querying a destination."""
    mock_vm.VICTORIA_METRICS_VERSION = 'victoriametrics'
    with pytest.raises(ValueError, match='Unsupported influxDb version'):
        destination.getLastDBTimeStamp(vmConfig('victoria'), 'd', 'c', '1m', 'a', 'b', False)
    mock_influx.getLastDBTimeStamp.assert_not_called()
    mock_vm.getLastTimeStamp.assert_not_called()
