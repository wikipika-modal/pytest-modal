import pytest

pytest_plugins = ["pytester"]


@pytest.fixture
def sharded(pytester):
    def run(*args):
        return pytester.runpytest_subprocess("--modal", "--modal-backend=local", *args)

    return run
