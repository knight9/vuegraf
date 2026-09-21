"""Local-only fake Emporia fixture with real, disposable InfluxDB and admin UI."""
import signal
import threading
import traceback
from types import SimpleNamespace

from pyemvue.device import VueDevice, VueDeviceChannelUsage

from vuegraf import admin_runtime
from vuegraf.admin_web import Server
from vuegraf.storage import provision


class FakeEmporia:
    def get_device_list_usage(self, gids, instant, scale, unit, **kwargs):
        channel = VueDeviceChannelUsage(gid=42, channelNum='1', usage=0.001)
        return {42: SimpleNamespace(channels={'1': channel})}

    def get_chart_usage(self, channel, start, stop, scale, unit):
        step = {'1S': 1, '1MIN': 60, '1H': 3600, '1D': 86400}[scale]
        return [0.001] * int((stop - start).total_seconds() / step), start


def main():
    device = VueDevice(gid=42)
    device.device_name = 'Test Panel'
    channel = VueDeviceChannelUsage(gid=42, channelNum='1', usage=.001)
    config = {
        'args': SimpleNamespace(debug=False, dryrun=False, resetdatabase=False, historydays=0),
        'influxDb': {'version': 2, 'url': 'http://influxdb:8086', 'org': 'admin-test',
                     'bucket': 'legacy-test', 'token': 'admin-test-only-token',
                     'telemetryBuckets': {'second': 'seconds-test', 'minute': 'minutes-test',
                                          'hour': 'coarse-test', 'day': 'coarse-test'}},
        'timezone': 'UTC', 'addStationField': True, 'legacyEnergyEnabled': False,
        'updateIntervalSecs': 60, 'detailedDataEnabled': True, 'detailedIntervalSecs': 3600,
        'detailedDataSecondsEnabled': True, 'detailedDataHoursEnabled': True, 'detailedDataDaysEnabled': True,
        'telemetry': {'enabled': True, 'metrics': ['energy', 'current', 'voltage'], 'recovery': {
            'enabled': True, 'statePath': '/opt/vuegraf/state/coverage.sqlite3', 'initialLookbackSecs': 60}},
        'accounts': [{'name': 'Test', 'vue': FakeEmporia(), 'deviceIdMap': {42: device},
                      'channelIdMap': {'42-1': channel}}],
        'admin': {'storageSnapshotPath': '/storage-stats/disk.json'}
    }
    provision(config, apply=True)
    admin_runtime.initDeviceAccount = lambda config, account: None
    runtime = admin_runtime.Runtime(config)
    original_initialize = runtime.initialize

    def initialize():
        try:
            original_initialize()
        except Exception:
            traceback.print_exc()  # This fixture contains dummy credentials/data only.
            raise

    runtime.initialize = initialize
    server = Server(('0.0.0.0', 8080), runtime.controller, config)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    runtime.start()
    web = threading.Thread(target=server.serve_forever, daemon=True)
    web.start()
    print('Fake-source admin fixture ready; no Emporia credentials or production access', flush=True)
    stop.wait()
    server.shutdown()
    server.server_close()
    runtime.stop()


if __name__ == '__main__':
    main()
