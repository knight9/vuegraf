# Copyright (c) Jason Ertel (jertel).
# This file is part of the Vuegraf project and is made available under the MIT License.

# Contains logic relating to preparing, retrieving and saving InfluxDB data points.

import datetime
import influxdb         # InfluxDB v1
import influxdb_client  # InfluxDB v2
import json
import logging
import pprint
import requests          # VictoriaMetrics

from vuegraf.config import (
  VICTORIA_METRICS_VERSION,
  getConfigValue,
  getInfluxTag,
  getInfluxVersion,
  getVictoriaMetricsNaming,
)
from vuegraf.time import getTimeNow


logger = logging.getLogger('vuegraf.influx')

# Points written per request, to avoid sending a single enormous request when a large
# history backfill accumulates many points. Shared by all backends.
WRITE_BATCH_SIZE = 5000

# Lookback windows tried in order when locating the last stored sample. The widest matches
# the InfluxDB backends' 3 week horizon; the narrower ones exist because this query runs per
# channel on every collection cycle, and a 3 week window over per-second data scans millions
# of samples per series. In steady state the first window hits.
VICTORIA_METRICS_LOOKBACK_WINDOWS = ['10m', '6h', '3w']


def getVictoriaMetricsTimeoutSecs(config):
    """VictoriaMetrics requests are made with the 'requests' library, which expects a
    timeout in seconds, whereas the configured/default timeout follows the InfluxDB
    client convention of milliseconds."""
    timeout = config['influxDb']['timeout'] if 'timeout' in config['influxDb'] else 60_000
    return timeout / 1000


def createDataPoint(config, pt):
    """Creates appropriate Influx structure from a collect.Point."""
    accountName = pt.accountName
    deviceName = pt.deviceName
    chanName = pt.chanName
    watts = pt.usageWatts
    timestamp = pt.timestamp
    detailed = pt.detailed

    influxVersion = getInfluxVersion(config)
    tagName, tagValue_second, tagValue_minute, tagValue_hour, tagValue_day = getInfluxTag(config)
    addStationField = getConfigValue(config, 'addStationField')

    dataPoint = None
    if influxVersion == 2:
        dataPoint = influxdb_client.Point('energy_usage')
        dataPoint.tag('account_name', accountName)
        dataPoint.tag('device_name', chanName)
        dataPoint.tag(tagName, detailed)
        dataPoint.field('usage', watts)
        dataPoint.time(time=timestamp)
        if addStationField:
            dataPoint.tag('station_name', deviceName)
    elif influxVersion == VICTORIA_METRICS_VERSION:
        metricName, extraLabels = getVictoriaMetricsNaming(config)
        # Seeded with extraLabels so that the labels below always win, and a stray
        # extraLabels entry can never displace the metric name or a tag.
        metric = dict(extraLabels)
        metric['__name__'] = metricName
        metric['account_name'] = accountName
        metric['device_name'] = chanName
        metric[tagName] = detailed
        if addStationField:
            metric['station_name'] = deviceName
        dataPoint = {
            'metric': metric,
            # VictoriaMetrics expects millisecond timestamps on the JSON import endpoint.
            'values': [watts],
            'timestamps': [int(timestamp.timestamp() * 1000)],
        }
    else:
        dataPoint = {
            'measurement': 'energy_usage',
            'tags': {
                'account_name': accountName,
                'device_name': chanName,
                tagName: detailed,
            },
            'fields': {
                'usage': watts,
            },
            'time': timestamp
        }
        if addStationField:
            dataPoint['tags']['station_name'] = deviceName

    return dataPoint


