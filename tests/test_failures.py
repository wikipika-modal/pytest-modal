import os
import queue
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from pytest_modal.plugin import Controller, pump

COUNT_STARTS = """
import os
WORKER = "PYTEST_MODAL_SHARD" in os.environ
if WORKER:
    open("starts", "a").write("x")
"""


def starts(pytester):
    return len((pytester.path / "starts").read_text())


def test_a_test_that_kills_its_shard_fails_alone(pytester, sharded):
    pytester.makepyfile(
        """
        import os
        def test_first(): pass
        def test_dies(): os._exit(7)
        def test_after(): pass
        """
    )
    result = sharded()
    result.assert_outcomes(passed=2, failed=1)
    result.stdout.fnmatch_lines(["*exited 7 while running this test*", "*1 restarts*"])


def test_a_shard_that_dies_on_start_is_replaced(pytester, sharded):
    pytester.makeconftest(COUNT_STARTS + "if WORKER and len(open('starts').read()) == 1: os._exit(9)")
    pytester.makepyfile("def test_one(): pass")
    result = sharded()
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*1 restarts*"])


def test_a_shard_that_never_starts_gives_up(pytester, sharded):
    pytester.makeconftest(COUNT_STARTS + "if WORKER: os._exit(9)")
    pytester.makepyfile("def test_one(): pass")
    result = sharded("--modal-retries=1")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*exited 9 without running this test*"])
    assert starts(pytester) == 2


def test_a_test_the_shard_does_not_collect_is_an_error_and_not_retried(pytester, sharded):
    pytester.makeconftest(COUNT_STARTS)
    pytester.makepyfile(
        """
        import os
        def test_everywhere(): pass
        if "PYTEST_MODAL_SHARD" not in os.environ:
            def test_controller_only(): pass
        """
    )
    result = sharded()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*did not collect this test*"])
    assert starts(pytester) == 1


def test_a_file_only_the_shard_cannot_import_fails_alone(pytester, sharded):
    pytester.makepyfile(
        test_a="import os\nif 'PYTEST_MODAL_SHARD' in os.environ:\n    import nothing_by_this_name\ndef test_a(): pass",
        test_b="def test_b(): pass",
    )
    result = sharded("--modal-shards=1")
    result.assert_outcomes(passed=1, errors=2)
    result.stdout.fnmatch_lines(["*No module named 'nothing_by_this_name'*", "*did not collect this test*"])


@pytest.mark.parametrize("collect", ["local", "remote"])
def test_shards_on_another_pytest_version_are_a_usage_error(pytester, sharded, collect):
    pytester.makeconftest("import os, pytest\nif 'PYTEST_MODAL_SHARD' in os.environ: pytest.__version__ = '1.2.3'")
    pytester.makepyfile("def test_one(): pass")
    result = sharded(f"--modal-collect={collect}")
    result.stderr.fnmatch_lines(["*the shards run pytest 1.2.3 (*) and this process runs pytest *"])
    assert result.ret == pytest.ExitCode.USAGE_ERROR


def test_a_shard_that_hangs_on_start_is_replaced(pytester, sharded):
    pytester.makeconftest(COUNT_STARTS + "import time\nif WORKER and len(open('starts').read()) == 1: time.sleep(600)")
    pytester.makeini("[pytest]\nmodal_start_timeout = 2")
    pytester.makepyfile("def test_one(): pass")
    result = sharded()
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*1 restarts*"])


def test_a_shard_that_hangs_on_start_is_replaced_while_others_keep_reporting(pytester, sharded):
    """The busy shard reports several times a second until the other's replacement has started, so the
    controller never has a quiet moment in which to notice the one that hangs."""
    pytester.makeconftest(
        """
        import os, time
        if os.environ.get("PYTEST_MODAL_SHARD") == "1":
            open("starts", "a").write("x")
            if len(open("starts").read()) == 1:
                time.sleep(600)
        """
    )
    pytester.makeini("[pytest]\nmodal_start_timeout = 1")
    pytester.makepyfile(
        test_busy="""
            import os, time, pytest
            @pytest.mark.parametrize("n", range(200))
            def test_busy(n):
                if not os.path.exists("starts") or len(open("starts").read()) < 2:
                    time.sleep(0.3)
            def test_the_other_shard_was_replaced_by_now():
                assert len(open("starts").read()) == 2
        """,
        test_hangs="def test_hangs(): pass",
    )
    result = sharded("--modal-shards=2")
    result.assert_outcomes(passed=202)
    result.stdout.fnmatch_lines(["*1 restarts*"])


