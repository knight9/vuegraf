"""Offline regression tests: no Emporia or production database access."""
import datetime as dt
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pyemvue.device import VueDevice, VueDeviceChannelUsage
from pyemvue.enums import Scale

from vuegraf import telemetry, telemetry_recovery as recovery
from vuegraf.destination import writeDataPoints
from requests import HTTPError, Response

NOW = dt.datetime(2026, 9, 19, 12, tzinfo=dt.UTC)


def setup(tmp_path):
    cfg = {'telemetry': {'enabled': True, 'metrics': ['energy'], 'recovery': {
        'enabled': True, 'statePath': str(tmp_path / 'coverage.sqlite3'),
        'initialLookbackSecs': 60, 'pauseSecs': 0}},
        'timezone': 'America/Los_Angeles', 'influxDb': {'version': 2, 'bucket': 'test'},
        'args': SimpleNamespace(dryrun=False, resetdatabase=False)}
    device = VueDevice(gid=42)
    device.device_name = 'Example'
    channel = VueDeviceChannelUsage(gid=42, channelNum='1', usage=1)
    account = {'name': 'Test', 'vue': MagicMock(), 'deviceIdMap': {42: device},
               'channelIdMap': {'42-1': channel}}
    recovery.initialize(cfg)
    return cfg, account, channel


def points(account, channel, stamp):
    result = []
    telemetry.appendSample(account, channel, 'energy', 1, stamp, 60, 'False', result)
    return result


def run(cfg, account, now=NOW):
    recovery.recover(cfg, [account], now, False, threading.Event())


def test_slow_cycle_fills_intervening_minutes(tmp_path):
    cfg, account, channel = setup(tmp_path)
    store = cfg['_telemetryRecovery']
    store.record(points(account, channel, NOW - dt.timedelta(minutes=1)))
    run(cfg, account)  # Establish coverage origin, without a chart request.
    account['vue'].get_chart_usage.assert_not_called()
    later = NOW + dt.timedelta(minutes=5)
    store.record(points(account, channel, later))
    account['vue'].get_chart_usage.return_value = ([1] * 5, NOW)
    with patch('vuegraf.influx.writeInfluxPoints') as write:
        run(cfg, account, later)
    call = account['vue'].get_chart_usage.call_args
    assert call.args[1:3] == (NOW, later)
    assert {p.timestamp for p in write.call_args.args[1]} == {
        NOW + dt.timedelta(minutes=i) for i in range(5)}
    run(cfg, account, later)
    assert account['vue'].get_chart_usage.call_count == 1


def test_restart_keeps_internal_holes_not_just_latest_timestamp(tmp_path):
    cfg, account, channel = setup(tmp_path)
    store = cfg['_telemetryRecovery']
    store.record(points(account, channel, NOW - dt.timedelta(minutes=1)))
    run(cfg, account)
    store.record(points(account, channel, NOW + dt.timedelta(minutes=2)))
    store.close()
    recovery.initialize(cfg)
    account['vue'].get_chart_usage.return_value = ([1, 1], NOW)
    with patch('vuegraf.influx.writeInfluxPoints'):
        run(cfg, account, NOW + dt.timedelta(minutes=3))
    assert account['vue'].get_chart_usage.call_args.args[1:3] == (NOW, NOW + dt.timedelta(minutes=2))


def test_failed_write_and_dryrun_do_not_record_coverage(tmp_path):
    cfg, account, channel = setup(tmp_path)
    batch = points(account, channel, NOW - dt.timedelta(minutes=1))
    with patch('vuegraf.influx.writeInfluxPoints', side_effect=RuntimeError('private')):
        with pytest.raises(RuntimeError):
            writeDataPoints(cfg, batch)
    assert cfg['_telemetryRecovery'].db.execute('select count(*) from coverage').fetchone()[0] == 0
    cfg['args'].dryrun = True
    with patch('vuegraf.influx.writeInfluxPoints'):
        writeDataPoints(cfg, batch)
    assert cfg['_telemetryRecovery'].db.execute('select count(*) from coverage').fetchone()[0] == 0


def test_partial_response_retries_hole_with_backoff_but_collects_newer_data(tmp_path):
    cfg, account, channel = setup(tmp_path)
    cfg['_telemetryRecovery'].record(points(account, channel, NOW - dt.timedelta(minutes=1)))
    run(cfg, account)
    account['vue'].get_chart_usage.return_value = ([1, None, 1], NOW)
    later = NOW + dt.timedelta(minutes=3)
    with patch('vuegraf.influx.writeInfluxPoints'):
        run(cfg, account, later)
    run(cfg, account, later)
    assert account['vue'].get_chart_usage.call_count == 1
    account['vue'].get_chart_usage.return_value = ([1], later)
    with patch('vuegraf.influx.writeInfluxPoints'):
        run(cfg, account, later + dt.timedelta(minutes=1))
    assert account['vue'].get_chart_usage.call_args.args[1] == later
    # Retry the old hole once its backoff expires, not the already written points.
    account['vue'].get_chart_usage.return_value = ([1], NOW + dt.timedelta(minutes=1))
    with patch('vuegraf.influx.writeInfluxPoints'):
        run(cfg, account, later + dt.timedelta(minutes=5))
    assert account['vue'].get_chart_usage.call_args.args[1] == NOW + dt.timedelta(minutes=1)


