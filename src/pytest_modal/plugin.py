from __future__ import annotations

import json
import queue
import sys
import threading
import time
from collections import deque

import pytest

from ._common import MARK, add_hooks, add_options
from .backends import BACKENDS
from .plan import plan

DURATIONS = "modal/durations"


def pytest_addoption(parser):
    add_options(parser)


def pytest_addhooks(pluginmanager):
    add_hooks(pluginmanager)


def pytest_configure(config):
    if config.getoption("modal") and not config.getoption("modal_worker"):
        config.pluginmanager.register(Controller(config), "modal-controller")


class Gateway:
    def __init__(self, id):
        self.id = id


class Lane:
    """One shard as the controller sees it. Reports carry it as `node`, where the terminal and
    junitxml plugins look for a worker's id."""

    def __init__(self, shard):
        self.shard = shard
        self.gateway = Gateway(f"modal{shard.index}")
        self.workerinfo = {"sysplatform": sys.platform, "version_info": tuple(sys.version_info), "executable": ""}
        self.workerinfo["id"] = self.gateway.id
        self.pending = dict.fromkeys(shard.ids)
        self.tail = deque(maxlen=15)
        self.seconds = 0.0


class Controller:
    def __init__(self, config):
        self.config = config
        self.lanes = []
        self.spent = {}
        self.wall = 0.0

    def pytest_report_header(self):
        return f"modal: {self.config.getoption('modal_backend')} backend"

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session):
        config = self.config
        if session.testsfailed and not config.option.continue_on_collection_errors:
            raise session.Interrupted(f"{session.testsfailed} errors during collection")
        if config.option.collectonly or not session.items:
            return True
        cache = getattr(config, "cache", None)
        known = cache.get(DURATIONS, {}) if cache else {}
        by_file = config.getoption("modal_group_by") == "file"
        tests = [(item.nodeid, group_of(item, by_file)) for item in session.items]
        count = config.getoption("modal_shards")
        shards = plan(tests, None if count == "auto" else int(count), known, config.getoption("modal_max_shards"))
        self.locations = {item.nodeid: item.location for item in session.items}
        self.lanes = [Lane(shard) for shard in shards]
        terminal = config.pluginmanager.get_plugin("terminalreporter")
        if terminal:
            terminal.showfspath = False
        args = worker_args(config)
        backend = BACKENDS[config.getoption("modal_backend")](config)
        events = queue.Queue()
        started = time.monotonic()
        try:
            for lane in self.lanes:
                threading.Thread(target=pump, args=(backend, lane, args, events), daemon=True).start()
            running = len(self.lanes)
            while running:
                lane, event = events.get()
                if isinstance(event, str):
                    self.line(lane, event)
                else:
                    running -= 1
                    lane.seconds = time.monotonic() - started
                    self.finish(lane, event)
                if session.shouldfail:
                    raise session.Failed(session.shouldfail)
                if session.shouldstop:
                    raise session.Interrupted(session.shouldstop)
        finally:
            backend.close()
            self.wall = time.monotonic() - started
            if cache:
                cache.set(DURATIONS, {**known, **self.spent})
        return True

    def line(self, lane, line):
        at = line.find(MARK)
        if at < 0:
            lane.tail.append(line.rstrip())
            return
        event = json.loads(line[at + len(MARK) :])
        kind = event.pop("kind")
        hook = self.config.hook
        if kind == "hello":
            lane.workerinfo.update(event)
        elif kind == "logstart":
            hook.pytest_runtest_logstart(nodeid=event["nodeid"], location=tuple(event["location"]))
        elif kind == "logfinish":
            hook.pytest_runtest_logfinish(nodeid=event["nodeid"], location=tuple(event["location"]))
        else:
            report = hook.pytest_report_from_serializable(config=self.config, data=event["report"])
            report.node = lane
            if isinstance(report, pytest.CollectReport):
                hook.pytest_collectreport(report=report)
                return
            # JSON turned these tuples into lists; junitxml asserts on the skip tuple.
            report.location = tuple(report.location)
            if isinstance(report.longrepr, list):
                report.longrepr = tuple(report.longrepr)
            self.spent[report.nodeid] = self.spent.get(report.nodeid, 0.0) + report.duration
            if report.when == "teardown":
                lane.pending.pop(report.nodeid, None)
            hook.pytest_runtest_logreport(report=report)

    def finish(self, lane, outcome):
        if not lane.pending:
            return
        how = f"exited {outcome}" if isinstance(outcome, int) else f"failed: {outcome!r}"
        head = f"{lane.gateway.id} ({lane.shard.where}) {how} without a result for this test"
        for nodeid in lane.pending:
            report = pytest.TestReport(
                nodeid, self.locations[nodeid], {}, "failed", "\n".join([head, *lane.tail]), "setup"
            )
            report.node = lane
            self.config.hook.pytest_runtest_logreport(report=report)

    def pytest_terminal_summary(self, terminalreporter):
        if not self.lanes:
            return
        if self.config.option.verbose > 0:
            for lane in self.lanes:
                shard = lane.shard
                terminalreporter.write_line(
                    f"{lane.gateway.id} {shard.where} {len(shard.ids)} tests {lane.seconds:.1f}s"
                )
        tests = sum(self.spent.values())
        terminalreporter.write_line(f"modal: {len(self.lanes)} shards, {self.wall:.1f}s wall, {tests:.1f}s of tests")


def pump(backend, lane, args, events):
    try:
        outcome = backend.run(lane.shard, args, lambda line: events.put((lane, line)))
    except Exception as error:  # noqa: BLE001
        outcome = error
    events.put((lane, outcome))


def group_of(item, by_file):
    mark = item.get_closest_marker("xdist_group")
    if mark:
        return "group:" + str(mark.args[0] if mark.args else mark.kwargs.get("name"))
    return item.nodeid.split("::")[0] if by_file else item.nodeid


def worker_args(config):
    given = [str(arg) for arg in config.invocation_params.args]
    if config.args_source == pytest.Config.ArgsSource.ARGS:
        given = [arg for arg in given if arg not in config.args]
    return [*given, "--modal-worker", "-p", "pytest_modal.worker"]
