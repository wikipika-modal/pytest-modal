from __future__ import annotations

import base64
import contextlib
import importlib.util
import json
import queue
import shlex
import signal
import sys
import threading
import time
import warnings
from collections import deque

import pytest

from ._common import MARK, add_hooks, add_options, group_mark
from .backends import BACKENDS
from .plan import Shard, plan

DURATIONS = "modal/durations"


def pytest_addoption(parser):
    add_options(parser)


def pytest_addhooks(pluginmanager):
    add_hooks(pluginmanager)


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config):
    options = early_config.known_args_namespace
    if options.modal and options.modal_collect == "remote" and not options.modal_worker:
        options.noconftest = True


def pytest_configure(config):
    if not config.getoption("modal") or config.getoption("modal_worker"):
        return
    conf = config.rootpath / config.getini("modal_conf")
    if conf.is_file():
        spec = importlib.util.spec_from_file_location("modal_conf", conf)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config.pluginmanager.register(module, "modal_conf")
    config.pluginmanager.register(Controller(config), "modal-controller")


class Gateway:
    def __init__(self, id):
        self.id = id


class Lane:
    """One sandbox's turn at a shard. Reports carry it as `node`, where the terminal and junitxml
    plugins look for a worker's id."""

    def __init__(self, shard, mode, total, stalled):
        self.shard = shard
        self.mode = mode
        self.stalled = stalled
        self.gateway = Gateway(f"modal{shard.index}")
        self.workerinfo = {"sysplatform": sys.platform, "version_info": tuple(sys.version_info), "executable": ""}
        self.workerinfo["id"] = self.gateway.id
        self.env = {"PYTEST_MODAL_SHARD": str(shard.index), "PYTEST_MODAL_SHARDS": str(total)}
        self.pending = dict.fromkeys(shard.ids)
        self.missing = []
        self.current = None
        self.finished = 0
        self.greeted = self.bye = False
        self.tail = deque(maxlen=15)
        self.launched = time.monotonic()
        self.seconds = 0.0
        self.stop = None
        self.outcome = None

    def ended(self):
        how = f"exited {self.outcome}" if isinstance(self.outcome, int) else f"failed: {self.outcome!r}"
        return f"{self.gateway.id} ({self.shard.where}) {how}"