def test_disabled_and_cancellation_are_noops(tmp_path):
    cfg, account, _ = setup(tmp_path)
    pause = threading.Event()
    pause.set()
    recovery.recover(cfg, [account], NOW, True, pause)
    recovery.recover({}, [account], NOW, True, threading.Event())
    account['vue'].get_chart_usage.assert_not_called()


@pytest.mark.parametrize('day,hours', [
    (dt.datetime(2026, 3, 8, 8, tzinfo=dt.UTC), 23),
    (dt.datetime(2026, 11, 1, 7, tzinfo=dt.UTC), 25)])
def test_day_coverage_uses_local_dst_boundary(tmp_path, day, hours):
    cfg, account, channel = setup(tmp_path)
    batch = []
    telemetry.appendSample(account, channel, 'energy', 1, day, hours * 3600, 'Day', batch)
    cfg['_telemetryRecovery'].record(batch)
    start, stop = cfg['_telemetryRecovery'].db.execute('select start, stop from coverage').fetchone()
    assert stop - start == hours * 3600


def test_scope_changes_when_database_changes(tmp_path):
    cfg, account, channel = setup(tmp_path)
    cfg['_telemetryRecovery'].record(points(account, channel, NOW))
    old = cfg['_telemetryRecovery'].scope
    cfg['_telemetryRecovery'].close()
    cfg['influxDb']['bucket'] = 'different'
    recovery.initialize(cfg)
    assert cfg['_telemetryRecovery'].scope != old


def test_retention_and_request_budget(tmp_path):
    cfg, account, _ = setup(tmp_path)
    cfg['telemetry']['recovery']['initialLookbackSecs'] = 7 * 86400
    cfg['telemetry']['recovery']['maxRequestsPerCycle'] = 1
    cfg['_telemetryRecovery'].close()
    recovery.initialize(cfg)
    account['vue'].get_chart_usage.return_value = ([], None)
    run(cfg, account)
    call = account['vue'].get_chart_usage.call_args
    assert call.args[1] >= NOW - dt.timedelta(days=7)
    assert call.args[2] - call.args[1] <= dt.timedelta(hours=12)
    assert call.kwargs['scale'] == Scale.MINUTE.value
    assert account['vue'].get_chart_usage.call_count == 1


def test_acknowledgement_required_from_both_databases(tmp_path):
    cfg, account, channel = setup(tmp_path)
    cfg['victoriaMetrics'] = {'url': 'http://example.invalid'}
    with patch('vuegraf.influx.writeInfluxPoints'), \
            patch('vuegraf.victoriametrics.writePoints', side_effect=RuntimeError('write failed')):
        with pytest.raises(RuntimeError):
            writeDataPoints(cfg, points(account, channel, NOW))
    assert cfg['_telemetryRecovery'].db.execute('select count(*) from coverage').fetchone()[0] == 0


def test_second_backlog_continues_without_new_detail_trigger(tmp_path):
    cfg, account, channel = setup(tmp_path)
    cfg.update(detailedDataEnabled=True, detailedDataHoursEnabled=False, detailedDataDaysEnabled=False)
    cfg['_telemetryRecovery'].record(points(account, channel, NOW - dt.timedelta(minutes=1)))
    account['vue'].get_chart_usage.return_value = ([], None)
    recovery.recover(cfg, [account], NOW, True, threading.Event())
    first = account['vue'].get_chart_usage.call_args
    assert first.kwargs['scale'] == Scale.SECOND.value
    # Restart: target and missing coverage survive, while backoff expires.
    cfg['_telemetryRecovery'].close()
    recovery.initialize(cfg)
    cfg['_telemetryRecovery'].record(points(account, channel, NOW + dt.timedelta(minutes=5)))
    with patch('vuegraf.influx.writeInfluxPoints'):
        run(cfg, account, NOW + dt.timedelta(minutes=5))
    seconds = [c for c in account['vue'].get_chart_usage.call_args_list if c.kwargs['scale'] == Scale.SECOND.value]
    assert len(seconds) == 2
    assert seconds[1].args[1:3] == first.args[1:3]


