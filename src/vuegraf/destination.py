# Copyright (c) Jason Ertel (jertel).
# This file is part of the Vuegraf project and is made available under the MIT License.

# Routes database operations to the configured database. Contains no database specific
# logic of its own; each is implemented in its own module and reads its own config
# section. The MQTT output is not routed here - it is additive and runs alongside
# whichever database is configured.

import logging

from vuegraf import influx, victoriametrics
from vuegraf.config import getInfluxTag, getInfluxVersion


logger = logging.getLogger('vuegraf.destination')

# Versions served by the InfluxDB destination.
INFLUX_VERSIONS = [1, 2]


def usesInflux(config):
    """Returns True when an InfluxDB database is configured."""
    return bool(config.get('influxDb'))


def usesVictoriaMetrics(config):
    """Returns True when a VictoriaMetrics database is configured."""
    return bool(config.get('victoriaMetrics'))


def validateDestination(config):
    """Raises unless exactly one supported database is configured.

    Each is matched on the presence of its own config section, following the pattern used
    by the MQTT output, rather than one acting as a fallback for anything unrecognized.
    Note that MQTT is additive and is not mutually exclusive with either database.
    """
    if usesInflux(config) and usesVictoriaMetrics(config):
        raise ValueError('Both influxDb and victoriaMetrics sections are configured; '
                         'only one of the two may be used.')
    if not usesInflux(config) and not usesVictoriaMetrics(config):
        raise ValueError('No database configured; expected an influxDb or '
                         'victoriaMetrics section.')
    if usesInflux(config) and getInfluxVersion(config) not in INFLUX_VERSIONS:
        raise ValueError('Unsupported influxDb version: {}; expected one of {}'.format(
                         getInfluxVersion(config), INFLUX_VERSIONS))


def getTags(config):
    """Returns the resolution tag name and values for the configured destination.

    Collection stamps this value onto every data point before any destination sees it, so
    it is resolved here rather than read from a fixed config section.
    """
    validateDestination(config)
    if usesVictoriaMetrics(config):
        return victoriametrics.getTags(config)
    return getInfluxTag(config)


def initConnection(config):
    validateDestination(config)
    if usesVictoriaMetrics(config):
        victoriametrics.initConnection(config)
    else:
        influx.initInfluxConnection(config)


def writeDataPoints(config, usageDataPoints):
    validateDestination(config)
    if usesVictoriaMetrics(config):
        victoriametrics.writePoints(config, usageDataPoints)
    else:
        influx.writeInfluxPoints(config, usageDataPoints)


def getLastDBTimeStamp(config, deviceName, chanName, pointType, startTime, stopTime, fillInMissingData):
    validateDestination(config)
    if usesVictoriaMetrics(config):
        return victoriametrics.getLastTimeStamp(config, deviceName, chanName, pointType,
                                                startTime, stopTime, fillInMissingData)
    return influx.getLastDBTimeStamp(config, deviceName, chanName, pointType,
                                     startTime, stopTime, fillInMissingData)
