"""Non-destructive copy/hash verification against the disposable admin-test DB."""
import datetime as dt

from influxdb_client import Point
from influxdb_client.client.write_api import SYNCHRONOUS

from manage_retention import migrate
from vuegraf.admin_data import client_for


config = {'influxDb': {'version': 2, 'url': 'http://influxdb:8086', 'org': 'admin-test',
                       'bucket': 'legacy-test', 'token': 'admin-test-only-token',
                       'telemetryBuckets': {'second': 'seconds-test', 'minute': 'minutes-test',
                                            'hour': 'coarse-test', 'day': 'coarse-test'}}}
start = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0) - dt.timedelta(minutes=10)
stop = start + dt.timedelta(minutes=1)
with client_for(config) as client:
    with client.write_api(write_options=SYNCHRONOUS) as writer:
        points = [Point('electrical_telemetry').tag('account_name', 'migration-test').tag('device_gid', '99')
                  .tag('channel_num', '1').tag('detailed', 'True').field('power_watts', float(index))
                  .field('channel_name', 'Fixture').time(start + dt.timedelta(seconds=index)) for index in range(60)]
        writer.write(bucket='legacy-test', record=points)
result = migrate(config, 'second', start, stop, apply=True)
assert result['verified_rows'] == 120, result
verified = migrate(config, 'second', start, stop, apply=False)
assert verified['verified_rows'] == 120
# Repeating a copy writes the same identities, rather than duplicating them.
assert migrate(config, 'second', start, stop, apply=True)['verified_rows'] == 120
# A post-cutover collector may repair destination-only points. They must not make
# verification fail, but every source identity and value must still be present.
with client_for(config) as client:
    with client.write_api(write_options=SYNCHRONOUS) as writer:
        writer.write(bucket='seconds-test', record=Point('electrical_telemetry')
                     .tag('account_name', 'migration-test').tag('device_gid', '99')
                     .tag('channel_num', '2').tag('detailed', 'True')
                     .field('power_watts', 999.0).field('channel_name', 'Target only')
                     .time(start + dt.timedelta(seconds=30)))
superset = migrate(config, 'second', start, stop, apply=False)
assert superset['verified_rows'] == 120
assert superset['target_extra_rows'] >= 2, superset
# A destination-only row must not mask a changed source identity.
with client_for(config) as client:
    with client.write_api(write_options=SYNCHRONOUS) as writer:
        writer.write(bucket='seconds-test', record=Point('electrical_telemetry')
                     .tag('account_name', 'migration-test').tag('device_gid', '99')
                     .tag('channel_num', '1').tag('detailed', 'True')
                     .field('power_watts', -1.0).time(start))
try:
    migrate(config, 'second', start, stop, apply=False)
    raise AssertionError('changed source identity was not detected')
except ValueError as error:
    assert 'Verification mismatch' in str(error), error
assert migrate(config, 'second', start, stop, apply=True)['verified_rows'] == 120
print('PASS: source preserved, 120 fields copied and verified, idempotent replay, destination supersets, and changed-value detection verified')
