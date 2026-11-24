from __future__ import annotations

import base64
import contextlib
import importlib.util
import json
import os
import queue
import shlex
import signal
import sys
import threading
import time
import warnings
from collections import deque
from fnmatch import fnmatch
from pathlib import Path

import pytest

from ._common import MARK, add_hooks, add_options, elsewhere, group_mark
from .backends import BACKENDS, Local, origin, sign_in, without_modal
from .plan import Shard, plan

DURATIONS = "modal/durations"
ISOLATED = "isolate:"
OUTPUT = ("Captured stdout", "Captured stderr")
CONTROLLER_ONLY = ("--junitxml", "--junit-xml", "--basetemp", "--cov-report", "--cov-fail-under")


def pytest_addoption(parser):
    add_options(parser)


def pytest_addhooks(pluginmanager):
    add_hooks(pluginmanager)


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config):
    options = early_config.known_args_namespace
    if not options.modal or options.modal_worker:
        return
    remote = options.modal_collect == "remote" or elsewhere(options.modal_from)
    options.noconftest = options.noconftest or remote
    unused = options.help or options.version or (options.collectonly and not remote)
    if options.modal_backend == "modal" and not unused:
        origin(options.modal_from)
        sign_in()


def pytest_configure(config):
    if not config.getoption("modal") or config.getoption("modal_worker"):
        return
    if config.option.usepdb or config.option.trace:
        raise pytest.UsageError("--pdb and --trace need a terminal where the test runs; run that test without --modal")
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
        self.queued = self.created = self.up = self.seconds = 0.0
        self.stop = None
        self.outcome = None

    def ended(self):
        how = f"exited {self.outcome}" if isinstance(self.outcome, int) else f"failed: {self.outcome!r}"
        return f"{self.gateway.id} ({self.shard.where}) {how}"


