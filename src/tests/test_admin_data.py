import pytest
from vuegraf.admin_data import export_query

CONFIG = {'influxDb': {'bucket': 'telemetry', 'telemetryBuckets': {'second': 'short'}}}
REQUEST = {'start': '2026-09-19T00:00:00Z', 'stop': '2026-09-19T00:01:00Z'}


def test_filters_are_quoted_and_resolution_routes():
    query, _ = export_query(CONFIG, {**REQUEST, 'resolution': 'second', 'channel': '1" or true //'})
    assert 'from(bucket: "short")' in query
    assert '1\\" or true //' in query
    assert '== "True"' in query


@pytest.mark.parametrize('change', [
    {'field': 'bogus'}, {'resolution': 'bogus'}, {'aggregate': 'sum'},
    {'window': '1s', 'aggregate': 'mean'}, {'start': '2026-09-19T00:00:00'},
    {'stop': '2030-01-01T00:00:00Z'}, {'flux': 'drop()'},
    {'resolution': 'day', 'aggregate': 'mean', 'window': '1h'},
])
def test_reject_invalid_queries(change):
    with pytest.raises((ValueError, KeyError)):
        export_query(CONFIG, {**REQUEST, **change})


def test_energy_sum_allowed():
    query, _ = export_query(CONFIG, {**REQUEST, 'field': 'energy_kwh', 'aggregate': 'sum', 'window': '1h'})
    assert 'fn: sum' in query


def test_tempfile_failure_releases_export_slot(monkeypatch):
    from vuegraf import admin_data
    exports = admin_data.Exports(CONFIG)

    def fail(**kwargs):
        raise OSError('disk unavailable')

    monkeypatch.setattr(admin_data.tempfile, 'SpooledTemporaryFile', fail)
    for _ in range(3):
        with pytest.raises(OSError):
            exports.create(REQUEST)


def test_query_failure_closes_file_and_releases_slot(monkeypatch):
    import io
    from vuegraf import admin_data
    exports = admin_data.Exports(CONFIG)
    output = io.BytesIO()
    monkeypatch.setattr(admin_data.tempfile, 'SpooledTemporaryFile', lambda **kwargs: output)

    def fail(config):
        raise TimeoutError('query timed out')

    monkeypatch.setattr(admin_data, 'client_for', fail)
    with pytest.raises(TimeoutError):
        exports.create(REQUEST)
    assert output.closed
    assert exports.slots.acquire(blocking=False)
    assert exports.slots.acquire(blocking=False)