def test_rate_limit_stops_all_repairs_and_does_not_log_secrets(tmp_path):
    cfg, account, _ = setup(tmp_path)
    response = Response()
    response.status_code = 429
    account['vue'].get_chart_usage.side_effect = HTTPError('private-token', response=response)
    with patch.object(recovery.logger, 'warning') as warning:
        run(cfg, account)
        run(cfg, account, NOW + dt.timedelta(minutes=1))
    assert account['vue'].get_chart_usage.call_count == 1
    assert 'private-token' not in str(warning.call_args)
    assert warning.call_args.args[1:] == ('HTTPError', 429)
    assert cfg['_telemetryRecovery'].db.execute('select count(*) from coverage').fetchone()[0] == 0


def test_hourly_outage_repairs_all_missing_hours(tmp_path):
    cfg, account, channel = setup(tmp_path)
    cfg.update(detailedDataEnabled=True, detailedDataHoursEnabled=True,
               detailedDataDaysEnabled=False, detailedDataSecondsEnabled=False)
    batch = points(account, channel, NOW - dt.timedelta(minutes=1))
    telemetry.appendSample(account, channel, 'energy', 1, NOW - dt.timedelta(hours=1), 3600, 'Hour', batch)
    cfg['_telemetryRecovery'].record(batch)
    run(cfg, account)
    account['vue'].get_chart_usage.assert_not_called()
    batch = []
    telemetry.appendSample(account, channel, 'energy', 1, NOW + dt.timedelta(hours=3), 3600, 'Hour', batch)
    cfg['_telemetryRecovery'].record(batch)
    account['vue'].get_chart_usage.side_effect = lambda channel, start, stop, **kw: (
        [1] * int((stop - start).total_seconds() / (3600 if kw['scale'] == Scale.HOUR.value else 60)), start)
    with patch('vuegraf.influx.writeInfluxPoints'):
        run(cfg, account, NOW + dt.timedelta(hours=4))
    hours = [c for c in account['vue'].get_chart_usage.call_args_list if c.kwargs['scale'] == Scale.HOUR.value]
    assert len(hours) == 1
    assert hours[0].args[1:3] == (NOW, NOW + dt.timedelta(hours=3))


def test_expired_holes_counted_not_fabricated(tmp_path):
    cfg, account, channel = setup(tmp_path)
    account['vue'].get_chart_usage.return_value = ([], None)
    run(cfg, account)
    run(cfg, account, NOW + dt.timedelta(days=8))
    store = cfg['_telemetryRecovery']
    expired = store.db.execute('select expired_seconds from streams').fetchone()[0]
    assert expired == 86400 + 60
    assert store.db.execute('select count(*) from coverage').fetchone()[0] == 0
    assert account['vue'].get_chart_usage.call_args.args[1] == NOW + dt.timedelta(days=1)


def test_repeated_partial_retries_do_not_accumulate_duplicate_deferrals(tmp_path):
    cfg, account, channel = setup(tmp_path)
    store = cfg['_telemetryRecovery']
    key = store.key(account['name'], 42, '1', 'energy', Scale.MINUTE.value)
    start = int(NOW.timestamp())
    store.defer(key, start, start + 180, start)
    store.record(points(account, channel, NOW))
    for i in range(10):
        store.defer(key, start + 60, start + 180, start + i * 3600)
    assert store.db.execute('select count(*) from deferred').fetchone()[0] == 1
    assert store.db.execute('select attempts from deferred').fetchone()[0] == 11
    store.record(points(account, channel, NOW + dt.timedelta(minutes=1))
                 + points(account, channel, NOW + dt.timedelta(minutes=2)))
    assert store.db.execute('select count(*) from deferred').fetchone()[0] == 0


def test_configuration_validation_and_disabled_dryrun(tmp_path):
    cfg, _, _ = setup(tmp_path)
    cfg['_telemetryRecovery'].close()
    del cfg['_telemetryRecovery']
    cfg['args'].dryrun = True
    recovery.initialize(cfg)
    assert '_telemetryRecovery' not in cfg
    cfg['args'].dryrun = False
    cfg['args'].resetdatabase = True
    with pytest.raises(ValueError, match='resetting'):
        recovery.initialize(cfg)
    cfg['args'].resetdatabase = False
    cfg['telemetry']['recovery']['maxRequestsPerCycle'] = 0
    with pytest.raises(ValueError, match='Out-of-range'):
        recovery.initialize(cfg)


def test_coverage_requires_derived_fields_and_compacts_contiguous_samples(tmp_path):
    cfg, account, channel = setup(tmp_path)
    store = cfg['_telemetryRecovery']
    store.record(points(account, channel, NOW)[:1])
    assert store.db.execute('select count(*) from coverage').fetchone()[0] == 0
    batch = []
    for second in range(3600):
        telemetry.appendSample(account, channel, 'energy', 1, NOW + dt.timedelta(seconds=second), 1, 'True', batch)
    store.record(batch)
    assert store.db.execute('select count(*) from coverage').fetchone()[0] == 1
    assert store.db.execute('select stop-start from coverage').fetchone()[0] == 3600