class Controller:
    def __init__(self, config):
        self.config = config
        foreign = elsewhere(config.getoption("modal_from"))
        self.remote = foreign or config.getoption("modal_collect") == "remote"
        # The shards' root directory; one that is not on this machine is learned from the shard that collects.
        self.root = None if foreign else config.rootpath
        self.targets, options = split(config)
        self.given = for_shards(options) + shlex.split(config.getoption("modal_args"))
        if config.pluginmanager.hasplugin("_cov"):
            self.given.append("--cov-report=")
        # With -s a shard still captures, and what each test wrote is shown here when the test finishes.
        self.loud = config.option.capture == "no"
        if self.loud:
            self.given.append("--capture=fd")
        self.retries = config.getoption("modal_retries")
        self.patience = float(config.getini("modal_start_timeout"))
        self.backend = self.collector = self.starting = self.broken = None
        self.named = False
        self.events = queue.Queue()
        self.lanes = []
        self.live = set()
        self.restarts = 0
        self.shards = 0
        self.tests = []
        self.locations = {}
        self.spent = {}
        self.wall = 0.0
        self.phases = {}

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
        config = self.config
        # The interpreter that runs the tests is on this disk when the shards start from a snapshot of it:
        # it lists the tests here while the snapshot is taken.
        if config.getoption("modal_backend") == "modal" and origin(config.getoption("modal_from")) == "disk":
            self.collector = Local(config, without_modal(os.environ))
        with calm():
            self.start()
            for _ in range(self.retries + 1):
                self.tests, self.locations = [], {}
                lane = self.launch(Shard(0), "collect")
                self.drain()
                if lane.bye:
                    break
        self.phases["collect"] = lane.seconds
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
        tests = [(nodeid, self.group(nodeid, group)) for nodeid, group in self.tests]
        alone = frozenset(group for _, group in tests if group.startswith(ISOLATED))
        cache = getattr(config, "cache", None)
        # Weights live in two places: this machine's cache, and the backend's store for machines that keep none.
        project = (self.root or config.rootpath).name
        known = {**self.ready().weights(project), **(cache.get(DURATIONS, {}) if cache else {})}
        count = None if config.getoption("modal_shards") == "auto" else int(config.getoption("modal_shards"))
        shards = plan(tests, count, known, config.getoption("modal_max_shards"), alone)
        self.shards = len(shards)
        terminal = config.pluginmanager.get_plugin("terminalreporter")
        if terminal:
            terminal.showfspath = False
        main = threading.current_thread() is threading.main_thread()
        previous = signal.signal(signal.SIGTERM, interrupt) if main else None
        started = time.monotonic()
        try:
            with calm():
                # Heaviest first: the backend may start them a few at a time, and the run ends no sooner
                # than the heaviest does.
                for shard in sorted(shards, key=lambda shard: -shard.weight):
                    self.launch(shard)
                self.drain(session)
        finally:
            if main:
                signal.signal(signal.SIGTERM, previous)
            self.wall = time.monotonic() - started
            if cache:
                cache.set(DURATIONS, {**known, **self.spent})
            self.backend.keep(project, {**known, **self.spent})
        return True

    def group(self, nodeid, marked):
        path = nodeid.split("::")[0]
        for number, line in enumerate(self.config.getini("modal_isolate")):
            if any(fnmatch(path, pattern) for pattern in line.split()):
                return f"{ISOLATED}{number}"
        return marked or (path if self.config.getoption("modal_group_by") == "file" else nodeid)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self):
        if self.starting:
            self.starting.join()
        for runner in (self.collector, self.backend):
            if runner:
                runner.close()

    def start(self):
        if self.starting is None:
            self.starting = threading.Thread(target=self.create, daemon=True)
            self.starting.start()

    def create(self):
        started = time.monotonic()
        try:
            self.backend = BACKENDS[self.config.getoption("modal_backend")](self.config)
        except Exception as error:  # noqa: BLE001
            self.broken = error
        self.phases["start"] = time.monotonic() - started

    def ready(self):
        self.start()
        self.starting.join(5)
        if self.starting.is_alive():
            self.say("modal: preparing the image the shards start from")
            self.starting.join()
        if self.broken:
            raise pytest.UsageError(f"pytest-modal could not start: {self.broken}") from self.broken
        if self.backend.name and not self.named:
            # Said as soon as it is known: a run that hangs is found and stopped by this.
            self.named = True
            self.say(f"modal: {self.backend.name}")
        return self.backend

    def launch(self, shard, mode="run", stalled=0):
        runner = self.collector if mode == "collect" and self.collector else self.ready()
        lane = Lane(shard, mode, self.shards, stalled)
        if "PYTEST_ADDOPTS" in os.environ:
            lane.env["PYTEST_ADDOPTS"] = shlex.join(for_shards(shlex.split(os.environ["PYTEST_ADDOPTS"])))
        root = ["--rootdir", str(self.root)] if self.root else []
        paths = self.targets if mode == "collect" else [str(self.root / path) for path in shard.paths]
        args = [*self.given, f"--modal-worker={mode}", "-p", "pytest_modal.worker", *root, *paths]
        self.lanes.append(lane)
        self.live.add(lane)
        threading.Thread(target=pump, args=(runner, lane, args, self.events), daemon=True).start()
        return lane

    def drain(self, session=None):
        check = 0.0
        while self.live:
            # By the clock: with many shards reporting there may be no quiet second to wait for.
            if time.monotonic() >= check:
                self.replace_slow_starters()
                check = time.monotonic() + 1
            try:
                lane, event = self.events.get(timeout=1)
            except queue.Empty:
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
                # Not from this thread: stopping a sandbox is a call to Modal, and while this thread waits
                # for one no shard's reports are read.
                threading.Thread(target=quietly, args=(stop,), daemon=True).start()

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
            lane.up = time.monotonic() - lane.launched
            theirs = event.pop("pytest")
            if theirs != pytest.__version__:
                # Reports cross between the two as pytest's own serialised form, which is tied to its version.
                raise pytest.UsageError(
                    f"the shards run pytest {theirs} ({event['executable']}) and this process runs pytest "
                    f"{pytest.__version__}: install the same version in both"
                )
            self.root = self.root or Path(event.pop("rootdir"))
            # pytest 8.0 formats this with %, which wants the tuple JSON turned into a list.
            lane.workerinfo.update(event, version_info=tuple(event["version_info"]))
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
        elif kind == "warning":
            module, name = event.pop("category")
            # Only a category this process already has is looked up; it imports nothing of the project's.
            category = getattr(sys.modules.get(module), name, None)
            if not (isinstance(category, type) and issubclass(category, Warning)):
                category = type(name, (Warning,), {"__module__": module})
            recorded = warnings.WarningMessage(
                event["message"], category, event["filename"], event["lineno"], line=event["line"]
            )
            where = tuple(event["location"]) if event["location"] else None
            hook.pytest_warning_recorded.call_historic(
                kwargs={
                    "warning_message": recorded,
                    "when": event["when"],
                    "nodeid": event["nodeid"],
                    "location": where,
                }
            )
        elif kind == "logstart":
            lane.current = event["nodeid"]
            hook.pytest_runtest_logstart(nodeid=event["nodeid"], location=tuple(event["location"]))
        elif kind == "logfinish":
            lane.current = None
            self.spent[event["nodeid"]] = event["took"]
            hook.pytest_runtest_logfinish(nodeid=event["nodeid"], location=tuple(event["location"]))
        else:
            report = hook.pytest_report_from_serializable(config=self.config, data=event["report"])
            report.node = lane
            # JSON turned these tuples into lists; junitxml asserts on the skip tuple.
            if isinstance(report.longrepr, list):
                report.longrepr = tuple(report.longrepr)
            if isinstance(report, pytest.CollectReport):
                hook.pytest_collectreport(report=report)
                return
            report.location = tuple(report.location)
            if report.when == "teardown" and report.nodeid in lane.pending:
                del lane.pending[report.nodeid]
                lane.finished += 1
            if self.loud:
                written = [section for section in report.sections if section[0].startswith(OUTPUT)]
                if written and report.when == "teardown":
                    self.say("".join(text for _, text in written).rstrip("\n"))
                report.sections = [section for section in report.sections if section not in written]
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
                    f"{lane.gateway.id} {shard.where} {len(shard.ids)} tests {lane.seconds:.1f}s "
                    f"(created {lane.created:.1f}s, up {lane.up:.1f}s)"
                )
        restarts = f", {self.restarts} restarts" if self.restarts else ""
        phases = "".join(f", {name} {seconds:.1f}s" for name, seconds in self.phases.items())
        tests = sum(self.spent.values())
        where = f", {self.backend.name}" if self.backend.name else ""
        up = sorted(lane.up for lane in ran if lane.greeted)
        phases += f", shards up in {up[len(up) // 2]:.1f}s (slowest {up[-1]:.1f}s)" if up else ""
        queued = max(lane.queued for lane in ran)
        phases += f", the last waited {queued:.1f}s for its turn to start" if queued >= 1 else ""
        terminalreporter.write_line(
            f"modal: {self.shards} shards, {self.wall:.1f}s wall, {tests:.1f}s of tests{phases}{restarts}{where}"
        )


