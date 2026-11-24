from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ._common import BOOT

PACKAGE = Path(__file__).parent
REMOTE_ROOT = "/root/project"
REMOTE_LIB = "/opt/pytest-modal"
IGNORE = [".git", ".venv", "**/__pycache__", "**/.pytest_cache", "**/*.pyc", "**/node_modules"]


class Local:
    def __init__(self, config):
        self.root = str(config.rootpath)
        self.processes = []

    def run(self, shard, args, emit) -> int:
        command = [sys.executable, "-c", BOOT, str(PACKAGE.parent), *args, "--rootdir", self.root, *shard.paths]
        process = subprocess.Popen(
            command, cwd=self.root, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        self.processes.append(process)
        shard.where = f"pid {process.pid}"
        process.stdin.write("\n".join(shard.ids))
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

        self.modal = modal
        self.app = modal.App.lookup(config.getoption("modal_app"), create_if_missing=True)
        image = config.hook.pytest_modal_image(config=config) or default_image(modal)
        self.image = image.add_local_dir(PACKAGE, f"{REMOTE_LIB}/pytest_modal", ignore=["__pycache__"]).add_local_dir(
            config.rootpath, REMOTE_ROOT, ignore=IGNORE
        )
        self.options = {"timeout": 1800, **(config.hook.pytest_modal_sandbox(config=config) or {})}
        self.sandboxes = []

    def run(self, shard, args, emit) -> int:
        box = self.modal.Sandbox.create(
            "sleep", "infinity", app=self.app, image=self.image, workdir=REMOTE_ROOT, **self.options
        )
        self.sandboxes.append(box)
        shard.where = box.object_id
        command = ["python", "-c", BOOT, REMOTE_LIB, *args, "--rootdir", REMOTE_ROOT, *shard.paths]
        process = box.exec("sh", "-c", 'exec "$@" 2>&1', "sh", *command, bufsize=1)
        process.stdin.write("\n".join(shard.ids))
        process.stdin.write_eof()
        process.stdin.drain()
        for line in process.stdout:
            emit(line)
        code = process.wait()
        box.terminate()
        return code

    def close(self):
        for box in self.sandboxes:
            box.terminate()


def default_image(modal):
    version = "{}.{}".format(*sys.version_info)
    return modal.Image.debian_slim(python_version=version).pip_install(f"pytest=={pytest.__version__}")


BACKENDS = {"local": Local, "modal": Modal}
