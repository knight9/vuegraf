# Copyright (c) Jason Ertel (jertel).
# This file is part of the Vuegraf project and is made available under the MIT License.

# Contains logic relating to preparing, retrieving and saving InfluxDB data points.

import datetime
import influxdb         # InfluxDB v1
import influxdb_client  # InfluxDB v2
import logging
import pprint

from vuegraf.config import getConfigValue, getInfluxTag, getInfluxVersion
from vuegraf.time import calculateResumeTimeRange, getTimeNow


logger = logging.getLogger('vuegraf.influx')


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

    return calculateResumeTimeRange(config, timeStr, pointType, tagValue_second, tagValue_minute,
                                    startTime, stopTime, fillInMissingData)


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
        else:
            config['influx'].write_points(influxPoints, batch_size=5000)


def dumpPoints(config, label, usageDataPoints):
    influxVersion = getInfluxVersion(config)
    logger.debug(label)
    for point in usageDataPoints:
        if influxVersion == 2:
            logger.debug('  {}'.format(point.to_line_protocol()))
        else:
            logger.debug(f'  {pprint.pformat(point)}')
