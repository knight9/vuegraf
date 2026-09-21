import threading
import time

import pytest

from vuegraf.controller import Busy, Controller


def test_admission_is_a_mutex_and_failure_keeps_success():
    c = Controller()
    with pytest.raises(Busy):
        c.admit('second', 'manual')
    c.ready = True
    c.admit('second', 'manual')
    with pytest.raises(Busy):
        c.admit('minute', 'scheduled')
    c.finish('success')
    success = c.snapshot()['jobs']['second']['last_success']
    c.admit('second', 'manual')
    c.finish('failed')
    assert c.snapshot()['jobs']['second']['last_success'] == success
    assert c.snapshot()['active'] is None


def test_snapshot_is_detached():
    c = Controller()
    c.snapshot()['jobs']['minute']['state'] = 'running'
    assert c.jobs['minute']['state'] == 'idle'


def test_concurrent_admission_has_one_winner():
    c = Controller()
    c.ready = True
    winners = []

    def request():
        try:
            winners.append(c.admit('second', 'manual'))
        except Busy:
            pass

    threads = [threading.Thread(target=request) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1


def test_worker_survives_failed_job_and_runs_cleanup():
    c = Controller()
    cleaned = threading.Event()

    def execute(job):
        raise ValueError('secret must not be returned')

    thread = threading.Thread(target=c.serve, args=(lambda: None, execute, {'minute': 60}, cleaned.set))
    thread.start()
    deadline = time.monotonic() + 2
    while c.snapshot()['jobs']['minute']['last_result'] is None and time.monotonic() < deadline:
        time.sleep(.01)
    c.shutdown()
    thread.join(2)
    assert cleaned.is_set()
    assert c.snapshot()['jobs']['minute']['details'] == {'error_type': 'ValueError'}