def getLastDBTimeStamp(config, deviceName, chanName, pointType, startTime, stopTime, fillInMissingData):
    tagName, tagValue_second, tagValue_minute, tagValue_hour, tagValue_day = getInfluxTag(config)
    influxVersion = getInfluxVersion(config)
    addStationField = getConfigValue(config, 'addStationField')
    timeStr = ''
    # Get timestamp of last record in database
    # Influx v2
    if influxVersion == 2:
        stationFilter = ""
        if addStationField:
            stationFilter = '  r.station_name == "' + deviceName + '" and '
        bucket = config['influxDb']['bucket']
        query_api = config['influx'].query_api()
        result = query_api.query('from(bucket:"' + bucket + '") ' +
                                 '|> range(start: -3w) ' +
                                 '|> filter(fn: (r) => ' +
                                 '  r._measurement == "energy_usage" and ' +
                                 '  r.' + tagName + ' == "' + pointType + '" and ' +
                                 '  r._field == "usage" and ' + stationFilter +
                                 '  r.device_name == "' + chanName + '")' +
                                 '|> last()')

        if len(result) > 0 and len(result[0].records) > 0:
            lastRecord = result[0].records[0]
            timeStr = lastRecord['_time'].isoformat()

    elif influxVersion == VICTORIA_METRICS_VERSION:
        metricName, extraLabels = getVictoriaMetricsNaming(config)
        # json.dumps quotes label values, escaping embedded quotes, backslashes and
        # control characters the way PromQL string literals expect.
        labelFilters = ['device_name=' + json.dumps(chanName),
                        tagName + '=' + json.dumps(pointType)]
        # Scope by extraLabels too, so this cannot match a same-named series written by
        # some other source into the same VictoriaMetrics.
        for labelName in sorted(extraLabels):
            labelFilters.append(labelName + '=' + json.dumps(extraLabels[labelName]))
        if addStationField:
            labelFilters.append('station_name=' + json.dumps(deviceName))
        selector = metricName + '{' + ','.join(labelFilters) + '}'
        url = config['influxDb']['url'].rstrip('/') + '/api/v1/query'
        timeoutSecs = getVictoriaMetricsTimeoutSecs(config)
        for window in VICTORIA_METRICS_LOOKBACK_WINDOWS:
            # tlast_over_time returns the timestamp of the last raw sample, which is the
            # MetricsQL equivalent of Influx's last(). Note that timestamp(last_over_time(..))
            # would instead return the query evaluation time, which is always ~now and would
            # therefore silently suppress all backfilling.
            promQuery = 'tlast_over_time(' + selector + '[' + window + '])'
            logger.debug('VictoriaMetrics Query: %s', promQuery)
            response = config['influx'].get(url, params={'query': promQuery}, timeout=timeoutSecs)
            response.raise_for_status()
            # Indexed rather than .get()-chained on purpose; a well-formed response always
            # carries these keys, and quietly treating an unexpected payload as "no data"
            # would trigger the full 7 day rewind on every collection cycle.
            result = response.json()['data']['result']

            if len(result) > 0:
                epochSeconds = float(result[0]['value'][1])
                timeStr = datetime.datetime.fromtimestamp(epochSeconds, tz=datetime.timezone.utc).isoformat()
                break

    else:  # Influx v1
        stationFilter = ""
        if addStationField:
            stationFilter = 'station_name = \'' + deviceName.replace('\'', '\\\'') + '\' AND '
        query = 'select last(usage), time from energy_usage where (' + stationFilter + \
                'device_name = \'' + chanName.replace('\'', '\\\'') + '\' AND ' + \
                tagName.replace('\'', '\\\'') + ' = \'' + pointType + '\')'
        logger.debug('InfluxDB v1 Query: %s', query)
        result = config['influx'].query(query)

        if len(result) > 0:
            timeStr = next(result.get_points())['time']

    # Depending on version of Influx, the string format for the time is different.
    # So strip out the variable timezone bits (along with any microsecond values)
    if len(timeStr) > 0:
        timeStr = timeStr[:19] + 'Z'

        # Convert the timeStr into an aware datetime object.
        dbLastRecordTime = datetime.datetime.strptime(timeStr, '%Y-%m-%dT%H:%M:%S%z').replace(tzinfo=datetime.timezone.utc)

        if pointType == tagValue_minute:
            if dbLastRecordTime < (stopTime - datetime.timedelta(minutes=2, seconds=stopTime.second)):
                fillInMissingData = True
                startTime = dbLastRecordTime + datetime.timedelta(minutes=1)
                # Can only back a maximum of 7 days for minute data.
                # So if last record in DB exceeds 7 days, set the startTime to be 7 days ago.
                if int((stopTime - startTime).total_seconds()) > 604800:      # 7 Days
                    startTime = stopTime - datetime.timedelta(minutes=10080)  # 7 Days

                # Can only get a maximum of 12 hours worth of minute data in a single API call.
                # If more than 12 hours worth is needed, get data in batches; set stopTime to be
                # 12 hours more than the starttime
                if int((stopTime - startTime).total_seconds()) > 43200:       # 12 Hours
                    stopTime = startTime + datetime.timedelta(minutes=720)    # 12 Hours

        if pointType == tagValue_second:
            if dbLastRecordTime < (startTime - datetime.timedelta(seconds=2)):
                fillInMissingData = True
                startTime = (dbLastRecordTime + datetime.timedelta(seconds=1)).replace(microsecond=0)
                # Adjust start or stop times if backfill interval exceeds 1 hour
                if (int((stopTime - startTime).total_seconds()) > 3600):
                    detailedIntervalSecs = getConfigValue(config, 'detailedIntervalSecs')
                    # Can never get more than 1 hour of historical second data if detailedIntervalSecs
                    # is set to greater than 1h.  Set backfill period to be just the past one hour in that case.
                    if (detailedIntervalSecs > 3600):
                        # 1 Hour max since detailedIntervalSecs is more than 1 hour
                        startTime = stopTime - datetime.timedelta(seconds=3600)
                    else:
                        # Can only backfill a maximum of 3 hours for second data.
                        # So if last record in DB exceeds 3 hours, set the startTime to be 3 hours ago.
                        if int((stopTime - startTime).total_seconds()) > 10800:        # 3 Hours
                            startTime = stopTime - datetime.timedelta(seconds=10800)   # 3 Hours

                        # Can only get a maximum of 1 hour's worth of second data in a single API call.
                        # If more than 1 hour's worth is needed, get data in batches; set stopTime to be
                        # 1 hour more than the starttime
                        stopTime = startTime + datetime.timedelta(seconds=3600)  # limit to 1 hour batch
    else:
        if pointType == tagValue_minute:
            startTime = startTime - datetime.timedelta(days=7)
            stopTime = startTime + datetime.timedelta(hours=12)
            fillInMissingData = True
        elif pointType == tagValue_second:
            startTime = startTime - datetime.timedelta(hours=3)
            stopTime = startTime + datetime.timedelta(hours=1)
            fillInMissingData = True

    return startTime, stopTime, fillInMissingData


