import pytest


@pytest.hookspec(firstresult=True)
def pytest_modal_image(config):
    """Return the modal.Image the tests run in. The project directory is added on top of it."""


@pytest.hookspec(firstresult=True)
def pytest_modal_sandbox(config):
    """Return keyword arguments for modal.Sandbox.create, such as cpu, memory, gpu, secrets or volumes."""