class Controller:
    def __init__(self, config):
        self.config = config
        self.remote = config.getoption("modal_collect") == "remote"
        given = [str(arg) for arg in config.invocation_params.args]
        named = config.args if config.args_source == pytest.Config.ArgsSource.ARGS else []
        self.targets = [arg for arg in given if arg in named]
        self.given = [arg for arg in given if arg not in named] + shlex.split(config.getoption("modal_args"))
        self.retries = config.getoption("modal_retries")
        self.patience = float(config.getini("modal_start_timeout"))
        self.backend = None
        self.events = queue.Queue()
        self.lanes = []
        self.live = set()
        self.restarts = 0
        self.shards = 0
        self.tests = []
        self.locations = {}
        self.spent = {}
        self.wall = 0.0

    def pytest_report_header(self):
        return f"modal: {self.config.getoption('modal_backend')} backend"

    def say(self, text):
        terminal = self.config.pluginmanager.get_plugin("terminalreporter")
        if terminal:
            terminal.write_line(text)

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session):
        if not self.remote:
            return None
        with calm():
            for _ in range(self.retries + 1):
                self.tests, self.locations = [], {}
                lane = self.launch(Shard(0), "collect")
                self.drain()
                if lane.bye:
                    break
        if not lane.bye:
            raise pytest.UsageError("\n".join([f"collection: {lane.ended()}", *lane.tail]))
        session.items = []
        session.testscollected = len(self.tests)
        self.say(f"collected {len(self.tests)} items in {lane.shard.where}")
        return True

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session):
        config = self.config
        if session.testsfailed and not config.option.continue_on_collection_errors:
            raise session.Interrupted(f"{session.testsfailed} errors during collection")
        if not self.remote:
            self.tests = [(item.nodeid, group_mark(item)) for item in session.items]
            self.locations = {item.nodeid: item.location for item in session.items}
        if config.option.collectonly:
            for nodeid, _ in self.tests if self.remote else []:
                self.say(nodeid)
            return True
        if not self.tests:
            return True
        by_file = config.getoption("modal_group_by") == "file"
        tests = [(nodeid, group or (nodeid.split("::")[0] if by_file else nodeid)) for nodeid, group in self.tests]
        cache = getattr(config, "cache", None)
        known = cache.get(DURATIONS, {}) if cache else {}
        count = config.getoption("modal_shards")
        shards = plan(tests, None if count == "auto" else int(count), known, config.getoption("modal_max_shards"))
        self.shards = len(shards)
        terminal = config.pluginmanager.get_plugin("terminalreporter")
        if terminal:
            terminal.showfspath = False
        main = threading.current_thread() is threading.main_thread()
        previous = signal.signal(signal.SIGTERM, interrupt) if main else None
        started = time.monotonic()
        try:
            with calm():
                for shard in shards:
                    self.launch(shard)
                self.drain(session)
        finally:
            if main:
                signal.signal(signal.SIGTERM, previous)
            self.wall = time.monotonic() - started
            if cache:
                cache.set(DURATIONS, {**known, **self.spent})
        return True

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self):
        if self.backend:
            self.backend.close()

    def launch(self, shard, mode="run", stalled=0):
        if self.backend is None:
            try:
                self.backend = BACKENDS[self.config.getoption("modal_backend")](self.config)
            except Exception as error:
                raise pytest.UsageError(f"pytest-modal could not start: {error}") from error
        lane = Lane(shard, mode, self.shards, stalled)
        root = self.config.rootpath
        paths = self.targets if mode == "collect" else [str(root / path) for path in shard.paths]
        args = [*self.given, f"--modal-worker={mode}", "-p", "pytest_modal.worker", "--rootdir", str(root), *paths]
        self.lanes.append(lane)
        self.live.add(lane)
        threading.Thread(target=pump, args=(self.backend, lane, args, self.events), daemon=True).start()
        return lane

    def drain(self, session=None):
        while self.live:
            try:
                lane, event = self.events.get(timeout=1)
            except queue.Empty:
                self.replace_slow_starters()
                continue
            if isinstance(event, str):
                self.line(lane, event)
            else:
                self.live.discard(lane)
                lane.seconds = time.monotonic() - lane.launched
                lane.outcome = event
                if lane.mode == "run":
                    self.finish(lane)
            if session is not None and session.shouldfail:
                raise session.Failed(session.shouldfail)
            if session is not None and session.shouldstop:
                raise session.Interrupted(session.shouldstop)

    def replace_slow_starters(self):
        now = time.monotonic()
        for lane in self.live:
            if lane.stop and not lane.greeted and now - lane.launched > self.patience:
                lane.tail.append(f"no start within {self.patience:.0f}s")
                stop, lane.stop = lane.stop, None
                stop()

    def line(self, lane, line):
        at = line.find(MARK)
        if at < 0:
            lane.tail.append(line.rstrip())
            return
        event = json.loads(line[at + len(MARK) :])
        kind = event.pop("kind")
        hook = self.config.hook
        if kind == "hello":
            lane.greeted = True
            lane.workerinfo.update(event)
        elif kind == "items":
            for nodeid, location, group in event["items"]:
                self.tests.append((nodeid, group))
                self.locations[nodeid] = tuple(location)
        elif kind == "missing":
            lane.missing = event["ids"]
        elif kind == "coverage":
            self.coverage(lane, event["data"])
        elif kind == "bye":
            lane.bye = True
        elif kind == "logstart":
            lane.current = event["nodeid"]
            hook.pytest_runtest_logstart(nodeid=event["nodeid"], location=tuple(event["location"]))
        elif kind == "logfinish":
            lane.current = None
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
            if report.when == "teardown" and report.nodeid in lane.pending:
                del lane.pending[report.nodeid]
                lane.finished += 1
            hook.pytest_runtest_logreport(report=report)

    def finish(self, lane):
        for nodeid in lane.missing:
            if nodeid in lane.pending:
                del lane.pending[nodeid]
                self.fail(lane, nodeid, "setup", "did not collect this test")
        crashed = lane.current in lane.pending
        if crashed:
            del lane.pending[lane.current]
            self.fail(lane, lane.current, "call", "while running this test")
        left = list(lane.pending)
        if not left:
            return
        stalled = 0 if crashed or lane.finished else lane.stalled + 1
        if lane.bye or stalled > self.retries:
            for nodeid in left:
                self.fail(lane, nodeid, "setup", "without running this test")
            return
        self.restarts += 1
        self.launch(Shard(lane.shard.index, left), stalled=stalled)

    def fail(self, lane, nodeid, when, what):
        text = "\n".join([f"{lane.ended()} {what}", *lane.tail])
        report = pytest.TestReport(nodeid, self.locations[nodeid], {}, "failed", text, when)
        report.node = lane
        self.config.hook.pytest_runtest_logreport(report=report)

    def coverage(self, lane, blob):
        measured = getattr(self.config.pluginmanager.get_plugin("_cov"), "cov_controller", None)
        if measured is None:
            return
        from coverage import CoverageData

        data = CoverageData(basename=measured.combining_cov.config.data_file, suffix=f"modal{self.lanes.index(lane)}")
        data.loads(base64.b64decode(blob))
        data.write()

    def pytest_terminal_summary(self, terminalreporter):
        ran = [lane for lane in self.lanes if lane.mode == "run"]
        if not ran:
            return
        if self.config.option.verbose > 0:
            for lane in ran:
                shard = lane.shard
                terminalreporter.write_line(
                    f"{lane.gateway.id} {shard.where} {len(shard.ids)} tests {lane.seconds:.1f}s"
                )
        restarts = f", {self.restarts} restarts" if self.restarts else ""
        tests = sum(self.spent.values())
        terminalreporter.write_line(
            f"modal: {self.shards} shards, {self.wall:.1f}s wall, {tests:.1f}s of tests{restarts}"
        )


def pump(backend, lane, args, events):
    try:
        outcome = backend.run(lane, args, lambda line: events.put((lane, line)))
    except Exception as error:  # noqa: BLE001
        outcome = error
    events.put((lane, outcome))


def interrupt(signum, frame):
    raise KeyboardInterrupt


@contextlib.contextmanager
def calm():
    """The project's warning filters, often `error`, are meant for its tests and not for this process's
    own threads."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        yield
