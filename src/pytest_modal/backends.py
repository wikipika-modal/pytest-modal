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
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from ._common import BOOT, BYE

PACKAGE = Path(__file__).parent
REMOTE_LIB = "/opt/pytest-modal"
# The weights are left out because every run rewrites them and no shard reads them.
IGNORE = [".git", ".venv", "**/__pycache__", "**/*.pyc", "**/node_modules", "**/.pytest_cache/v/modal"]
SITE = "/usr/local/lib/python{}/site-packages"
NOT_MIRRORED = {"pytest-modal", "pip"}
STORE = "pytest-modal"
# Shards that may be starting at once. The Modal client does its work on one thread, which this many keep
# busy: past it every shard waited for all the others, 6 s at 1,024 and 19 s at 2,765 against 1.5 s.
STARTING = 128
CREDENTIALS = (
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "MODAL_OAUTH_REFRESH_TOKEN",
    "MODAL_OAUTH_CLIENT_ID",
    "MODAL_OAUTH_CLIENT_SECRET",
    "MODAL_OAUTH_JWT_KEY",
)
HOW_TO_SIGN_IN = (
    "On your own machine: `modal token new`. In CI: set MODAL_TOKEN_ID and MODAL_TOKEN_SECRET, best those of a "
    "service user that can write to one environment. Inside a Modal sandbox, which has no identity of its own: "
    "give the two variables to this pytest command alone, or run pytest outside the sandbox with "
    "--modal-from=<sandbox id>, which needs no token inside it."
)


def library() -> str:
    """A directory holding this package and nothing else, for a shard's interpreter to import it from."""
    folder = tempfile.mkdtemp(prefix="pytest-modal-")
    shutil.copytree(PACKAGE, Path(folder, "pytest_modal"), ignore=shutil.ignore_patterns("__pycache__"))
    return folder


def origin(source: str) -> str:
    """Where the shards get the project, from the value of --modal-from: "upload", "disk" (a snapshot of this
    machine's disk, which only a Modal sandbox can take), or the id of a sandbox or image that holds it."""
    inside = "MODAL_SANDBOX_ID" in os.environ
    if source == "disk" and not inside:
        raise pytest.UsageError("--modal-from=disk only works inside a Modal sandbox")
    if source == "auto":
        return "disk" if inside else "upload"
    return source


def sign_in() -> None:
    """Open the Modal client, then take its credentials out of this process's environment: the client keeps
    them, and project code imported here, or anything it starts, does not inherit them."""
    import modal

    try:
        modal.Client.from_env()
    except modal.exception.AuthError as error:
        raise pytest.UsageError(f"pytest --modal could not sign in to Modal: {error}\n{HOW_TO_SIGN_IN}") from error
    except modal.exception.Error as error:
        raise pytest.UsageError(f"pytest --modal could not reach Modal: {error}") from error
    for name in CREDENTIALS:
        os.environ.pop(name, None)


def without_modal(env) -> dict[str, str]:
    """The environment test code gets: this sandbox's identity and Modal credentials stay with the controller."""
    return {key: value for key, value in env.items() if not key.startswith("MODAL_")}


