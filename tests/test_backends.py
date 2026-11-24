"""The Modal backend against a stand-in for the Modal client: what it asks of a sandbox, and in what order."""

import threading
import time
from types import SimpleNamespace

import pytest

from pytest_modal._common import BYE, MARK
from pytest_modal.backends import Modal, lines
from pytest_modal.plan import Shard


class Process:
    def __init__(self, client):
        self.stdout = client.output()
        self.stdin = SimpleNamespace(write=lambda text: None, write_eof=lambda: None, drain=client.started)

    def wait(self):
        return 3


class Box:
    object_id = "sb-stand-in"

    def __init__(self, client):
        self.client = client

    def exec(self, *args, **options):
        self.client.execs.append(options)
        return Process(self.client)

    def terminate(self):
        self.client.terminated += 1


class Client:
    """Stands where the `modal` module does."""

    def __init__(self, pieces=()):
        self.pieces = pieces
        self.execs = []
        self.terminated = 0
        self.Sandbox = SimpleNamespace(create=self.create)
        self.lock = threading.Lock()
        self.starting = self.most = self.created = 0
        self.read = threading.Event()
        self.read.set()

    def create(self, *args, **options):
        with self.lock:
            self.created += 1
            self.starting += 1
            self.most = max(self.most, self.starting)
        time.sleep(0.01)
        return Box(self)

    def started(self):
        with self.lock:
            self.starting -= 1

    def output(self):
        self.read.wait(10)
        yield from self.pieces


@pytest.fixture
def backend():
    made = object.__new__(Modal)
    made.modal = Client()
    made.app = made.image = None
    made.options, made.tags, made.env = {}, {}, {}
    made.command, made.cwd = ["python"], "/"
    made.gate, made.closed = threading.BoundedSemaphore(3), False
    return made


def lane(index=0):
    shard = Shard(index, [f"test_{index}.py::test"])
    return SimpleNamespace(
        shard=shard, mode="run", env={}, gateway=SimpleNamespace(id=f"modal{index}"), launched=time.monotonic()
    )


def test_lines_come_whole_from_text_that_arrives_in_pieces():
    assert list(lines(["one\ntw", "o\n", "", "three\nfour"])) == ["one", "two", "three", "four"]
    assert list(lines(["\n\n"])) == ["", ""]
    assert list(lines([])) == []


def test_output_is_read_as_it_arrives_and_not_a_line_for_each_call(backend):
    hello = MARK + '{"kind": "hello"}'
    backend.modal.pieces = [hello[:20], hello[20:] + "\nprinted\n" + BYE + "\nafter the goodbye\n"]
    seen = []
    assert backend.run(lane(), [], seen.append) == 0
    assert seen == [hello, "printed", BYE]
    assert backend.modal.terminated == 1
    assert "bufsize" not in backend.modal.execs[0]


def test_a_shard_that_ends_without_a_goodbye_has_the_exit_code_of_its_process(backend):
    backend.modal.pieces = ["Traceback\n"]
    assert backend.run(lane(), [], lambda line: None) == 3


def run_all(backend, count):
    codes = []
    threads = [
        threading.Thread(target=lambda n=n: codes.append(backend.run(lane(n), [], lambda line: None)))
        for n in range(count)
    ]
    for thread in threads:
        thread.start()
    return threads, codes


def test_only_so_many_shards_start_at_once(backend):
    backend.modal.pieces = [BYE + "\n"]
    threads, codes = run_all(backend, 20)
    for thread in threads:
        thread.join(10)
    assert codes == [0] * 20
    assert backend.modal.most == 3


def test_a_shard_that_has_started_does_not_keep_others_from_starting(backend):
    backend.modal.pieces = [BYE + "\n"]
    backend.modal.read.clear()
    threads, codes = run_all(backend, 9)
    deadline = time.monotonic() + 10
    while backend.modal.created < 9 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert backend.modal.created == 9 and not codes
    backend.modal.read.set()
    for thread in threads:
        thread.join(10)
    assert codes == [0] * 9


def test_the_time_a_shard_waits_for_its_turn_is_not_its_start_time(backend):
    backend.modal.pieces = [BYE + "\n"]
    waiting = lane()
    with backend.gate, backend.gate, backend.gate:
        thread = threading.Thread(target=backend.run, args=(waiting, [], lambda line: None))
        thread.start()
        time.sleep(0.3)
        asked = waiting.launched
    thread.join(10)
    assert waiting.launched - asked >= 0.3 and waiting.queued >= 0.3 and waiting.created < 0.3


def test_no_shard_starts_once_the_run_has_ended(backend):
    backend.closed = True
    with pytest.raises(RuntimeError, match="the run ended"):
        backend.run(lane(), [], lambda line: None)
    assert backend.modal.created == 0
