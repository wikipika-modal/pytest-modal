import os

import modal
import pytest

MODAL_CONFIG = os.path.expanduser("~/.modal.toml")


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


@pytest.fixture
def on_modal(pytester):
    def run(*args):
        return pytester.runpytest_subprocess("--modal", *args)

    yield run
    # The weights a run keeps on Modal, under the name of this test's directory.
    modal.Dict.from_name("pytest-modal", create_if_missing=True).pop(pytester.path.name, None)


@pytest.fixture
def app():
    """An app for the sandboxes a test creates itself; it stops, and they with it, when the test ends."""
    with modal.App("pytest-modal-live").run() as running:
        yield running
