import json
from pathlib import Path

import pytest
from modal.file_pattern_matcher import FilePatternMatcher

from pytest_modal.backends import IGNORE, environment


def test_the_packages_of_this_environment_are_pinned():
    pins, _, _ = environment()
    assert f"pytest=={pytest.__version__}" in pins
    assert not [pin for pin in pins if pin.startswith(("pytest-modal==", "pip=="))]


def test_a_package_installed_from_a_path_is_carried_as_its_files(tmp_path):
    def installed(name, version, **files):
        info = tmp_path / f"{name}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        for file, text in files.items():
            (info / file).write_text(text)

    source = tmp_path / "checkout"
    record = "__editable__.thing-1.0.pth,,\nthing-1.0.dist-info/METADATA,,\n../../bin/thing,,\n__pycache__/x.pyc,,\n"
    origin = json.dumps({"url": source.as_uri(), "dir_info": {"editable": True}})
    installed("thing", "1.0", RECORD=record, **{"direct_url.json": origin})
    installed("Other_Thing", "2.0")
    (tmp_path / "__editable__.thing-1.0.pth").write_text(str(source))

    pins, files, sources = environment([str(tmp_path)])
    assert pins == ["other-thing==2.0"]
    assert files == {
        tmp_path / "__editable__.thing-1.0.pth": "__editable__.thing-1.0.pth",
        tmp_path / "thing-1.0.dist-info/METADATA": "thing-1.0.dist-info/METADATA",
    }
    assert sources == [source]


def test_the_kept_weights_stay_off_the_upload():
    ignored = FilePatternMatcher(*IGNORE)
    assert ignored(Path(".pytest_cache/v/modal/durations"))
    assert not ignored(Path(".pytest_cache/v/cache/lastfailed"))
