"""pytest --modal run inside a Modal sandbox starts its shards from a snapshot of that sandbox's disk."""

from pathlib import Path

import modal
import pytest
from modal.config import config

REPO = Path(__file__).parents[2]
SETUP = """
set -e
mkdir -p /work && cd /work
echo built > built.txt
echo "$MODAL_SANDBOX_ID" > runner
cat > test_a.py <<'EOF'
def test_reads_what_the_runner_built():
    assert open("/work/built.txt").read().strip() == "built"
EOF
cat > test_b.py <<'EOF'
import os
# Imported by the controller too, which by then has taken its token out of its environment.
assert "MODAL_TOKEN_SECRET" not in os.environ
def test_runs_in_another_sandbox():
    assert os.environ["MODAL_SANDBOX_ID"] != open("/work/runner").read().strip()
def test_has_no_modal_credentials():
    assert "MODAL_TOKEN_SECRET" not in os.environ
EOF
"""
# Imported only by the project's interpreter: at collection in the runner, then in a shard.
LISTED_IN_THE_RUNNER = """
cat > /work/test_c.py <<'EOF'
import os
assert "MODAL_TOKEN_SECRET" not in os.environ
def test_listed_without_modal_credentials(): pass
EOF
"""


@pytest.mark.parametrize("runtime", [None, "vm"])
def test_shards_start_from_the_runner_disk(app, runtime):
    image = (
        modal.Image.debian_slim(python_version="3.12")
        .pip_install("pytest", "modal")
        .add_local_dir(REPO, "/opt/src", copy=True, ignore=[".venv", ".git", "**/__pycache__", "**/.pytest_cache"])
        .run_commands("pip install /opt/src")
    )
    credentials = modal.Secret.from_dict(
        {"MODAL_TOKEN_ID": config["token_id"], "MODAL_TOKEN_SECRET": config["token_secret"]}
    )
    environment = {"MODAL_ENVIRONMENT": config.get("environment") or ""}
    runner = modal.Sandbox.create("sleep", "infinity", app=app, image=image, runtime=runtime, timeout=900)
    try:
        assert runner.exec("sh", "-c", SETUP).wait() == 0
        write = runner.exec("sh", "-c", "cat > /work/modal_conf.py")
        write.stdin.write(f"def pytest_modal_sandbox(config):\n    return {{'runtime': {runtime!r}}}\n")
        write.stdin.write_eof()
        write.stdin.drain()
        assert write.wait() == 0
        run = runner.exec(
            "python", "-m", "pytest", "--modal", "-v", "--modal-shards=2", workdir="/work",
            secrets=[credentials], env=environment,
        )  # fmt: skip
        output = run.stdout.read() + run.stderr.read()
        assert run.wait() == 0, output
        assert "3 passed" in output and "modal: 2 shards" in output, output
        assert runner.exec("sh", "-c", LISTED_IN_THE_RUNNER).wait() == 0
        run = runner.exec(
            "python", "-m", "pytest", "--modal", "--modal-collect=remote", "--modal-shards=2", workdir="/work",
            secrets=[credentials], env=environment,
        )  # fmt: skip
        output = run.stdout.read() + run.stderr.read()
        assert run.wait() == 0, output
        assert "collected 4 items in pid" in output and "4 passed" in output, output
    finally:
        runner.terminate()


def test_shards_start_from_a_sandbox_that_is_given_no_token(app, on_modal):
    """The controller runs here, outside the sandbox that holds the project, and snapshots it by id."""
    image = modal.Image.debian_slim(python_version="3.12").pip_install(f"pytest=={pytest.__version__}")
    runner = modal.Sandbox.create("sleep", "infinity", app=app, image=image, timeout=900)
    try:
        assert runner.exec("sh", "-c", SETUP).wait() == 0
        result = on_modal(f"--modal-from={runner.object_id}", "--modal-workdir=/work", "--modal-shards=2")
        result.assert_outcomes(passed=3)
        result.stdout.fnmatch_lines(["collected 3 items in sb-*", "modal: 2 shards*"])
    finally:
        runner.terminate()
