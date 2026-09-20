"""Offline-source integration probe for a disposable InfluxDB test container.

Never uses Emporia credentials. The database and bucket must be disposable.
Run phase 1 and phase 2 in separate collector containers sharing only the state
volume, to prove recovery survives a process/container restart.
"""
import datetime as dt
import os
import sys
import threading
from types import SimpleNamespace

from influxdb_client import InfluxDBClient
from pyemvue.device import VueDevice, VueDeviceChannelUsage
from pyemvue.enums import Scale

from vuegraf.destination import closeConnection, initConnection, writeDataPoints
from vuegraf.telemetry import appendSample
from vuegraf.telemetry_recovery import recover

NOW = dt.datetime(2026, 9, 19, 12, tzinfo=dt.UTC)


class FakeEmporia:
    def __init__(self):
        self.requests = []

    def get_chart_usage(self, channel, start, stop, scale, unit):
        assert scale == Scale.MINUTE.value
        self.requests.append((start, stop))
        return [0.001] * int((stop - start).total_seconds() / 60), start


def main():
    phase = int(sys.argv[1])
    assert phase in (1, 2)
    # Fixed test-only destination: no production URL/config override.
    cfg = {
        'args': SimpleNamespace(dryrun=False, debug=False, resetdatabase=False),
        'influxDb': {'version': 2, 'url': 'http://vuegraf-recovery-influx-test:8086',
                     'org': 'recovery-test', 'bucket': 'recovery-test',
                     'token': os.environ['RECOVERY_TEST_TOKEN']},
        'telemetry': {'enabled': True, 'metrics': ['energy'], 'recovery': {
            'enabled': True, 'statePath': '/opt/vuegraf/state/coverage.sqlite3',
            'initialLookbackSecs': 60, 'pauseSecs': 0}},
        'timezone': 'UTC'
    }
    device = VueDevice(gid=42)
    device.device_name = 'Test Panel'
    channel = VueDeviceChannelUsage(gid=42, channelNum='1', usage=0.001)
    source = FakeEmporia()
    account = {'name': 'Test', 'vue': source, 'deviceIdMap': {42: device},
               'channelIdMap': {'42-1': channel}}
    initConnection(cfg)

    def write_snapshot(stamp):
        points = []
        appendSample(account, channel, 'energy', 0.001, stamp, 60, 'False', points)
        writeDataPoints(cfg, points)

    if phase == 1:
        write_snapshot(NOW - dt.timedelta(minutes=1))
        recover(cfg, [account], NOW, False, threading.Event())
        assert source.requests == []
        write_snapshot(NOW + dt.timedelta(minutes=5))
        recover(cfg, [account], NOW + dt.timedelta(minutes=5), False, threading.Event())
        assert source.requests == [(NOW, NOW + dt.timedelta(minutes=5))]
        count = 7
    else:
        write_snapshot(NOW + dt.timedelta(minutes=9))
        recover(cfg, [account], NOW + dt.timedelta(minutes=9), False, threading.Event())
        assert source.requests == [(NOW + dt.timedelta(minutes=6), NOW + dt.timedelta(minutes=9))]
        count = 11
    # A second pass must not refetch the covered interval.
    recover(cfg, [account], NOW + dt.timedelta(minutes=5 if phase == 1 else 9), False, threading.Event())
    assert len(source.requests) == 1
    with InfluxDBClient(url=cfg['influxDb']['url'], token=cfg['influxDb']['token'], org='recovery-test') as client:
        query = '''from(bucket: "recovery-test")
          |> range(start: 2026-09-19T11:59:00Z, stop: 2026-09-19T12:10:00Z)
          |> filter(fn: (r) => r._measurement == "electrical_telemetry" and r._field == "power_watts")
          |> sort(columns: ["_time"])'''
        records = [row for table in client.query_api().query(query) for row in table.records]
        assert len(records) == count, len(records)
        assert [r.get_time() for r in records] == [NOW + dt.timedelta(minutes=i - 1) for i in range(count)]
        assert all(abs(r.get_value() - 60) < 1e-8 for r in records)
    closeConnection(cfg)
    cfg['influx'].close()
    print(f'PASS phase {phase}: {count} contiguous minute readings in real InfluxDB; no duplicate fetch')


if __name__ == '__main__':
    main()
