import sys

import pytest


def pytest_modal_image(config):
    import modal

    version = "{}.{}".format(*sys.version_info)
    return modal.Image.debian_slim(python_version=version).pip_install(f"pytest=={pytest.__version__}", "numpy")
