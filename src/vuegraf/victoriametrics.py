# Copyright (c) Jason Ertel (jertel).
# This file is part of the Vuegraf project and is made available under the MIT License.

# Contains logic relating to preparing, retrieving and saving VictoriaMetrics data points.

import datetime
import json
import logging
import requests

from vuegraf.config import getConfigValue
from vuegraf.time import calculateResumeTimeRange
from vuegraf.telemetry import TelemetryPoint


logger = logging.getLogger('vuegraf.victoriametrics')

# Points written per request, to avoid sending a single enormous request when a large
# history backfill accumulates many points.
WRITE_BATCH_SIZE = 5000

# Lookback windows tried in order when locating the last stored sample. The widest matches
# the InfluxDB destination's 3 week horizon; the narrower ones exist because this query runs
# per channel on every collection cycle, and a 3 week window over per-second data scans
# millions of samples per series. In steady state the first window hits.
LOOKBACK_WINDOWS = ['10m', '6h', '3w']


def validateConfig(config):
    """Raises unless this destination's config section is usable.
    """
    section = config['victoriaMetrics']
    if not section.get('url'):
        raise ValueError('victoriaMetrics: a url entry is required.')
    if ('user' in section) != ('pass' in section):
        raise ValueError('victoriaMetrics: user and pass must be provided together.')


def getTags(config):
    """Returns the resolution label name and its per-resolution values.

    Mirrors the InfluxDB destination's tag settings, read from this destination's own
    config block so each destination owns its naming.
    """
    section = config['victoriaMetrics']
    tagName = 'detailed'
    if 'tagName' in section:
        tagName = section['tagName']
    tagValue_second = 'True'
    if 'tagValue_second' in section:
        tagValue_second = section['tagValue_second']
    tagValue_minute = 'False'
    if 'tagValue_minute' in section:
        tagValue_minute = section['tagValue_minute']
    tagValue_hour = 'Hour'
    if 'tagValue_hour' in section:
        tagValue_hour = section['tagValue_hour']
    tagValue_day = 'Day'
    if 'tagValue_day' in section:
        tagValue_day = section['tagValue_day']
    return tagName, tagValue_second, tagValue_minute, tagValue_hour, tagValue_day


def getNaming(config):
    """Returns the metric name and any static labels to emit.

    The default mirrors the InfluxDB measurement name, and the tag names emitted
    alongside it are the same, so both destinations produce comparable series.

    metricName  - overrides the metric name. Users migrating an existing InfluxDB
                  database with vmctl will want 'energy_usage_usage', since
                  VictoriaMetrics names line protocol data <measurement>_<field>.
    extraLabels - static labels added to every series, such as the 'db' label
                  VictoriaMetrics adds when ingesting InfluxDB line protocol.
    """
    metricName = 'energy_usage'
    if 'metricName' in config['victoriaMetrics']:
        metricName = config['victoriaMetrics']['metricName']
    extraLabels = {}
    if 'extraLabels' in config['victoriaMetrics']:
        extraLabels = config['victoriaMetrics']['extraLabels']
    return metricName, extraLabels


def getTimeoutSecs(config):
    """Requests are made with the 'requests' library, which expects a timeout in seconds,
    whereas the configured/default timeout follows the InfluxDB client convention of
    milliseconds."""
    timeout = config['victoriaMetrics']['timeout'] if 'timeout' in config['victoriaMetrics'] else 60_000
    return timeout / 1000


def createDataPoint(config, pt):
    """Creates the JSON import structure from a collect.Point."""
    tagName, _, _, _, _ = getTags(config)
    addStationField = getConfigValue(config, 'addStationField')
    metricName, extraLabels = getNaming(config)

    if isinstance(pt, TelemetryPoint):
        metric = dict(extraLabels)
        metric.update({'__name__': 'electrical_telemetry_' + pt.metric, 'account_name': pt.accountName,
                       'device_gid': str(pt.deviceGid), 'channel_num': pt.channelNum, tagName: pt.detailed})
        return {'metric': metric, 'values': [pt.value], 'timestamps': [int(pt.timestamp.timestamp() * 1000)]}

    # Seeded with extraLabels so that the labels below always win, and a stray
    # extraLabels entry can never displace the metric name or a tag.
    metric = dict(extraLabels)
    metric['__name__'] = metricName
    metric['account_name'] = pt.accountName
    metric['device_name'] = pt.chanName
    metric[tagName] = pt.detailed
    if addStationField:
        metric['station_name'] = pt.deviceName

    return {
        'metric': metric,
        # VictoriaMetrics expects millisecond timestamps on the JSON import endpoint.
        'values': [pt.usageWatts],
        'timestamps': [int(pt.timestamp.timestamp() * 1000)],
    }


