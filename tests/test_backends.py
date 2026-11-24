"""The Modal backend against a stand-in for the Modal client: what it asks of a sandbox, and in what order."""

import time
from types import SimpleNamespace

import pytest

from pytest_modal._common import BYE, MARK
from pytest_modal.backends import Modal, lines
from pytest_modal.plan import Shard


class Process:
    def __init__(self, pieces):
        self.stdout = pieces
        self.stdin = SimpleNamespace(write=lambda text: None, write_eof=lambda: None, drain=lambda: None)

    def wait(self):
        return 3


class Box:
    object_id = "sb-stand-in"

    def __init__(self, client):
        self.client = client

    def exec(self, *args, **options):
        self.client.execs.append(options)
        return Process(self.client.pieces)

    def terminate(self):
        self.client.terminated += 1


class Client:
    """Stands where the `modal` module does."""

    def __init__(self, pieces=()):
        self.pieces = pieces
        self.execs = []
        self.terminated = 0
        self.Sandbox = SimpleNamespace(create=lambda *args, **options: Box(self))


@pytest.fixture
def backend():
    made = object.__new__(Modal)
    made.modal = Client()
    made.app = made.image = None
    made.options, made.tags, made.env = {}, {}, {}
    made.command, made.cwd = ["python"], "/"
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
