# pytest-modal

Run a pytest session's tests across [Modal](https://modal.com) sandboxes by adding `--modal` to the
command you already run. A prototype.

## Quickstart

You need Python 3.10 or later, a project whose tests run with `pytest`, and a
[Modal account](https://modal.com/signup).

```
pip install pytest-modal    # into the environment you run pytest from
modal setup                 # once: opens a browser and stores a token on this machine
pytest --modal
```

The first run builds an image holding the packages of that environment, then starts the sandboxes and
reports each test as it finishes. For a project of 20 one-second tests in four files:

```
$ pytest --modal
============================= test session starts ==============================
platform linux -- Python 3.12.16, pytest-9.2.0, pluggy-1.7.0
modal: modal backend
rootdir: /home/you/project
plugins: modal-0.0.1, anyio-4.16.0
collected 20 items
modal: preparing the image the shards start from
....................                                                     [100%]
modal: 4 shards, 6.9s wall, 20.0s of tests, start 20.8s, shards up in 1.6s (slowest 1.8s), app ap-...
============================= 20 passed in 27.97s ==============================
```

The last `modal:` line is the run in brief: four sandboxes ran 20.0 s of tests in 6.9 s, after 20.8 s spent
on the image. The image is kept, so the next run of the same project ends with `20 passed in 8.31s`, where
plain `pytest` takes 20.03 s. Sandboxes are billed as any other use of Modal is.

To try it without an account, `pytest --modal --modal-backend=local` shards over local subprocesses
instead. The same command takes these options, which the sections below explain:

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
  the same paths, working directory and environment, less the variables starting with `MODAL_`.
- **A sandbox or image named by id** (`--modal-from=sb-...` or `im-...`): the controller runs somewhere
  else, snapshots that sandbox (or takes that image) and starts the shards from it, in the directory given
  by `--modal-workdir`. Collection is remote, the sandbox needs pytest and neither this plugin nor a Modal
  token, and the controller needs none of the project: `pip install pytest-modal` and the same pytest
  version are enough.

`--modal-from=upload|disk|ID` overrides the choice.

## Credentials

The controller is the only process that talks to Modal. It needs to create an app, build an image and
create sandboxes: a Contributor in one environment.

- **Your machine**: the profile `modal token new` wrote.
- **CI**: `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` in the job's environment, and `MODAL_ENVIRONMENT`. Use
  a [service user](https://modal.com/docs/guide/service-users) that is Contributor in an environment kept
  for test runs, created with an expiry if it should not live long
  (`modal workspace service-users create NAME --expires-in 90d`), and not a person's token.
- **Inside a Modal sandbox**: a sandbox has no Modal identity of its own. Either pass the two variables to
  the `pytest --modal` command alone (`sandbox.exec(..., secrets=[...])`), or keep the token out of the
  sandbox altogether: run `pytest --modal --modal-from=<sandbox id>` from the process that created it.

The controller signs in before it imports any of the project, then removes the token from its own
environment. Shards never receive it, so tests cannot read it. Code that runs in the same sandbox as the
controller (a `conftest.py` it imports during local collection, a build step that left a process behind) is
not kept from it by this: for code you do not trust, use `--modal-from=<sandbox id>`.

### How many sandboxes

No number of sandboxes finishes before the heaviest group (a file, by default) does. `--modal-shards=auto`
fills shards to half that group's weight, up to `--modal-max-shards`. Weights are the wall time each test
cost its shard in earlier runs; a test never seen weighs the median. They are kept in `.pytest_cache` and,
for machines that keep no cache between runs, in a Modal Dict named `pytest-modal` in the environment the
run uses, under the name of the project's root directory.

### What a run leaves on Modal

- One app per run, named `pytest-modal` (`--modal-app`). It is ephemeral, which is what lets Modal stop the
  sandboxes of a controller that died, and like the app of a `modal run` it stays listed as stopped. The
  summary line gives its id.
- Sandboxes tagged `pytest-modal=<app name>` and `pytest-modal-shard=<modalN or collect>`, besides the tags
  your `pytest_modal_sandbox` hook returns. `modal.Sandbox.list(tags={"pytest-modal": "pytest-modal"})`
  finds what is running.
- The disk snapshot of a run is deleted when the run ends, and expires after `modal_timeout` if the
  controller died first.
- The Dict of weights; an entry expires after a week without use.

## Listing the tests

- `--modal-collect=local` (default): the controller collects as pytest always does, so the tests' imports
  must work where it runs.
- `--modal-collect=remote`: the interpreter that runs the tests lists them, in a sandbox, or in a
  subprocess while the disk snapshot is taken when the shards start from this disk. The controller loads no
  `conftest.py` and imports no test module, and can be a different interpreter from the one in the shards
  (`modal_python`). Options a `conftest.py` defines go through `--modal-args="--runslow"`.

## Configuration

Options: `--modal-shards`, `--modal-max-shards` (64), `--modal-group-by=file|test`,
`--modal-collect=local|remote`, `--modal-from=auto|upload|disk|ID`, `--modal-workdir`, `--modal-retries` (2),
`--modal-args`, `--modal-backend=modal|local`, `--modal-app`.

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
| No Modal credentials, Modal cannot be reached, or the options do not fit | A usage error before anything is collected, no traceback |
| The shards run another pytest version than the controller | A usage error naming both |
| A shard cannot import a file the controller listed | That file's tests are errors; the shard runs its others |

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

- With `-s`, what a test wrote is shown when that test finishes, not while it runs. `--pdb` and `--trace`
  are refused: there is no terminal where the test runs.
- One thread per shard in the controller.
- The auto shard count ignores what a sandbox costs to start.
