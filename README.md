# pytest-modal

Run a pytest session's tests across [Modal](https://modal.com) sandboxes by adding `--modal` to the
command you already run. A prototype.

```
pytest --modal                         # as many sandboxes as shorten the run
pytest --modal --modal-shards=8
pytest --modal --modal-group-by=test   # split inside files too
pytest --modal --modal-collect=remote  # this process imports no test code
pytest --modal --modal-backend=local   # subprocesses instead of sandboxes, needs no Modal account
```

The terminal output, the exit code, `-k`, `-m`, `-x`, `--lf`, `--junitxml`, `--cov` and the warnings
summary behave as in a plain run.

## How it works

1. The pytest process you start is the controller. It lists the tests and runs none.
2. It splits the tests into shards. A file stays in one shard unless `--modal-group-by=test`; tests sharing
   an `xdist_group` mark stay together; a shard runs its tests in the order a plain run would.
3. Each shard is one sandbox. It runs pytest on its tests and writes every report to stdout as a JSON line.
4. The controller feeds those reports to pytest's own report hooks, so the terminal reporter, junitxml,
   pytest-cov and other reporting plugins see them as if the tests had run in the controller.

### Where the sandboxes get the project

- **Upload** (the default outside Modal): an image plus the project directory at the same absolute path.
  The default image is `debian_slim` with this Python minor version and the packages of the environment
  pytest runs in, at the same versions; a package installed from a path (`pip install -e .`) goes up as
  it is installed. Return another image from `pytest_modal_image` when that does not fit: system
  packages, a private index, packages that have no Linux build.
- **Disk** (the default inside a Modal sandbox): a snapshot of the sandbox the controller runs in. A runner
  sandbox installs and builds as usual, then `pytest --modal` fans the tests out to copies of its disk, with
  the same paths, working directory and environment. Variables starting with `MODAL_`, which include the
  controller's credentials, are not passed on to test code.

`--modal-from=upload|disk` overrides the choice.

### How many sandboxes

No number of sandboxes finishes before the heaviest group (a file, by default) does. `--modal-shards=auto`
fills shards to half that group's weight, up to `--modal-max-shards`. Weights are the wall time each test
cost its shard in earlier runs, kept in `.pytest_cache`; a test never seen weighs the median.

## Listing the tests

- `--modal-collect=local` (default): the controller collects as pytest always does, so the tests' imports
  must work where it runs.
- `--modal-collect=remote`: the interpreter that runs the tests lists them, in a sandbox, or in a
  subprocess while the disk snapshot is taken when the shards start from this disk. The controller loads no
  `conftest.py` and imports no test module, and can be a different interpreter from the one in the shards
  (`modal_python`). Options a `conftest.py` defines go through `--modal-args="--runslow"`.

## Configuration

Options: `--modal-shards`, `--modal-max-shards` (64), `--modal-group-by=file|test`,
`--modal-collect=local|remote`, `--modal-from=auto|upload|disk`, `--modal-retries` (2), `--modal-args`,
`--modal-backend=modal|local`, `--modal-app`.

Ini values:

| Name | Meaning |
| --- | --- |
| `modal_isolate` | Each line: patterns for test files that run together in a sandbox of their own |
| `modal_python` | Interpreter the shards run |
| `modal_conf` | File with the hooks below (`modal_conf.py`) |
| `modal_timeout` | Seconds a sandbox may live (3600) |
| `modal_start_timeout` | Seconds a shard may take to start before it is replaced (300) |

`modal_isolate` is for order dependencies a suite has and does not declare: files that pass only in a
process that also loaded another file go on one line, and a file that fails after certain others goes on a
line by itself.

Hooks go in `modal_conf.py` next to the pytest configuration. Only the controller loads it, so it can
import `modal`:

```python
import modal


def pytest_modal_image(config):  # upload only
    return modal.Image.debian_slim(python_version="3.12").pip_install("pytest==9.2.0", "numpy")


def pytest_modal_sandbox(config):  # keyword arguments for modal.Sandbox.create
    return {"cpu": (0.125, 8), "gpu": "T4"}
```

## What a shard changes for the tests

- Session-scoped fixtures set up and tear down once per sandbox. Module-scoped fixtures run once per file
  while files stay whole. `PYTEST_MODAL_SHARD` and `PYTEST_MODAL_SHARDS` tell a fixture where it is.
- Coverage: with `--cov`, each shard's data comes back and pytest-cov combines it; the report and
  `--cov-fail-under` use the combined result.
- A file skipped as a whole and warnings raised in a sandbox are reported once, as in a plain run.

## When something goes wrong

| Event | Result |
| --- | --- |
| A sandbox dies while running a test | That test fails with the sandbox's last output; the rest of the shard runs in a new sandbox |
| A sandbox dies or fails to start before running anything | The shard is retried, `--modal-retries` times |
| A sandbox does not start within `modal_start_timeout` | It is replaced |
| A shard does not collect a test the controller listed | That test is an error |
| The controller is interrupted or terminated | It stops its sandboxes |
| The controller is killed outright | Modal stops the run's app and its sandboxes when the heartbeat lapses (about 3 minutes) |
| Modal cannot be reached, or the options do not fit | A usage error, no traceback |

## Measured

`pytest example --modal`: 28 tests holding 24.2 s of test time, from a cloud VM, one run each.

| Shards | Wall |
| --- | --- |
| 5 (one per file), image not yet built | 14.3 s |
| 5 (one per file) | 7.8 s |
| 25 (`--modal-group-by=test`) | 4.7 s |

pybind11's pytest suite (1,138 to 1,483 tests on each of six interpreters, C++ modules built in a runner
sandbox, shards from its disk, `--modal-collect=remote`, default shard count): every row reports the same
counts as a plain run. With a warm build the six rows finish in 15 to 21 s; a row's pytest step takes 5 to
19 s where a plain run takes 14 to 129 s.

## Development

- `pyproject.toml` registers `pytest_modal.plugin` under the `pytest11` entry point.
- `src/pytest_modal/plugin.py`: the controller. `worker.py`: the hooks loaded in each shard, needing only
  pytest. `plan.py`: the shard plan. `backends.py`: sandboxes or subprocesses. `hookspecs.py`: the hooks.
- `uv run pytest` drives real pytest runs through `pytester` with the local backend.
- `PYTEST_MODAL_LIVE=1 uv run pytest tests/live` runs against Modal with the active profile: upload,
  disk snapshots on both sandbox runtimes, a lost sandbox and a killed controller. The disk tests pass the
  profile's token to the one `pytest` command inside their runner sandbox.

## Not done

- Output a test prints is shown when the test fails, not live; `-s` and `--pdb` do not work.
- One thread per shard in the controller.
- The auto shard count ignores what a sandbox costs to start.
