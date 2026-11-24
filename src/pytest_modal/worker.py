"""Loaded with -p inside each shard. Needs only pytest and the standard library."""

from __future__ import annotations

import base64
import json
import os
import sys
import time

import pytest

from ._common import MARK, add_hooks, add_options, group_mark

# Taken at import, before pytest's capture replaces the process's stdin and stdout.
_stdin, _stdout = os.dup(0), os.dup(1)


def pytest_addoption(parser):
    add_options(parser)


def pytest_addhooks(pluginmanager):
    add_hooks(pluginmanager)


def pytest_configure(config):
    mode = config.getoption("modal_worker")
    if mode:
        config.pluginmanager.register(Worker(config, mode == "collect"), "modal-worker")


class Worker:
    def __init__(self, config, collecting):
        self.config = config
        self.collecting = collecting
        with os.fdopen(_stdin) as ids:
            self.ids = [line for line in ids.read().split("\n") if line]
        self.out = os.fdopen(_stdout, "w")
        self.mark = time.monotonic()
        self.emit("hello", sysplatform=sys.platform, version_info=tuple(sys.version_info), executable=sys.executable)

    def emit(self, kind, **data):
        self.out.write(MARK + json.dumps({"kind": kind, **data}, default=repr) + "\n")
        self.out.flush()

    def report(self, report):
        self.emit("report", report=self.config.hook.pytest_report_to_serializable(config=self.config, report=report))

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        if self.collecting:
            listed = [[item.nodeid, item.location, group_mark(item)] for item in items]
            for start in range(0, len(listed), 500):
                self.emit("items", items=listed[start : start + 500])
            return
        found = {item.nodeid: item for item in items}
        missing = [nodeid for nodeid in self.ids if nodeid not in found]
        if missing:
            self.emit("missing", ids=missing)
        keep = [found[nodeid] for nodeid in self.ids if nodeid in found]
        kept = {item.nodeid for item in keep}
        dropped = [item for item in items if item.nodeid not in kept]
        if dropped:
            config.hook.pytest_deselected(items=dropped)
        items[:] = keep
        self.mark = time.monotonic()

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self):
        return True if self.collecting else None

    def pytest_collectreport(self, report):
        # A file skipped as a whole has no tests for a shard to run, so only collection can report it.
        if report.failed or (self.collecting and report.skipped):
            self.report(report)

    def pytest_warning_recorded(self, warning_message, when, nodeid, location):
        # A shard collects its files again; those warnings were already recorded where the tests were listed.
        if self.collecting or when == "runtest":
            category = warning_message.category
            self.emit(
                "warning",
                message=str(warning_message.message),
                category=[category.__module__, category.__name__],
                filename=warning_message.filename,
                lineno=warning_message.lineno,
                line=warning_message.line,
                when=when,
                nodeid=nodeid,
                location=location,
            )

    def pytest_runtest_logstart(self, nodeid, location):
        self.emit("logstart", nodeid=nodeid, location=location)

    def pytest_runtest_logfinish(self, nodeid, location):
        # What the test cost this process: everything since the test before it finished, which also
        # counts the time pytest and other plugins spend around a test and report for none.
        now = time.monotonic()
        self.emit("logfinish", nodeid=nodeid, location=location, took=now - self.mark)
        self.mark = now

    def pytest_runtest_logreport(self, report):
        self.report(report)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self):
        measured = getattr(self.config.pluginmanager.get_plugin("_cov"), "cov_controller", None)
        if measured is not None and not self.collecting:
            self.emit("coverage", data=base64.b64encode(measured.cov.get_data().dumps()).decode())
        self.emit("bye")
