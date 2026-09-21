"""Isolated, network-free disk metadata scanner; never reads database contents."""
import datetime as dt
import json
import os
import shutil
import signal
import threading
import time


def measure(path):
    result = {'checked_at': dt.datetime.now(dt.UTC).isoformat(), 'status': 'ok'}
    try:
        usage = shutil.disk_usage(path)
        result['filesystem'] = {'total': usage.total, 'used': usage.used, 'free': usage.free,
                                'scope': 'filesystem containing the configured InfluxDB mount'}
        result['warning'] = usage.free / usage.total < .15
        started = time.monotonic()
        size, count, complete = 0, 0, True

        def scan_error(error):
            raise error

        # Never follow symlinks out of the database directory.
        for directory, _, files in os.walk(path, followlinks=False, onerror=scan_error):
            for name in files:
                try:
                    info = os.stat(os.path.join(directory, name), follow_symlinks=False)
                except FileNotFoundError:
                    continue  # Influx compaction may remove a file during the scan.
                size += info.st_blocks * 512
                count += 1
                if count >= 100000 or time.monotonic() - started > 2:
                    complete = False
                    break
            if not complete or time.monotonic() - started > 2:
                complete = False
                break
        result.update(database_bytes=size, database_scan_complete=complete, files_scanned=count)
        if not complete:
            result['status'] = 'partial'
    except OSError as error:
        result.update(status='partial', filesystem_error=type(error).__name__, database_scan_complete=False)
    return result


def read_snapshot(path):
    with open(path) as source:
        payload = source.read(16385)
    if len(payload) > 16384:
        raise ValueError('Oversized storage snapshot')
    result = json.loads(payload)
    age = (dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(result['checked_at'])).total_seconds()
    if not 0 <= age <= 900:
        raise ValueError('Storage snapshot is stale')
    return result


def main():
    # Fixed paths: Compose mounts only the Influx volume and statistics output.
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        result = measure('/influx-storage')
        temporary = '/stats/disk.json.tmp'
        with open(temporary, 'w') as output:
            json.dump(result, output)
        os.chmod(temporary, 0o644)
        os.replace(temporary, '/stats/disk.json')
        print('Storage scan: ' + result['status'], flush=True)
        stop.wait(300)


if __name__ == '__main__':
    main()
