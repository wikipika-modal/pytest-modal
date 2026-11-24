# pytest-modal

Run a pytest session's tests across [Modal](https://modal.com) sandboxes by adding `--modal` to the
command you already run. An alpha: it works end to end, and its options and behaviour may still change.

## Quickstart

You need Python 3.10 or later, a project whose tests run with `pytest`, and a
[Modal account](https://modal.com/signup).

```
pip install git+https://github.com/wikipika-modal/pytest-modal   # into the environment you run pytest from
modal setup                 # once: opens a browser and stores a token on this machine
pytest --modal
```

Once the package is on PyPI the first line becomes `pip install pytest-modal`.

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
modal: app ap-...
....................                                                     [100%]
modal: 4 shards, 7.1s wall, 20.0s of tests, start 5.7s, shards up in 1.7s (slowest 1.8s), app ap-...
modal: test_b.py took 5.0s of the 7.1s; --modal-group-by=test would split its tests over sandboxes
============================= 20 passed in 13.03s ==============================
```

The last two `modal:` lines are the run in brief: four sandboxes ran 20.0 s of tests in 7.1 s, after 5.7 s
spent on the image (20.8 s for an environment none of whose packages Modal had seen), and one file of
five tests is as long as the whole run. The image is kept, so the next run of the same project ends with
`20 passed in 7.74s`, where plain `pytest` takes 20.03 s. Sandboxes are billed as any other use of Modal
is.

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
  token, and the controller needs none of the project: this plugin and the same pytest version are
  enough.

`--modal-from=upload|disk|ID` overrides the choice.

### How many sandboxes

No number of sandboxes finishes before the heaviest group (a file, by default) does. `--modal-shards=auto`
fills shards to half that group's weight, and to no less than a shard was measured to cost besides its
tests (a sandbox and an interpreter to start, files to collect), up to `--modal-max-shards`. A number given
to `--modal-shards` is used as given, however large; shards start 128 at a time, the heaviest first, and
the summary line says how long the last one waited. Weights are the wall time each test cost its shard in
earlier runs; a test never seen weighs the median. They are kept in `.pytest_cache` and, for machines that
keep no cache between runs, in a Modal Dict named `pytest-modal` in the environment the run uses. The entry
is named after where the repository was cloned from (`github.com/you/project`, without credentials), so
checkouts on different machines share it; outside a repository, after the root directory, and
`modal_project` names it otherwise. A run of the whole suite replaces what was kept; a run of part of it
adds to it.

### What a run leaves on Modal

- One app per run, named `pytest-modal` (`--modal-app`). It is ephemeral, which is what lets Modal stop the
  sandboxes of a controller that died, and like the app of a `modal run` it stays listed as stopped. Its
  id is printed when the app starts (`modal: app ap-...`) and again in the summary line;
  `modal app stop ap-...` ends a run from elsewhere.
- Sandboxes tagged `pytest-modal=<app name>` and `pytest-modal-shard=<modalN or collect>`, besides the tags
  your `pytest_modal_sandbox` hook returns: `modal.Sandbox.list(app_id="ap-...", tags={"pytest-modal-shard":
  "modal3"})` is the sandbox of one shard.
- The disk snapshot of a run is deleted when the run ends, and expires after `modal_timeout` if the
  controller died first.
- The Dict of weights; an entry expires after a week without use.

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

## Listing the tests

- `--modal-collect=local` (default): the controller collects as pytest always does, so the tests' imports
  must work where it runs.
- `--modal-collect=remote`: the interpreter that runs the tests lists them, in a sandbox, or in a
  subprocess while the disk snapshot is taken when the shards start from this disk. The controller loads no
  `conftest.py` and imports no test module, and can be a different interpreter from the one in the shards
  (`modal_python`). Options a `conftest.py` defines go through `--modal-args="--runslow"`.

## Configuration

Options: `--modal-shards`, `--modal-max-shards` (1024, or 64 local processes), `--modal-group-by=file|test`,
`--modal-collect=local|remote`, `--modal-from=auto|upload|disk|ID`, `--modal-workdir`, `--modal-retries` (2),
`--modal-args`, `--modal-backend=modal|local`, `--modal-app`.

Ini values:

| Name | Meaning |
| --- | --- |
| `modal_isolate` | Each line: patterns for test files that run together in a sandbox of their own |
| `modal_python` | Interpreter the shards run |
| `modal_project` | Name the weights are kept under on Modal |
| `modal_conf` | File with the hooks below (`modal_conf.py`) |
| `modal_timeout` | Seconds a sandbox may live (3600) |
| `modal_start_timeout` | Seconds a shard may take to start before it is replaced (300); see below |

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
| Modal has no room for another sandbox (a limit on how many the workspace runs) | The shard waits, using no retry, and starts when a sandbox of the run ends |
| A sandbox does not start within `modal_start_timeout`, or its first start takes ten times as long as most of the run's and more than 15 s | It is replaced |
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

[mpmath](https://github.com/mpmath/mpmath)'s suite (2,765 tests with its doctests; pure Python; needs
hypothesis, pexpect, ipython and pytest-timeout), with no configuration for this plugin. Plain pytest takes
560 s on the same cloud VM and 576 s in a sandbox. One run each:

| Where the shards get the project | Run | Shards | Wall |
| --- | --- | --- | --- |
| Upload | first: no weights, image built (11 s) | 4 | 210 s |
| Upload | later: kept weights | 9 | 156 s |
| Upload | `--modal-group-by=test` | 42 | 40 s |
| Disk, pytest inside a runner sandbox | first | 4 | 232 s |
| Disk | later | 8 | 192 s |
| Disk | `--modal-group-by=test` | 28 | 61 s |
| `--modal-from=<sandbox id>`, pytest outside it | `--modal-group-by=test` | 64 | 44 s |

In each row every test has the outcome it has in a plain run (the junit files compared test by test), also
with `--modal-collect=remote`, `--cov`, `-k`, `-x`, `--lf` and a failing test. With files kept whole the
run is as long as the suite's longest file, 136 s here, which is why `--modal-group-by=test` is four times
faster. Coverage came to the same 21,845 statements; 5 lines differed, all reached only by tests that draw
unseeded random numbers.

The same suite with `--modal-group-by=test` and the shard count given, up to a sandbox for each test. One
test of 44 s bounds every one of these runs (31 to 53 s), so the columns are what the count changes. Upload;
a disk snapshot gives much the same.

| Shards | A shard is up in (median) | The last one is up after | Controller CPU | Sandbox-seconds |
| --- | --- | --- | --- | --- |
| 64 | 1.3 s | 4.3 s | 2.9 s | 1,158 |
| 128 | 1.3 s | 2.7 s | 3.4 s | 1,424 |
| 256 | 1.4 s | 3.7 s | 4.7 s | 2,001 |
| 512 | 1.6 s | 5.2 s | 6.8 s | 2,603 |
| 1,024 | 1.8 s | 13.9 s | 12.0 s | 4,643 |
| 2,765 (one per test) | 2.0 s | 30.4 s | 30.8 s | 9,699 |

Every run reports what a plain run does, test by test, with no shard restarted and nothing refused by
Modal. The controller starts about 80 shards a second on one core, so past a few hundred the later ones
wait for their turn. A shard costs 3 to 5 s besides its tests, which is why sandbox-seconds grow with the
count. The controller keeps a thread and about 0.4 MB for each shard: 1.1 GB at 2,765.

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
- The controller is one process with a thread per shard and starts about 80 shards a second. Starting a
  thousand in a second or two would take sandboxes driven from several processes.
- On a first run, with no weights, the auto shard count counts tests: a file holding half the tests gets
  half the weight whatever it costs, and with `--modal-group-by=test` it asks for a sandbox for each test,
  up to `--modal-max-shards`. Later runs plan from what the tests took.
- A shard collects the whole of each file it has a test from.
- With `--modal-from=ID` the controller has none of the project, so `--cov` reports need the sources at the
  same paths where it runs, and options a plugin of the project defines go through `--modal-args`.
- The default image cannot mirror what an index does not have for Linux; such environments need the
  `pytest_modal_image` hook.
- The controller is tested on Linux and on macOS (arm64), not on Windows; sandboxes are Linux.
