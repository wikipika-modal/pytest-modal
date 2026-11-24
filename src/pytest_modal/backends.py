from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from ._common import BOOT

PACKAGE = Path(__file__).parent
REMOTE_LIB = "/opt/pytest-modal"
IGNORE = [".git", ".venv", "**/__pycache__", "**/*.pyc", "**/node_modules"]
SITE = "/usr/local/lib/python{}/site-packages"
NOT_MIRRORED = {"pytest-modal", "pip"}


def library() -> str:
    """A directory holding this package and nothing else, for a shard's interpreter to import it from."""
    folder = tempfile.mkdtemp(prefix="pytest-modal-")
    shutil.copytree(PACKAGE, Path(folder, "pytest_modal"), ignore=shutil.ignore_patterns("__pycache__"))
    return folder


def from_disk(config) -> bool:
    """Whether shards start from a snapshot of this machine's disk, which only a Modal sandbox can take."""
    source = config.getoption("modal_from")
    inside = "MODAL_SANDBOX_ID" in os.environ
    if source == "disk" and not inside:
        raise pytest.UsageError("--modal-from=disk only works inside a Modal sandbox")
    return inside if source == "auto" else source == "disk"


def without_modal(env) -> dict[str, str]:
    """The environment test code gets: this sandbox's identity and Modal credentials stay with the controller."""
    return {key: value for key, value in env.items() if not key.startswith("MODAL_")}


class Local:
    def __init__(self, config, env=None):
        self.command = [config.getini("modal_python") or sys.executable, "-c", BOOT, library()]
        self.cwd = str(config.invocation_params.dir)
        self.env = dict(os.environ) if env is None else env
        self.processes = []

    def run(self, lane, args, emit) -> int:
        process = subprocess.Popen(
            [*self.command, *args],
            cwd=self.cwd,
            env={**self.env, **lane.env},
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(process)
        lane.stop = process.kill
        lane.shard.where = f"pid {process.pid}"
        process.stdin.write("\n".join(lane.shard.ids))
        process.stdin.close()
        for line in process.stdout:
            emit(line)
        return process.wait()

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()


class Modal:
    def __init__(self, config):
        import modal

        disk = from_disk(config)
        self.modal = modal
        self.stack = contextlib.ExitStack()
        # An ephemeral app: Modal stops it, and the sandboxes in it, when this process stops heartbeating.
        self.app = modal.App(config.getoption("modal_app"))
        self.stack.enter_context(self.app.run())
        self.options = {
            "timeout": int(config.getini("modal_timeout")),
            **(config.hook.pytest_modal_sandbox(config=config) or {}),
        }
        root, here = config.rootpath, config.invocation_params.dir
        if disk:
            self.command = [config.getini("modal_python") or sys.executable, "-c", BOOT, library()]
            self.env = without_modal(os.environ)
            self.cwd = str(here)
            self.image = modal.Sandbox.from_id(os.environ["MODAL_SANDBOX_ID"]).snapshot_filesystem(
                timeout=300, ttl=3600
            )
        else:
            image = config.hook.pytest_modal_image(config=config) or mirror(modal, root, self.stack)
            # Built here, once, so that a build that fails is one error and not one for each shard.
            self.image = (
                image.add_local_dir(PACKAGE, f"{REMOTE_LIB}/pytest_modal", ignore=["__pycache__"])
                .add_local_dir(root, str(root), ignore=IGNORE)
                .build(self.app)
            )
            self.command = [config.getini("modal_python") or "python", "-c", BOOT, REMOTE_LIB]
            self.env = {}
            self.cwd = str(here if here.is_relative_to(root) else root)

    def run(self, lane, args, emit) -> int:
        box = self.modal.Sandbox.create("sleep", "infinity", app=self.app, image=self.image, **self.options)
        lane.stop = box.terminate
        lane.shard.where = box.object_id
        process = box.exec(
            "sh",
            "-c",
            'exec "$@" 2>&1',
            "sh",
            *self.command,
            *args,
            workdir=self.cwd,
            env={**self.env, **lane.env},
            bufsize=1,
        )
        process.stdin.write("\n".join(lane.shard.ids))
        process.stdin.write_eof()
        process.stdin.drain()
        for line in process.stdout:
            emit(line)
        code = process.wait()
        box.terminate()
        return code

    def close(self):
        self.stack.close()


def environment(folders=None):
    """What an image needs to match this environment: a pin for each package an index has and, for each
    package installed from a path, its files in site-packages and the source an editable install points at."""
    pins, files, sources = {}, {}, []
    folders = folders or sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
    for dist in importlib.metadata.distributions(path=folders):
        name = re.sub(r"[-_.]+", "-", dist.metadata["Name"] or "").lower()
        if not name or name in NOT_MIRRORED or name in pins:
            continue
        origin = json.loads(dist.read_text("direct_url.json") or "null")
        if origin is None:
            pins[name] = f"{name}=={dist.version}"
            continue
        if origin.get("dir_info", {}).get("editable"):
            sources.append(Path(unquote(urlparse(origin["url"]).path)))
        for file in dist.files or []:
            if ".." not in file.parts and "__pycache__" not in file.parts:
                files[file.locate()] = file.as_posix()
    return sorted(pins.values()), files, sources


def mirror(modal, root, stack):
    """debian_slim with this Python minor version and the packages of this environment."""
    version = "{}.{}".format(*sys.version_info)
    pins, files, sources = environment()
    image = modal.Image.debian_slim(python_version=version).uv_pip_install(*pins)
    if files:
        staged = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="pytest-modal-")))
        for local, relative in files.items():
            (staged / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local, staged / relative)
        image = image.add_local_dir(staged, SITE.format(version))
    for source in sources:
        if source.is_dir() and not source.is_relative_to(root):
            image = image.add_local_dir(source, str(source), ignore=IGNORE)
    return image


BACKENDS = {"local": Local, "modal": Modal}