@pytest.mark.parametrize('day,hours', [
    (dt.datetime(2026, 3, 8, 8, tzinfo=dt.UTC), 23),
    (dt.datetime(2026, 11, 1, 7, tzinfo=dt.UTC), 25)])
def test_daily_recovery_fetches_complete_dst_day(tmp_path, day, hours):
    cfg, account, channel = setup(tmp_path)
    cfg.update(detailedDataEnabled=True, detailedDataHoursEnabled=False, detailedDataDaysEnabled=True,
               detailedDataSecondsEnabled=False)
    cfg['_telemetryRecovery'].record(points(account, channel, day - dt.timedelta(minutes=1)))
    account['vue'].get_chart_usage.return_value = ([], None)
    run(cfg, account, day)  # Register previous completed day, currently unavailable.
    account['vue'].get_chart_usage.reset_mock()

    def chart(channel, start, stop, scale, **kwargs):
        if scale == Scale.DAY.value:
            # The previous unavailable day is also retried; return both days.
            return [1, 1], start
        return [1] * int((stop - start).total_seconds() / 60), start

    account['vue'].get_chart_usage.side_effect = chart
    end = day + dt.timedelta(hours=hours)
    with patch('vuegraf.influx.writeInfluxPoints'):
        run(cfg, account, end)
        # Previous and current holes can require separate bounded passes.
        run(cfg, account, end)
    key = cfg['_telemetryRecovery'].key(account['name'], 42, '1', 'energy', Scale.DAY.value)
    assert cfg['_telemetryRecovery'].missing(
        key, int(day.timestamp()), int(end.timestamp()), int(end.timestamp()), False) == []


def test_failed_recovery_write_is_replayed_after_cooldown(tmp_path):
    cfg, account, _ = setup(tmp_path)
    account['vue'].get_chart_usage.side_effect = lambda channel, start, stop, **kw: (
        [1] * int((stop - start).total_seconds() / 60), start)
    with patch('vuegraf.influx.writeInfluxPoints', side_effect=RuntimeError('write failed')):
        run(cfg, account)
    assert cfg['_telemetryRecovery'].db.execute('select count(*) from coverage').fetchone()[0] == 0
    first = account['vue'].get_chart_usage.call_args.args[1]
    with patch('vuegraf.influx.writeInfluxPoints'):
        run(cfg, account, NOW + dt.timedelta(minutes=5))
    assert account['vue'].get_chart_usage.call_args.args[1] == first
    assert cfg['_telemetryRecovery'].db.execute('select count(*) from coverage').fetchone()[0] == 1


def test_influx_v1_false_acknowledgement_is_failure(tmp_path):
    from vuegraf.influx import writeInfluxPoints
    cfg, account, channel = setup(tmp_path)
    cfg['influxDb']['version'] = 1
    cfg['influx'] = MagicMock()
    cfg['influx'].write_points.return_value = False
    cfg['args'].debug = False
    with pytest.raises(RuntimeError, match='acknowledge'):
        writeInfluxPoints(cfg, points(account, channel, NOW))


def test_multiple_minute_holes_are_coalesced_without_crossing_backoff(tmp_path):
    cfg, account, channel = setup(tmp_path)
    store = cfg['_telemetryRecovery']
    store.record(points(account, channel, NOW - dt.timedelta(minutes=1)))
    run(cfg, account)
    store.record(points(account, channel, NOW + dt.timedelta(minutes=1))
                 + points(account, channel, NOW + dt.timedelta(minutes=3)))
    account['vue'].get_chart_usage.return_value = ([1] * 5, NOW)
    with patch('vuegraf.influx.writeInfluxPoints'):
        run(cfg, account, NOW + dt.timedelta(minutes=5))
    assert account['vue'].get_chart_usage.call_args.args[1:3] == (NOW, NOW + dt.timedelta(minutes=5))


def test_minute_work_is_not_starved_by_older_daily_backlog(tmp_path):
    cfg, account, _ = setup(tmp_path)
    cfg.update(detailedDataEnabled=True, detailedDataDaysEnabled=True, detailedDataHoursEnabled=True,
               detailedDataSecondsEnabled=False)
    cfg['_telemetryRecovery'].settings['maxRequestsPerCycle'] = 1
    account['vue'].get_chart_usage.return_value = ([], None)
    run(cfg, account)
    assert account['vue'].get_chart_usage.call_args.kwargs['scale'] == Scale.MINUTE.value
