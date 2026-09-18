# Copyright (c) Vuegraf contributors. MIT License.
"""Additional Emporia metrics, kept separate from legacy energy_usage records."""

import datetime
import logging
import math
import time
from dataclasses import dataclass

from pyemvue.enums import Scale, Unit
from requests import HTTPError

from vuegraf.device import lookupChannelName, lookupDeviceName


logger = logging.getLogger('vuegraf.telemetry')

# The last five units are Emporia's calculated cost/environmental equivalents,
# not additional electrical sensors. Preserve them only when explicitly selected.
METRICS = {
    'energy': (Unit.KWH.value, 'energy_kwh'),
    'current': (Unit.AMPHOURS.value, 'charge_ah'),
    'voltage': (Unit.VOLTS.value, 'voltage_volts'),
    'cost': (Unit.USD.value, 'cost_dollars'),
    'trees': (Unit.TREES.value, 'trees'),
    'gas': (Unit.GAS.value, 'gas_gallons'),
    'distance': (Unit.DRIVEN.value, 'distance_miles'),
    'carbon': (Unit.CARBON.value, 'carbon'),
}


@dataclass
class TelemetryPoint:
    accountName: str
    deviceName: str
    chanName: str
    deviceGid: int
    channelNum: str
    metric: str
    value: float
    timestamp: datetime.datetime
    detailed: str


def selectedMetrics(config):
    names = config.get('telemetry', {}).get('metrics', ['energy', 'current', 'voltage'])
    if names == ['all']:
        return list(METRICS)
    if not isinstance(names, list) or not names or any(name not in METRICS for name in names):
        raise ValueError('telemetry.metrics must be a nonempty list of metric names or ["all"]')
    return list(dict.fromkeys(names))


def validateConfig(config):
    section = config.get('telemetry', {})
    if not isinstance(section, dict):
        raise ValueError('telemetry must be an object')
    if section.get('enabled', False):
        selectedMetrics(config)


def unpackDevices(devices):
    """Flatten nested and top-level results without collecting a channel twice."""
    channels = {}
    for device in devices.values():
        for channel in device.channels.values():
            channels[(channel.device_gid, str(channel.channel_num))] = channel
            channels.update(unpackDevices(channel.nested_devices))
    return channels


def appendSample(account, channel, metric, value, timestamp, seconds, detail, points):
    # None means unavailable, not zero. Reject nonnumeric sentinels/NaN/infinity.
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        return False
    device = lookupDeviceName(account, channel.device_gid)
    name = lookupChannelName(account, channel)
    base = (account['name'], device, name, channel.device_gid, str(channel.channel_num))
    points.append(TelemetryPoint(*base, METRICS[metric][1], float(value), timestamp, detail))
    if metric == 'energy':
        points.append(TelemetryPoint(*base, 'power_watts', value * 3_600_000 / seconds, timestamp, detail))
    elif metric == 'current':
        points.append(TelemetryPoint(*base, 'current_amps', value * 3600 / seconds, timestamp, detail))
    return True


def fetchChart(config, account, channel, metric, start, stop, scale, detail, points):
    """Bound unsupported-channel retries; let authentication/rate-limit failures stop this cycle."""
    cache = account.setdefault('_telemetryUnavailable', {})
    key = (channel.device_gid, channel.channel_num, metric, scale)
    if cache.get(key, 0) > time.monotonic():
        return
    seconds = 1 if scale == Scale.SECOND.value else 60
    try:
        values, first = account['vue'].get_chart_usage(channel, start, stop, scale=scale, unit=METRICS[metric][0])
    except HTTPError as error:
        status = error.response.status_code if error.response is not None else None
        if status not in (400, 404, 422):
            raise
        cache[key] = time.monotonic() + 3600
        logger.info('Metric unavailable: channel=%s metric=%s scale=%s status=%s',
                    channel.channel_num, metric, scale, status)
        return
    found = False
    if first is not None:
        for index, value in enumerate(values):
            stamp = first + datetime.timedelta(seconds=index * seconds)
            if start <= stamp < stop:
                found = appendSample(account, channel, metric, value, stamp, seconds, detail, points) or found
    if not found:
        cache[key] = time.monotonic() + 3600


def collectTelemetry(config, account, stopTimeUTC, collectDetails, points, detailedStartTimeUTC,
                     powerUsages=None):
    """Minute snapshots plus hourly second-resolution batches for every observed channel.

    Discover channels from the UNION of units: current/voltage expose Mains_A/B
    and unmerged circuits which are absent from the power response.
    """
    if not config.get('telemetry', {}).get('enabled', False):
        return
    # Local import avoids a destination/telemetry import cycle.
    from vuegraf.destination import getTags

    metrics = selectedMetrics(config)
    _, secondTag, minuteTag, _, _ = getTags(config)
    stop = stopTimeUTC.replace(second=0, microsecond=0)
    channels = {}
    available = {}
    gids = list(account['deviceIdMap'])
    try:
        for metric in metrics:
            if metric == 'energy' and powerUsages is not None:
                usages = powerUsages
            else:
                usages = account['vue'].get_device_list_usage(
                    gids, stopTimeUTC, scale=Scale.MINUTE.value, unit=METRICS[metric][0], max_retry_attempts=1)
            metricChannels = unpackDevices(usages or {})
            channels.update(metricChannels)
            available[metric] = set(metricChannels)
            for channel in metricChannels.values():
                appendSample(account, channel, metric, channel.usage, stop, 60, minuteTag, points)

        # Fill omissions in the bulk response, notably per-leg mains power. Never
        # invent channel numbers: all identifiers came from a successful API response.
        for metric in metrics:
            for key in channels.keys() - available[metric]:
                fetchChart(config, account, channels[key], metric, stop - datetime.timedelta(minutes=1),
                           stop, Scale.MINUTE.value, minuteTag, points)

        if collectDetails and config.get('detailedDataSecondsEnabled', True):
            # Emporia's second-resolution retention is short. Bound restart recovery
            # to three hours and never query expensive second history every minute.
            detailStop = stopTimeUTC.replace(microsecond=0)
            start = max(account.get('_telemetrySecondStop') or detailedStartTimeUTC or
                        detailStop - datetime.timedelta(hours=1),
                        detailStop - datetime.timedelta(hours=3)).replace(microsecond=0)
            for channel in channels.values():
                if channel.channel_num in ('Balance', 'TotalUsage', 'MainsFromGrid', 'MainsToGrid'):
                    continue
                for metric in metrics:
                    fetchChart(config, account, channel, metric, start, detailStop, Scale.SECOND.value, secondTag, points)
            account['_telemetrySecondStop'] = detailStop
    except Exception as error:
        # HTTP error strings may contain request details; log only the type/status.
        status = getattr(getattr(error, 'response', None), 'status_code', None)
        logger.warning('Telemetry cycle incomplete (%s, status=%s); will retry next cycle', type(error).__name__, status)
