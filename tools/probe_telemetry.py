"""Read-only Emporia metric probe. Credentials are prompted, never saved."""

import datetime
import getpass
import json
from collections import Counter

from pyemvue import PyEmVue
from pyemvue.enums import Scale, Unit

from vuegraf.device import populateDevices
from vuegraf.telemetry import collectTelemetry


def main():
    vue = PyEmVue()
    vue.login(username=input('Emporia email: '), password=getpass.getpass('Emporia password: '))
    account = {'name': 'Probe', 'vue': vue}
    populateDevices(account)
    stop = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=2)
    config = {'telemetry': {'enabled': True}, 'influxDb': {'version': 2}}
    points = []
    collectTelemetry(config, account, stop, False, points, None)
    print(json.dumps({'points_by_metric': dict(Counter(point.metric for point in points))}))
    for point in points:
        if point.channelNum.startswith('Mains_'):
            print(json.dumps({'channel': point.channelNum, 'metric': point.metric, 'value': point.value}))
    # A short, bounded chart probe confirms high-resolution units without a full
    # all-channel backfill or any database writes.
    from pyemvue.device import VueDeviceChannel
    discovered = {(point.deviceGid, point.channelNum) for point in points if point.channelNum.startswith('Mains_')}
    for gid, channel in sorted(discovered):
        for unit in (Unit.KWH, Unit.AMPHOURS, Unit.VOLTS):
            values, first = vue.get_chart_usage(VueDeviceChannel(gid=gid, channelNum=channel),
                                                stop - datetime.timedelta(seconds=5), stop,
                                                scale=Scale.SECOND.value, unit=unit.value)
            print(json.dumps({'channel': channel, 'unit': unit.value, 'second_samples': values}))


if __name__ == '__main__':
    main()
