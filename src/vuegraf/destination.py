# Copyright (c) Jason Ertel (jertel).
# This file is part of the Vuegraf project and is made available under the MIT License.

# Routes output operations to whatever is configured. Contains no destination specific
# logic of its own; each is implemented in its own module and reads its own config
# section. More than one may be configured, in which case every data point is offered to
# each and filtered to what that destination is missing.
#
# Destinations that record a resume point - the databases - are asked where to resume
# from and are sent only what they are missing. MQTT keeps no such state, so it is never
# consulted about resuming and always receives the full set of points.

from vuegraf import influx, victoriametrics
from vuegraf.config import getInfluxTag, getInfluxVersion
from vuegraf.mqtt import initMqttConnectionIfConfigured, publishMqttMessagesIfConnected, stopMqttIfConnected
from vuegraf.telemetry import TelemetryPoint, validateConfig as validateTelemetryConfig


# Versions served by the InfluxDB destination.
INFLUX_VERSIONS = [1, 2]

# Config section names, also used to key the per-database resume state.
INFLUX = 'influxDb'
VICTORIA_METRICS = 'victoriaMetrics'
MQTT = 'mqtt'


def usesInflux(config):
    """Returns True when an InfluxDB database is configured."""
    return bool(config.get(INFLUX))


def usesVictoriaMetrics(config):
    """Returns True when a VictoriaMetrics database is configured."""
    return bool(config.get(VICTORIA_METRICS))


def usesMqtt(config):
    """Returns True when an MQTT output is configured."""
    return bool(config.get(MQTT))


def validateDestination(config):
    """Raises unless at least one supported database is correctly configured.

    Each is matched on the presence of its own config section.
    Configuring more than one is supported; every database then
    receives the same data points, filtered to whatever each is missing.

    Called once from initConnection, so that a configuration mistake is reported at
    startup rather than partway through the first collection cycle.
    """
    validateTelemetryConfig(config)
    if not usesInflux(config) and not usesVictoriaMetrics(config):
        raise ValueError('No database configured; expected an influxDb or '
                         'victoriaMetrics section.')
    if usesInflux(config) and getInfluxVersion(config) not in INFLUX_VERSIONS:
        raise ValueError('Unsupported influxDb version: {}; expected one of {}'.format(
                         getInfluxVersion(config), INFLUX_VERSIONS))
    if usesVictoriaMetrics(config):
        victoriametrics.validateConfig(config)
    if usesInflux(config) and usesVictoriaMetrics(config):
        # Collection stamps the resolution value onto every data point before any database
        # sees it, so a single value has to satisfy both. tagName is excluded: each
        # database reads its own when writing and when querying, so those may differ.
        if getInfluxTag(config)[1:] != victoriametrics.getTags(config)[1:]:
            raise ValueError('influxDb and victoriaMetrics tagValue_* settings must match '
                             'when both are configured.')


def getTags(config):
    """Returns the resolution tag name and values for the configured databases.

    Collection stamps this value onto every data point before any database sees it, so it
    is resolved here rather than read from a fixed config section. When both databases are
    configured their tagValue_* settings were validated at startup to be identical.
    """
    if usesVictoriaMetrics(config):
        return victoriametrics.getTags(config)
    return getInfluxTag(config)


def getResumeState(config):
    """Per-database resume point, keyed by series.

    Each entry is (resumeStart, seriesNeedsBackfill), where the flag is true if ANY
    database is backfilling this series. Populated by getLastDBTimeStamp during collection
    and consumed by writeDataPoints to decide what each database is missing. Bound to the
    config dict, and cleared after each write so that a stale entry cannot suppress a
    later point.
    """
    return config.setdefault('_resumeState', {})


