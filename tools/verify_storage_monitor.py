"""Local-only validation of monitor isolation and measured disk metadata."""
import json
import subprocess
import sys


def docker(*args):
    return subprocess.check_output(['docker', '--context', 'orbstack', *args], text=True)


def main():
    project = sys.argv[1] if len(sys.argv) > 1 else 'vuegraf-admin-test'
    if project not in ('vuegraf-admin-test', 'vuegraf-integration'):
        raise SystemExit('Only known local projects are allowed')
    monitor = project + '-storage-monitor-1'
    collector = project + ('-admin-1' if project == 'vuegraf-admin-test' else '-vuegraf-1')
    inspect = json.loads(docker('inspect', monitor))[0]
    host = inspect['HostConfig']
    assert host['NetworkMode'] == 'none'
    assert host['ReadonlyRootfs'] and not host['Privileged']
    assert host['CapDrop'] == ['ALL']
    assert [cap.removeprefix('CAP_') for cap in host['CapAdd']] == ['DAC_READ_SEARCH']
    assert 'no-new-privileges:true' in host['SecurityOpt']
    mounts = {m['Destination']: m for m in inspect['Mounts']}
    assert set(mounts) == {'/influx-storage', '/stats'}
    assert mounts['/influx-storage']['RW'] is False
    assert all(m['Type'] == 'volume' for m in mounts.values())
    code = "from vuegraf.disk_stats import read_snapshot; import json; print(json.dumps(read_snapshot('/stats/disk.json')))"
    snapshot = json.loads(docker('exec', monitor, 'python', '-c', code))
    assert snapshot['database_scan_complete'] is True
    assert snapshot['database_bytes'] > 0 and snapshot['files_scanned'] > 0
    assert snapshot['filesystem']['free'] > 0
    app = json.loads(docker('inspect', collector))[0]
    assert app['Config']['User'] == '1012:1012'
    assert all(m['Name'] != mounts['/influx-storage']['Name'] for m in app['Mounts'] if m['Type'] == 'volume')
    stats = next(m for m in app['Mounts'] if m['Destination'] == '/storage-stats')
    assert stats['RW'] is False
    print(f"PASS: {project}: isolated read-only Influx mount; unprivileged collector sees statistics only; "
          f"{snapshot['database_bytes']} bytes across {snapshot['files_scanned']} files")


if __name__ == '__main__':
    main()
