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
        "--modal-collect",
        choices=("local", "remote"),
        default="local",
        help="remote: collect in a sandbox; this process then imports no test code and loads no conftest.py",
    )
    group.addoption(
        "--modal-from",
        choices=("auto", "upload", "disk"),
        default="auto",
        help="upload the project, or start shards from a snapshot of this disk (auto: disk inside a sandbox)",
    )
    group.addoption("--modal-retries", type=int, default=2, metavar="N", help="restarts of a shard that ran nothing")
    group.addoption("--modal-args", default="", metavar="ARGS", help="extra pytest arguments for the shards only")
    group.addoption(
        "--modal-backend", choices=("modal", "local"), default="modal", help="local: one subprocess a shard"
    )
    group.addoption("--modal-app", default="pytest-modal", metavar="NAME", help="name of the Modal app for the run")
    group.addoption("--modal-worker", choices=("run", "collect"), help=argparse.SUPPRESS)
    parser.addini("modal_conf", "file with pytest-modal hooks, loaded only by the controller", default="modal_conf.py")
    parser.addini(
        "modal_isolate",
        "each line: patterns for test files that run together in a sandbox of their own",
        type="linelist",
        default=[],
    )
    parser.addini("modal_python", "interpreter the shards run (default: python, or this one with a disk snapshot)")
    parser.addini("modal_timeout", "seconds a sandbox may live", default="3600")
    parser.addini("modal_start_timeout", "seconds a shard may take to start before it is replaced", default="300")


def add_hooks(pluginmanager) -> None:
    if getattr(pluginmanager, "_modal_hooks", False):
        return
    pluginmanager._modal_hooks = True
    from . import hookspecs

    pluginmanager.add_hookspecs(hookspecs)


def group_mark(item) -> str | None:
    mark = item.get_closest_marker("xdist_group")
    if mark is None:
        return None
    return "group:" + str(mark.args[0] if mark.args else mark.kwargs.get("name"))
