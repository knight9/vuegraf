# Copyright (c) Jason Ertel (jertel).
# This file is part of the Vuegraf project and is made available under the MIT License.

# Routes database operations to the configured destination. Contains no destination
# specific logic of its own; each destination is implemented in its own module.

import logging

from vuegraf import influx, victoriametrics
from vuegraf.config import getInfluxVersion


logger = logging.getLogger('vuegraf.destination')

# Versions served by the InfluxDB destination.
INFLUX_VERSIONS = [1, 2]


def usesInflux(config):
    """Returns True when the configured destination is InfluxDB."""
    return getInfluxVersion(config) in INFLUX_VERSIONS


def usesVictoriaMetrics(config):
    """Returns True when the configured destination is VictoriaMetrics."""
    version = getInfluxVersion(config)
    if not isinstance(version, str):
        return False
    return version.strip().lower() == victoriametrics.VICTORIA_METRICS_VERSION


def unsupportedVersionError(config):
    """Returns the error raised when the configured version matches no destination.

    Every destination is matched explicitly rather than one of them acting as a
    fallback, so an unrecognized version fails here instead of reaching a destination
    that cannot serve it and failing later on a missing config field.
    """
    return ValueError("Unsupported influxDb version: {}; expected one of {} or '{}'".format(
                      getInfluxVersion(config), INFLUX_VERSIONS,
                      victoriametrics.VICTORIA_METRICS_VERSION))


def initConnection(config):
    if usesVictoriaMetrics(config):
        victoriametrics.initConnection(config)
    elif usesInflux(config):
        influx.initInfluxConnection(config)
    else:
        raise unsupportedVersionError(config)


def writeDataPoints(config, usageDataPoints):
    if usesVictoriaMetrics(config):
        victoriametrics.writePoints(config, usageDataPoints)
    elif usesInflux(config):
        influx.writeInfluxPoints(config, usageDataPoints)
    else:
        raise unsupportedVersionError(config)


def getLastDBTimeStamp(config, deviceName, chanName, pointType, startTime, stopTime, fillInMissingData):
    if usesVictoriaMetrics(config):
        return victoriametrics.getLastTimeStamp(config, deviceName, chanName, pointType,
                                                startTime, stopTime, fillInMissingData)
    elif usesInflux(config):
        return influx.getLastDBTimeStamp(config, deviceName, chanName, pointType,
                                         startTime, stopTime, fillInMissingData)
    raise unsupportedVersionError(config)
