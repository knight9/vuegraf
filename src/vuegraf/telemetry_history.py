"""Bounded historical and ongoing aggregate collection for all telemetry metrics."""

import datetime
import logging

from pyemvue.enums import Scale

from vuegraf.telemetry import METRICS, fetchChart, sampleWindow, selectedMetrics, unpackDevices


logger = logging.getLogger('vuegraf.telemetry_history')
EXCLUDED = ('Balance', 'TotalUsage', 'MainsFromGrid', 'MainsToGrid')


def discoverChannels(config, account, instant):
    channels = account.setdefault('_telemetryChannels', {})
    # Metadata retains known circuits even if a current usage response omits them.
    for channel in account.get('channelIdMap', {}).values():
        channels[(channel.device_gid, str(channel.channel_num))] = channel
    for metric in selectedMetrics(config):
        usages = account['vue'].get_device_list_usage(
            list(account['deviceIdMap']), instant, scale=Scale.MINUTE.value,
            unit=METRICS[metric][0], max_retry_attempts=1)
        channels.update(unpackDevices(usages or {}))
    return [channel for channel in channels.values() if channel.channel_num not in EXCLUDED]


def collectAggregate(config, account, instant, scale, points):
    """Use the same chart path for completed hourly/daily buckets and backfill."""
    if not config.get('telemetry', {}).get('enabled', False):
        return
    from vuegraf.destination import getTags

    start = instant.replace(minute=0, second=0, microsecond=0)
    start, seconds = sampleWindow(config, start, 0, scale)
    stop = start + datetime.timedelta(seconds=seconds)
    detail = getTags(config)[3 if scale == Scale.HOUR.value else 4]
    channels = account.get('_telemetryChannels')
    channels = list(channels.values()) if channels else discoverChannels(config, account, stop)
    for channel in channels:
        if channel.channel_num in EXCLUDED:
            continue
        for metric in selectedMetrics(config):
            fetchChart(config, account, channel, metric, start, stop, scale, detail, points, cacheEmpty=False)


def historyWindows(config, start, stop):
    """Prioritize short-lived detail, then long-term hour/day history.

    Second requests contain <=1 hour; minute requests <=12 hours. Retention
    limits apply only to fine detail, not the requested hourly/daily range.
    """
    from vuegraf.destination import getTags

    _, secondTag, minuteTag, hourTag, dayTag = getTags(config)
    resolutions = [(Scale.SECOND.value, secondTag, datetime.timedelta(hours=3), datetime.timedelta(hours=1)),
                   (Scale.MINUTE.value, minuteTag, datetime.timedelta(days=7), datetime.timedelta(hours=12)),
                   (Scale.HOUR.value, hourTag, None, datetime.timedelta(days=20)),
                   (Scale.DAY.value, dayTag, None, datetime.timedelta(days=20))]
    for scale, tag, retention, batch in resolutions:
        lower = max(start, stop - retention) if retention else start
        if scale == Scale.SECOND.value:
            lower = lower.replace(microsecond=0)
            upper = stop.replace(microsecond=0)
        elif scale == Scale.MINUTE.value:
            lower = lower.replace(second=0, microsecond=0)
            upper = stop.replace(second=0, microsecond=0)
        elif scale == Scale.HOUR.value:
            lower = lower.replace(minute=0, second=0, microsecond=0)
            upper = stop.replace(minute=0, second=0, microsecond=0)
        else:
            lower, _ = sampleWindow(config, lower, 0, scale)
            upper, _ = sampleWindow(config, stop, 0, scale)
        while lower < upper:
            end = min(lower + batch, upper)
            if scale == Scale.DAY.value:
                end, _ = sampleWindow(config, end, 0, scale)
            yield scale, tag, lower, end
            lower = end


def collectHistory(config, account, start, stop, pauseEvent):
    """Write one bounded channel/metric batch at a time, never accumulating days of seconds."""
    if not config.get('telemetry', {}).get('enabled', False) or pauseEvent.is_set():
        return
    from vuegraf.destination import writeDataPoints

    channels = discoverChannels(config, account, stop)
    for scale, detail, batchStart, batchStop in historyWindows(config, start, stop):
        logger.info('Backfilling telemetry: scale=%s start=%s stop=%s', scale, batchStart, batchStop)
        for channel in channels:
            for metric in selectedMetrics(config):
                if pauseEvent.is_set():
                    return
                points = []
                # Empty old periods must not suppress newer periods for a channel.
                fetchChart(config, account, channel, metric, batchStart, batchStop,
                           scale, detail, points, cacheEmpty=False)
                if points:
                    writeDataPoints(config, points)
                if pauseEvent.wait(0.2):
                    return
