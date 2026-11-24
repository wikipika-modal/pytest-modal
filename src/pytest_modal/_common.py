from __future__ import annotations

import argparse

MARK = "@@pytest-modal@@ "
BOOT = "import sys; sys.path.insert(0, sys.argv.pop(1)); import pytest; sys.exit(pytest.main(sys.argv[1:]))"


def add_options(parser) -> None:
    if getattr(parser, "_modal_options", False):
        return
    parser._modal_options = True
    group = parser.getgroup("modal", "run tests in Modal sandboxes")
    group.addoption("--modal", action="store_true", help="run the collected tests in Modal sandboxes")
    group.addoption("--modal-shards", default="auto", metavar="N", help="number of sandboxes, or auto (default)")
    group.addoption("--modal-max-shards", type=int, default=64, metavar="N", help="upper bound for auto (default 64)")
    group.addoption("--modal-group-by", choices=("file", "test"), default="file", help="what stays in one sandbox")
    group.addoption(
        "--modal-backend", choices=("modal", "local"), default="modal", help="local: one subprocess a shard"
    )
    group.addoption("--modal-app", default="pytest-modal", metavar="NAME", help="Modal app that owns the sandboxes")
    group.addoption("--modal-worker", action="store_true", help=argparse.SUPPRESS)


def add_hooks(pluginmanager) -> None:
    if getattr(pluginmanager, "_modal_hooks", False):
        return
    pluginmanager._modal_hooks = True
    from . import hookspecs

    pluginmanager.add_hookspecs(hookspecs)