class Argument(str):
    """One command-line argument, told from an equal one by identity."""


def split(config):
    """The positional arguments of the command line and the rest of it. pytest's parser says which are
    which: it hands a positional argument back as the object it was given."""
    given = [Argument(arg) for arg in config.invocation_params.args]
    positional = {id(arg) for arg in config._parser.parse_known_args(given).file_or_dir}
    return [arg for arg in given if id(arg) in positional], [arg for arg in given if id(arg) not in positional]


def for_shards(options):
    """Drop the options whose value is a file on this machine or a verdict on the whole run; the controller
    writes those reports from what the shards send back."""
    kept, skip = [], False
    for option in options:
        name, attached, _ = option.partition("=")
        if skip or name in CONTROLLER_ONLY:
            skip = not skip and not attached
        else:
            kept.append(option)
    return kept


def pump(backend, lane, args, events):
    try:
        outcome = backend.run(lane, args, lambda line: events.put((lane, line)))
    except Exception as error:  # noqa: BLE001
        outcome = error
    events.put((lane, outcome))


def quietly(stop):
    with contextlib.suppress(Exception):
        stop()


def interrupt(signum, frame):
    raise KeyboardInterrupt


@contextlib.contextmanager
def calm():
    """The project's warning filters, often `error`, are meant for its tests and not for this process's
    own threads."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        yield