class Local:
    name = ""

    def __init__(self, config, env=None):
        self.library = library()
        self.command = [config.getini("modal_python") or sys.executable, "-c", BOOT, self.library]
        self.cwd = str(config.invocation_params.dir)
        self.env = dict(os.environ) if env is None else env
        self.processes = []

    def run(self, lane, args, emit) -> int:
        # Shards share this directory, where pytest-cov erases and combines every .coverage.* it finds: each
        # keeps its data elsewhere, and only the controller writes here.
        private = {"COVERAGE_FILE": os.path.join(self.library, f"coverage-{id(lane)}")}
        process = subprocess.Popen(
            [*self.command, *args],
            cwd=self.cwd,
            env={**self.env, **lane.env, **private},
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

    def full(self, outcome) -> bool:
        return False

    def weights(self, project) -> dict:
        return {}

    def keep(self, project, weights) -> None:
        pass

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
        shutil.rmtree(self.library, ignore_errors=True)


class Modal:
    def __init__(self, config):
        import modal

        self.modal = modal
        # An ephemeral app: Modal stops it, and the sandboxes in it, when this process stops heartbeating.
        self.app = modal.App(config.getoption("modal_app"))
        self.options = {
            "timeout": int(config.getini("modal_timeout")),
            **(config.hook.pytest_modal_sandbox(config=config) or {}),
        }
        self.tags = {"pytest-modal": self.app.name, **self.options.pop("tags", {})}
        self.snapshots = []
        self.gate = threading.BoundedSemaphore(STARTING)
        self.closed = False
        # The app stops with this block if anything in it fails, and the snapshots go once it has.
        with contextlib.ExitStack() as stack:
            stack.callback(self.forget)
            stack.enter_context(self.app.run())
            self.name = f"app {self.app.app_id}"
            self.prepare(config, origin(config.getoption("modal_from")), stack)
            self.stack = stack.pop_all()

    def snapshot(self, sandbox):
        """The disk of a sandbox as an image. It is deleted when the run ends, or by Modal once no sandbox of
        the run can still be alive, should this process not live to do it."""
        image = self.modal.Sandbox.from_id(sandbox).snapshot_filesystem(timeout=300, ttl=self.options["timeout"])
        self.snapshots.append(image.object_id)
        return image

    def forget(self):
        for image in self.snapshots:
            with contextlib.suppress(self.modal.exception.Error):
                self.modal.experimental.image_delete(image)

    def full(self, outcome) -> bool:
        """Whether a shard ended this way because Modal has no room for another sandbox: a limit on how many
        run at once, or on how fast they are created, which the client has by then waited on and retried."""
        return isinstance(outcome, self.modal.exception.ResourceExhaustedError)

    def weights(self, project) -> dict:
        try:
            return self.modal.Dict.from_name(STORE, create_if_missing=True).get(project, {})
        except self.modal.exception.Error:
            return {}

    def keep(self, project, weights) -> None:
        with contextlib.suppress(self.modal.exception.Error):
            self.modal.Dict.from_name(STORE, create_if_missing=True).put(project, weights)

    def prepare(self, config, source, stack):
        modal = self.modal
        root, here = config.rootpath, config.invocation_params.dir
        if source == "disk":
            folder = library()
            stack.callback(shutil.rmtree, folder, ignore_errors=True)
            self.command = [config.getini("modal_python") or sys.executable, "-c", BOOT, folder]
            self.env = without_modal(os.environ)
            self.cwd = str(here)
            self.image = self.snapshot(os.environ["MODAL_SANDBOX_ID"])
            return
        self.command = [config.getini("modal_python") or "python", "-c", BOOT, REMOTE_LIB]
        self.env = {}
        if source == "upload":
            image = config.hook.pytest_modal_image(config=config) or mirror(modal, root, stack)
            image = image.add_local_dir(root, str(root), ignore=IGNORE)
            self.cwd = str(here if here.is_relative_to(root) else root)
        else:
            if source.startswith("sb-"):
                source = self.snapshot(source).object_id
            # Looked up by id: the image object a snapshot returns takes no files on top.
            image = modal.Image.from_id(source)
            self.cwd = config.getoption("modal_workdir")
        # Built here, once, so that a build that fails is one error and not one for each shard.
        self.image = image.add_local_dir(PACKAGE, f"{REMOTE_LIB}/pytest_modal", ignore=["__pycache__"]).build(self.app)

    def run(self, lane, args, emit) -> int:
        tags = {**self.tags, "pytest-modal-shard": lane.gateway.id if lane.mode == "run" else lane.mode}
        with self.gate:
            if self.closed:
                raise RuntimeError("the run ended before this shard's turn to start")
            # The shard's clock starts with its turn, not with the wait for it.
            lane.queued, lane.launched = time.monotonic() - lane.launched, time.monotonic()
            box = self.modal.Sandbox.create(
                "sleep", "infinity", app=self.app, image=self.image, tags=tags, **self.options
            )
            lane.stop = box.terminate
            lane.shard.where = box.object_id
            lane.created = time.monotonic() - lane.launched
            process = box.exec(
                "sh",
                "-c",
                'exec "$@" 2>&1',
                "sh",
                *self.command,
                *args,
                workdir=self.cwd,
                env={**self.env, **lane.env},
            )
            process.stdin.write("\n".join(lane.shard.ids))
            process.stdin.write_eof()
            process.stdin.drain()
        code = None
        for line in lines(process.stdout):
            emit(line)
            # After its goodbye a shard only has an interpreter to shut down, which nothing waits for.
            if BYE in line:
                code = 0
                break
        code = process.wait() if code is None else code
        box.terminate()
        return code

    def close(self):
        self.closed = True
        self.stack.close()


def lines(pieces):
    """The lines of text that arrives in pieces. Asked for a line at a time, the Modal client makes a trip to
    its event loop for every line, which at a thousand shards is most of what the controller does."""
    rest = ""
    for piece in pieces:
        *whole, rest = (rest + piece).split("\n")
        yield from whole
    if rest:
        yield rest


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