def getLastDBTimeStamp(config, deviceName, chanName, pointType, startTime, stopTime, fillInMissingData):
    """Returns the time range to fetch so that every configured database is satisfied.

    Each database works out its own resume window. The widest is returned, so a single
    Emporia request covers whichever database is furthest behind; writeDataPoints then
    trims the result per database.
    """
    windows = {}
    if usesInflux(config):
        windows[INFLUX] = influx.getLastDBTimeStamp(config, deviceName, chanName, pointType,
                                                    startTime, stopTime, fillInMissingData)
    if usesVictoriaMetrics(config):
        windows[VICTORIA_METRICS] = victoriametrics.getLastTimeStamp(
            config, deviceName, chanName, pointType, startTime, stopTime, fillInMissingData)

    # A backfill for one database widens the window for the whole series, so the others
    # are told about it too and can trim their own share of the result.
    seriesNeedsBackfill = any(window[2] for window in windows.values())
    state = getResumeState(config)
    for name, window in windows.items():
        state[(name, deviceName, chanName, pointType)] = (window[0], seriesNeedsBackfill)

    # The earliest start is the widest window, and its stop is the batch end that goes
    # with it.
    widest = min(windows.values(), key=lambda window: window[0])
    return widest[0], widest[1], seriesNeedsBackfill


def pointsMissingFrom(config, name, usageDataPoints):
    """Returns the points the named database does not already hold.

    With a single database this is every point, preserving existing behaviour. With more
    than one, the fetch window was widened to satisfy the database furthest behind, so the
    others would otherwise be re-sent history they already have.

    A database that is within the no-backfill threshold reports its resume point as the
    collection instant, so it may be trimmed of a point or two it actually wanted. That
    resolves on a later cycle: the threshold advances while its stored record does not, so
    the series shortly reports a backfill and the gap is filled.
    """
    if not (usesInflux(config) and usesVictoriaMetrics(config)):
        return usageDataPoints

    state = getResumeState(config)
    missing = []
    for pt in usageDataPoints:
        if isinstance(pt, TelemetryPoint):
            missing.append(pt)
            continue
        entry = state.get((name, pt.deviceName, pt.chanName, pt.detailed))
        if entry is None:
            # Nothing was looked up for this series - hourly and daily points never
            # consult the resume state - so it takes everything collected.
            missing.append(pt)
            continue
        resumeStart, seriesNeedsBackfill = entry
        # With no backfill anywhere on this series, collection emitted a single current
        # sample timestamped to the minute, which is earlier than resumeStart and must
        # not be trimmed.
        if not seriesNeedsBackfill or pt.timestamp >= resumeStart:
            missing.append(pt)
    return missing


def initConnection(config):
    validateDestination(config)
    from vuegraf.telemetry_recovery import initialize
    initialize(config)
    if usesInflux(config):
        influx.initInfluxConnection(config)
    if usesVictoriaMetrics(config):
        victoriametrics.initConnection(config)
    if usesMqtt(config):
        initMqttConnectionIfConfigured(config)


def writeDataPoints(config, usageDataPoints):
    if not config.get('legacyEnergyEnabled', True):
        from vuegraf.telemetry import TelemetryPoint
        usageDataPoints = [point for point in usageDataPoints if isinstance(point, TelemetryPoint)]
        if not usageDataPoints:
            return
    if usesInflux(config):
        influx.writeInfluxPoints(config, pointsMissingFrom(config, INFLUX, usageDataPoints))
    if usesVictoriaMetrics(config):
        victoriametrics.writePoints(config, pointsMissingFrom(config, VICTORIA_METRICS, usageDataPoints))
    # Commit coverage only after every configured database acknowledges the batch.
    # A crash before this commit causes safe, idempotent replay, never lost gaps.
    if config.get('_telemetryRecovery') is not None:
        config['_telemetryRecovery'].record(usageDataPoints)
    # Records no resume point, so it is offered every point and does its own filtering.
    if usesMqtt(config):
        publishMqttMessagesIfConnected(config, usageDataPoints)
    getResumeState(config).clear()


def closeConnection(config):
    if config.get('_telemetryRecovery') is not None:
        config['_telemetryRecovery'].close()
    if usesMqtt(config):
        stopMqttIfConnected(config)