def getLastTimeStamp(config, deviceName, chanName, pointType, startTime, stopTime, fillInMissingData):
    """Returns the time range to fetch, based on the last sample already stored."""
    tagName, tagValue_second, tagValue_minute, _, _ = getTags(config)
    addStationField = getConfigValue(config, 'addStationField')
    metricName, extraLabels = getNaming(config)

    # json.dumps quotes label values, escaping embedded quotes, backslashes and control
    # characters the way PromQL string literals expect.
    labelFilters = ['device_name=' + json.dumps(chanName),
                    tagName + '=' + json.dumps(pointType)]
    # Scope by extraLabels too, so this cannot match a same-named series written by some
    # other source into the same VictoriaMetrics.
    for labelName in sorted(extraLabels):
        labelFilters.append(labelName + '=' + json.dumps(extraLabels[labelName]))
    if addStationField:
        labelFilters.append('station_name=' + json.dumps(deviceName))
    selector = metricName + '{' + ','.join(labelFilters) + '}'

    url = config['victoriaMetrics']['url'].rstrip('/') + '/api/v1/query'
    timeoutSecs = getTimeoutSecs(config)
    timeStr = ''
    for window in LOOKBACK_WINDOWS:
        # tlast_over_time returns the timestamp of the last raw sample, which is the
        # MetricsQL equivalent of Influx's last(). Note that timestamp(last_over_time(..))
        # would instead return the query evaluation time, which is always ~now and would
        # therefore silently suppress all backfilling.
        promQuery = 'tlast_over_time(' + selector + '[' + window + '])'
        logger.debug('VictoriaMetrics Query: %s', promQuery)
        response = config['victoriaMetricsSession'].get(url, params={'query': promQuery}, timeout=timeoutSecs)
        response.raise_for_status()
        # Indexed rather than .get()-chained on purpose; a well-formed response always
        # carries these keys, and quietly treating an unexpected payload as "no data"
        # would trigger the full 7 day rewind on every collection cycle.
        result = response.json()['data']['result']

        if len(result) > 0:
            epochSeconds = float(result[0]['value'][1])
            timeStr = datetime.datetime.fromtimestamp(epochSeconds, tz=datetime.timezone.utc).isoformat()
            break

    return calculateResumeTimeRange(config, timeStr, pointType, tagValue_second, tagValue_minute,
                                    startTime, stopTime, fillInMissingData)


def initConnection(config):
    logger.info('Using VictoriaMetrics')

    sslVerify = True
    if 'ssl_verify' in config['victoriaMetrics']:
        sslVerify = config['victoriaMetrics']['ssl_verify']

    session = requests.Session()
    session.verify = sslVerify
    # Only authenticate to ingress if 'user' entry was provided in config;
    if 'user' in config['victoriaMetrics']:
        session.auth = (config['victoriaMetrics']['user'], config['victoriaMetrics']['pass'])
    if 'token' in config['victoriaMetrics']:
        session.headers['Authorization'] = 'Bearer ' + config['victoriaMetrics']['token']

    if config['args'].resetdatabase:
        logger.info('Resetting database')
        metricName, extraLabels = getNaming(config)
        deleteUrl = config['victoriaMetrics']['url'].rstrip('/') + '/api/v1/admin/tsdb/delete_series'
        # Scoped by extraLabels so that a reset cannot delete same-named series written
        # into this VictoriaMetrics by another source.
        selector = '{__name__=' + json.dumps(metricName)
        for labelName in sorted(extraLabels):
            selector += ',' + labelName + '=' + json.dumps(extraLabels[labelName])
        selector += '}'
        response = session.post(deleteUrl, params={'match[]': selector}, timeout=getTimeoutSecs(config))
        response.raise_for_status()

    config['victoriaMetricsSession'] = session


def writePoints(config, usageDataPoints):
    """Writes a list of collect.Point objects to VictoriaMetrics."""
    logger.info('Submitting datapoints to VictoriaMetrics database; points={}'.format(len(usageDataPoints)))
    dataPoints = [createDataPoint(config, pt) for pt in usageDataPoints]
    if config['args'].debug:
        dumpPoints(config, 'Sending to database', dataPoints)
    if config['args'].dryrun:
        logger.info('Dryrun mode enabled.  Skipping database write.')
        return

    url = config['victoriaMetrics']['url'].rstrip('/') + '/api/v1/import'
    timeoutSecs = getTimeoutSecs(config)
    for batchStart in range(0, len(dataPoints), WRITE_BATCH_SIZE):
        batch = dataPoints[batchStart:batchStart + WRITE_BATCH_SIZE]
        body = '\n'.join(json.dumps(point) for point in batch)
        response = config['victoriaMetricsSession'].post(url, data=body, timeout=timeoutSecs)
        response.raise_for_status()


def dumpPoints(config, label, dataPoints):
    logger.debug(label)
    for point in dataPoints:
        logger.debug('  {}'.format(json.dumps(point)))