def test_replacing_shards_does_not_wait_for_each_to_stop():
    stopped = []

    def stop():
        time.sleep(0.5)
        stopped.append(1)

    def fails():
        raise RuntimeError("already gone")

    lanes = [SimpleNamespace(stop=stop, greeted=False, launched=0.0, tail=[]) for _ in range(8)]
    lanes.append(SimpleNamespace(stop=fails, greeted=False, launched=0.0, tail=[]))
    lanes.append(SimpleNamespace(stop=stop, greeted=True, launched=0.0, tail=[]))
    started = time.monotonic()
    Controller.replace_slow_starters(SimpleNamespace(live=lanes, patience=1.0))
    assert time.monotonic() - started < 0.4
    assert [lane.stop for lane in lanes[:9]] == [None] * 9 and lanes[9].stop is stop
    deadline = time.monotonic() + 10
    while len(stopped) < 8 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(stopped) == 8


def test_a_shard_that_fails_after_its_sandbox_started_stops_the_sandbox():
    stopped, events = [], queue.Queue()

    class Backend:
        def run(self, lane, args, emit):
            lane.stop = lambda: stopped.append(lane)
            raise ConnectionError("lost the sandbox's output")

    lane = SimpleNamespace(stop=None)
    pump(Backend(), lane, [], events)
    assert stopped == [lane]
    assert isinstance(events.get_nowait()[1], ConnectionError)


ROOM_FOR = """
import threading
from pytest_modal import backends

class NoRoom(Exception):
    pass

room = threading.Semaphore({room})
run = backends.Local.run

def limited(self, lane, args, emit):
    open("attempts", "a").write("x")
    if not room.acquire(blocking=False):
        raise NoRoom("Concurrent sandbox limit ({room}) exceeded")
    try:
        return run(self, lane, args, emit)
    finally:
        room.release()

backends.Local.run = limited
backends.Local.full = lambda self, outcome: isinstance(outcome, NoRoom)
"""


def test_shards_there_is_no_room_for_wait_for_others_to_finish(pytester, sharded):
    """Only the controller loads modal_conf.py, where the local backend is given room for two shards."""
    pytester.makepyfile(modal_conf=ROOM_FOR.format(room=2), **{f"test_{n}": "def test_one(): pass" for n in range(6)})
    result = sharded("--modal-shards=6", "--modal-retries=0")
    result.assert_outcomes(passed=6)
    result.stdout.fnmatch_lines(["modal: no room for another sandbox (Concurrent sandbox limit (2) exceeded)*"])
    assert result.stdout.str().count("no room for another sandbox") == 1


def test_with_no_room_and_nothing_to_wait_for_shards_fail_after_their_retries(pytester, sharded):
    pytester.makepyfile(modal_conf=ROOM_FOR.format(room=0), **{f"test_{n}": "def test_one(): pass" for n in range(4)})
    result = sharded("--modal-shards=4", "--modal-retries=1")
    result.assert_outcomes(errors=4)
    result.stdout.fnmatch_lines(["*Concurrent sandbox limit (0) exceeded*without running this test*"])
    assert len((pytester.path / "attempts").read_text()) <= 4 * 2


def test_sigterm_stops_the_shards(pytester):
    pytester.makepyfile(
        """
        import os, time
        def test_slow():
            open("pid", "w").write(str(os.getpid()))
            time.sleep(120)
        """
    )
    controller = pytester.popen(
        [sys.executable, "-m", "pytest", "--modal", "--modal-backend=local"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    pid_file = pytester.path / "pid"
    deadline = time.monotonic() + 30
    while not (pid_file.exists() and pid_file.read_text()) and time.monotonic() < deadline:
        time.sleep(0.1)
    shard = int(pid_file.read_text())
    controller.send_signal(signal.SIGTERM)
    assert controller.wait(30) != 0
    while time.monotonic() < deadline:
        try:
            os.kill(shard, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise AssertionError("the shard outlived its controller")