def initInfluxConnection(config):
    sslVerify = True
    if 'ssl_verify' in config['influxDb']:
        sslVerify = config['influxDb']['ssl_verify']

    timeout = config['influxDb']['timeout'] if 'timeout' in config['influxDb'] else 60_000

    influxVersion = getInfluxVersion(config)
    if influxVersion == 2:
        logger.info('Using InfluxDB version 2')
        bucket = config['influxDb']['bucket']
        org = config['influxDb']['org']
        token = config['influxDb']['token']
        url = config['influxDb']['url']
        influx = influxdb_client.InfluxDBClient(
           url=url,
           token=token,
           org=org,
           verify_ssl=sslVerify,
           timeout=timeout,
        )

        if config['args'].resetdatabase:
            logger.info('Resetting database')
            delete_api = influx.delete_api()
            start = '1970-01-01T00:00:00Z'
            now = getTimeNow(datetime.UTC)
            stop = now.isoformat(timespec='seconds').replace("+00:00", "") + 'Z'
            delete_api.delete(start, stop, '_measurement="energy_usage"', bucket=bucket, org=org)

    elif influxVersion == VICTORIA_METRICS_VERSION:
        logger.info('Using VictoriaMetrics')
        url = config['influxDb']['url']
        influx = requests.Session()
        influx.verify = sslVerify
        # Only authenticate to ingress if 'user' entry was provided in config. A missing
        # 'pass' raises here rather than silently connecting unauthenticated, matching v1.
        if 'user' in config['influxDb']:
            influx.auth = (config['influxDb']['user'], config['influxDb']['pass'])
        if 'token' in config['influxDb']:
            influx.headers['Authorization'] = 'Bearer ' + config['influxDb']['token']

        if config['args'].resetdatabase:
            logger.info('Resetting database')
            metricName, extraLabels = getVictoriaMetricsNaming(config)
            deleteUrl = url.rstrip('/') + '/api/v1/admin/tsdb/delete_series'
            # Scoped by extraLabels so that a reset cannot delete same-named series
            # written into this VictoriaMetrics by another source.
            selector = '{__name__=' + json.dumps(metricName)
            for labelName in sorted(extraLabels):
                selector += ',' + labelName + '=' + json.dumps(extraLabels[labelName])
            selector += '}'
            response = influx.post(deleteUrl, params={'match[]': selector}, timeout=(timeout / 1000))
            response.raise_for_status()

    else:
        logger.info('Using InfluxDB version 1')

        sslEnable = False
        if 'ssl_enable' in config['influxDb']:
            sslEnable = config['influxDb']['ssl_enable']

        # Only authenticate to ingress if 'user' entry was provided in config
        if 'user' in config['influxDb']:
            influx = influxdb.InfluxDBClient(host=config['influxDb']['host'], port=config['influxDb']['port'], timeout=timeout,
                                             username=config['influxDb']['user'], password=config['influxDb']['pass'],
                                             database=config['influxDb']['database'], ssl=sslEnable, verify_ssl=sslVerify)
        else:
            influx = influxdb.InfluxDBClient(host=config['influxDb']['host'], port=config['influxDb']['port'], timeout=timeout,
                                             database=config['influxDb']['database'], ssl=sslEnable, verify_ssl=sslVerify)

        influx.create_database(config['influxDb']['database'])

        if config['args'].resetdatabase:
            logger.info('Resetting database')
            influx.delete_series(measurement='energy_usage')

    config['influx'] = influx


