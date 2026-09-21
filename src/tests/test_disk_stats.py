import datetime as dt
import json
from unittest.mock import patch

import pytest

from vuegraf.disk_stats import measure, read_snapshot
from vuegraf.storage import Storage


def test_counts_blocks_without_reading_contents_or_following_links(tmp_path):
    directory = tmp_path / 'data'
    directory.mkdir()
    record = directory / 'private'
    record.write_bytes(b'x' * 4096)
    record.chmod(0)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'unrelated').write_bytes(b'x' * 8192)
    (directory / 'link').symlink_to(outside, target_is_directory=True)
    result = measure(directory)
    assert result['database_scan_complete']
    assert result['database_bytes'] == record.stat().st_blocks * 512
    assert result['files_scanned'] == 1


def test_stale_snapshot_rejected(tmp_path):
    snapshot = tmp_path / 'stats.json'
    snapshot.write_text(json.dumps({'checked_at': (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()}))
    with pytest.raises(ValueError, match='stale'):
        read_snapshot(snapshot)


@patch('vuegraf.storage.client_for')
def test_storage_consumes_snapshot_without_access_to_database(client, tmp_path):
    snapshot = tmp_path / 'stats.json'
    snapshot.write_text(json.dumps(measure(tmp_path)))
    with patch('vuegraf.storage.measure', side_effect=AssertionError('must not scan database')):
        result = Storage({'admin': {'storageSnapshotPath': str(snapshot)},
                          'influxDb': {'bucket': 'test'}}).refresh()
    assert result['database_scan_complete']
    assert result['filesystem']['free'] > 0


@patch('vuegraf.disk_stats.os.walk', side_effect=PermissionError())
@patch('vuegraf.disk_stats.shutil.disk_usage')
def test_low_space_warning_survives_scan_error(usage, walk):
    usage.return_value.total = 100
    usage.return_value.used = 99
    usage.return_value.free = 1
    result = measure('/test')
    assert result['warning']
    assert result['status'] == 'partial'
