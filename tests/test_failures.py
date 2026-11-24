import os
import signal
import subprocess
import sys
import time

import pytest

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