def writeInfluxPoints(config, usageDataPoints):
    """Writes a list of collect.Point objects to the Influx db.

    Converts to the appropriate internal Influx data format for writing.
    """
    # Write to database after each historical batch to prevent timeout issues on large history intervals.
    logger.info('Submitting datapoints to database; points={}'.format(len(usageDataPoints)))
    influxPoints = [createDataPoint(config, pt) for pt in usageDataPoints]
    if config['args'].debug:
        dumpPoints(config, "Sending to database", influxPoints)
    if config['args'].dryrun:
        logger.info('Dryrun mode enabled.  Skipping database write.')
    else:
        influxVersion = getInfluxVersion(config)
        if influxVersion == 2:
            bucket = config['influxDb']['bucket']
            write_api = config['influx'].write_api(write_options=influxdb_client.client.write_api.SYNCHRONOUS)
            write_api.write(bucket=bucket, record=influxPoints)
        elif influxVersion == VICTORIA_METRICS_VERSION:
            url = config['influxDb']['url'].rstrip('/') + '/api/v1/import'
            timeoutSecs = getVictoriaMetricsTimeoutSecs(config)
            for batchStart in range(0, len(influxPoints), WRITE_BATCH_SIZE):
                batch = influxPoints[batchStart:batchStart + WRITE_BATCH_SIZE]
                body = '\n'.join(json.dumps(point) for point in batch)
                response = config['influx'].post(url, data=body, timeout=timeoutSecs)
                response.raise_for_status()
        else:
            config['influx'].write_points(influxPoints, batch_size=WRITE_BATCH_SIZE)


def dumpPoints(config, label, usageDataPoints):
    influxVersion = getInfluxVersion(config)
    logger.debug(label)
    for point in usageDataPoints:
        if influxVersion == 2:
            logger.debug('  {}'.format(point.to_line_protocol()))
        elif influxVersion == VICTORIA_METRICS_VERSION:
            logger.debug('  {}'.format(json.dumps(point)))
        else:
            logger.debug(f'  {pprint.pformat(point)}')
