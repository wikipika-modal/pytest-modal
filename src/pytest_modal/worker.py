"""Loaded with -p inside each shard. Needs only pytest and the standard library."""

from __future__ import annotations

import json
import os
import sys

import pytest

from ._common import MARK, add_hooks, add_options

# Taken at import, before pytest's capture replaces the process's stdin and stdout.
_stdin, _stdout = os.dup(0), os.dup(1)


def pytest_addoption(parser):
    add_options(parser)


def pytest_addhooks(pluginmanager):
    add_hooks(pluginmanager)


def pytest_configure(config):
    if config.getoption("modal_worker"):
        config.pluginmanager.register(Worker(config), "modal-worker")


class Worker:
    def __init__(self, config):
        self.config = config
        with os.fdopen(_stdin) as ids:
            self.ids = ids.read().split("\n")
        self.out = os.fdopen(_stdout, "w")
        self.emit("hello", sysplatform=sys.platform, version_info=tuple(sys.version_info), executable=sys.executable)

    def emit(self, kind, **data):
        self.out.write(MARK + json.dumps({"kind": kind, **data}, default=repr) + "\n")
        self.out.flush()

    def report(self, report):
        self.emit("report", report=self.config.hook.pytest_report_to_serializable(config=self.config, report=report))

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        found = {item.nodeid: item for item in items}
        keep = [found[nodeid] for nodeid in self.ids if nodeid in found]
        kept = {item.nodeid for item in keep}
        dropped = [item for item in items if item.nodeid not in kept]
        if dropped:
            config.hook.pytest_deselected(items=dropped)
        items[:] = keep

    def pytest_collectreport(self, report):
        if report.failed:
            self.report(report)

    def pytest_runtest_logstart(self, nodeid, location):
        self.emit("logstart", nodeid=nodeid, location=location)

    def pytest_runtest_logfinish(self, nodeid, location):
        self.emit("logfinish", nodeid=nodeid, location=location)

    def pytest_runtest_logreport(self, report):
        self.report(report)
