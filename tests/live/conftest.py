import os
from pathlib import PurePosixPath

import modal
import pytest

MODAL_CONFIG = os.path.expanduser("~/.modal.toml")
# Where the tests that build a project inside a sandbox put it.
RUNNER_PROJECT = PurePosixPath("/pytest-modal-live-project")


def pytest_collection_modifyitems(items):
    if os.environ.get("PYTEST_MODAL_LIVE"):
        return
    skip = pytest.mark.skip(reason="creates Modal sandboxes; set PYTEST_MODAL_LIVE=1")
    for item in items:
        if "live" in item.path.parts:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def modal_config(monkeypatch):
    """pytester points HOME at a temporary directory, where the Modal client finds no profile."""
    monkeypatch.setenv("MODAL_CONFIG_PATH", os.environ.get("MODAL_CONFIG_PATH", MODAL_CONFIG))


@pytest.fixture(autouse=True)
def no_weights_left(request):
    """A run keeps its weights on Modal under the name of its root directory; these tests' are removed."""
    names = {RUNNER_PROJECT.name}
    if "pytester" in request.fixturenames:
        names.add(request.getfixturevalue("pytester").path.name)
    yield
    store = modal.Dict.from_name("pytest-modal", create_if_missing=True)
    for name in names:
        store.pop(name, None)


@pytest.fixture
def on_modal(pytester):
    def run(*args):
        return pytester.runpytest_subprocess("--modal", *args)

    return run


@pytest.fixture
def app():
    """An app for the sandboxes a test creates itself; it stops, and they with it, when the test ends."""
    with modal.App("pytest-modal-live").run() as running:
        yield running
