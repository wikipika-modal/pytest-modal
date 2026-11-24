import pytest


@pytest.hookspec(firstresult=True)
def pytest_modal_image(config):
    """Return the modal.Image the tests run in when the project is uploaded. The project is added on top."""


@pytest.hookspec(firstresult=True)
def pytest_modal_sandbox(config):
    """Return keyword arguments for modal.Sandbox.create, such as cpu, memory, gpu, secrets or volumes."""
