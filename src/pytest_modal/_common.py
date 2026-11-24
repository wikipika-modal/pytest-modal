from __future__ import annotations

import argparse
import configparser
import re
from pathlib import Path

MARK = "@@pytest-modal@@ "
BYE = MARK + '{"kind": "bye"}'
BOOT = "import sys; sys.path.insert(0, sys.argv.pop(1)); import pytest; sys.exit(pytest.main(sys.argv[1:]))"


def source(value: str) -> str:
    if value in ("auto", "upload", "disk") or elsewhere(value):
        return value
    raise argparse.ArgumentTypeError("expected auto, upload, disk, a sandbox id (sb-...) or an image id (im-...)")


def elsewhere(source: str) -> bool:
    """Whether the project is held by a sandbox or image this process names and has no files of."""
    return source.startswith(("sb-", "im-"))


def project(root: Path) -> str:
    """A name for the project at `root` that its other checkouts share and other projects do not: where its
    repository was cloned from, without the scheme and any credentials, then the path to `root` inside it.
    A directory outside a repository has only its own name."""
    for folder in (root, *root.parents):
        settings = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            settings.read(folder / ".git" / "config")
            url = settings['remote "origin"']["url"]
        except (KeyError, OSError, configparser.Error):
            continue
        origin = re.sub(r"^\w+://([^@/]*@)?|^[^@/:]+@|(\.git)?/*$", "", url).replace(":", "/")
        return "/".join([origin, *root.relative_to(folder).parts])
    return root.name


def add_options(parser) -> None:
    if getattr(parser, "_modal_options", False):
        return
    parser._modal_options = True
    group = parser.getgroup("modal", "run tests in Modal sandboxes")
    group.addoption("--modal", action="store_true", help="run the collected tests in Modal sandboxes")
    group.addoption("--modal-shards", default="auto", metavar="N", help="number of sandboxes, or auto (default)")
    group.addoption(
        "--modal-max-shards",
        type=int,
        metavar="N",
        help="upper bound for auto (default 1024 sandboxes, or 64 processes with --modal-backend=local)",
    )
    group.addoption("--modal-group-by", choices=("file", "test"), default="file", help="what stays in one sandbox")
    group.addoption(
        "--modal-collect",
        choices=("local", "remote"),
        default="local",
        help="remote: collect in a sandbox; this process then imports no test code and loads no conftest.py "
        "(always the case with --modal-from=ID)",
    )
    group.addoption(
        "--modal-from",
        type=source,
        default="auto",
        metavar="SOURCE",
        help="upload: send the project; disk: snapshot this sandbox's disk (auto: disk inside a sandbox, else "
        "upload); or the id of a sandbox (sb-...) to snapshot or of an image (im-...) that holds the project",
    )
    group.addoption("--modal-workdir", metavar="DIR", help="directory the shards run in, with --modal-from=ID")
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
    parser.addini(
        "modal_project",
        "name the weights are kept under on Modal (default: where the repository was cloned from, else the "
        "root directory's name)",
    )
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
