import json

import pytest

from pytest_modal._common import project


def repository(folder, url):
    (folder / ".git").mkdir(parents=True)
    (folder / ".git/config").write_text(f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n')


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/someone/thing.git",
        "git@github.com:someone/thing.git",
        "https://user:secret@github.com/someone/thing",
        "ssh://git@github.com/someone/thing.git/",
    ],
)
def test_checkouts_of_one_repository_share_a_name_without_its_credentials(tmp_path, url):
    repository(tmp_path, url)
    assert project(tmp_path) == "github.com/someone/thing"


def test_a_root_inside_a_repository_adds_its_path(tmp_path):
    repository(tmp_path, "https://github.com/someone/thing.git")
    (tmp_path / "packages/one").mkdir(parents=True)
    assert project(tmp_path / "packages/one") == "github.com/someone/thing/packages/one"


def test_a_directory_outside_a_repository_has_its_own_name(tmp_path):
    assert project(tmp_path / "alone") == "alone"


@pytest.mark.parametrize("collect", ["local", "remote"])
def test_a_run_of_the_whole_suite_forgets_tests_that_are_gone(pytester, sharded, collect):
    def kept():
        return set(json.loads((pytester.path / ".pytest_cache/v/modal/durations").read_text())) - {"shard start"}

    pytester.makepyfile(test_a="def test_one(): pass\ndef test_two(): pass")
    sharded(f"--modal-collect={collect}")
    assert kept() == {"test_a.py::test_one", "test_a.py::test_two"}
    pytester.makepyfile(test_a="def test_one(): pass\ndef test_three(): pass")
    # A run of part of the suite cannot tell a test that is gone from one it was not asked for.
    sharded(f"--modal-collect={collect}", "-k", "one")
    assert kept() == {"test_a.py::test_one", "test_a.py::test_two"}
    sharded(f"--modal-collect={collect}", "test_a.py")
    assert kept() == {"test_a.py::test_one", "test_a.py::test_two", "test_a.py::test_three"}
    sharded(f"--modal-collect={collect}")
    assert kept() == {"test_a.py::test_one", "test_a.py::test_three"}
