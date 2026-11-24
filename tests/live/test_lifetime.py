"""What happens to sandboxes when one is lost, and when the controller dies without cleaning up."""

import os
import re
import signal
import subprocess
import sys
import time
import uuid

import modal
import pytest

SLOW = """
import time
def test_slow(): time.sleep(600)
def test_after(): pass
"""


@pytest.fixture
def run(pytester):
    tag = {"pytest-modal-live": uuid.uuid4().hex}
    pytester.makepyfile(modal_conf=f"def pytest_modal_sandbox(config):\n    return {{'tags': {tag!r}}}\n", test_x=SLOW)
    controller = pytester.popen(
        [sys.executable, "-m", "pytest", "--modal", "-v", "-p", "no:cacheprovider"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    seen = ""
    # Verbose output names a test when it starts and ends the line only when it finishes.
    while "test_x.py::test_slow" not in seen:
        chunk = os.read(controller.stdout.fileno(), 4096).decode()
        assert chunk, seen
        seen += chunk
    # The controller names its app before it starts a shard; sandboxes are listed within it.
    app = re.search(r"modal: app (ap-\w+)", seen).group(1)
    yield controller, {"app_id": app, "tags": tag}, seen
    controller.kill()
    for sandbox in modal.Sandbox.list(app_id=app, tags=tag):
        sandbox.terminate()


def test_a_lost_sandbox_fails_its_test_and_the_rest_run_elsewhere(run):
    controller, mine, seen = run
    # Found by the tags every shard carries, next to the one this project's hook adds.
    tags = {**mine["tags"], "pytest-modal": "pytest-modal", "pytest-modal-shard": "modal0"}
    lost = list(modal.Sandbox.list(app_id=mine["app_id"], tags=tags))
    assert len(lost) == 1
    lost[0].terminate()
    output = seen + controller.stdout.read().decode()
    assert controller.wait(300) == 1, output
    assert "while running this test" in output and "1 failed, 1 passed" in output and "1 restarts" in output, output


def test_a_killed_controller_leaves_no_sandboxes(run):
    controller, mine, _ = run
    assert list(modal.Sandbox.list(**mine))
    controller.send_signal(signal.SIGKILL)
    deadline = time.monotonic() + 400
    while list(modal.Sandbox.list(**mine)):
        assert time.monotonic() < deadline, "sandboxes outlived a killed controller"
        time.sleep(10)
