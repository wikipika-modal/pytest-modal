# pytest-modal

Run a pytest session's tests across [Modal](https://modal.com) sandboxes by adding `--modal` to the
command you already run. A prototype: see "Not done".

```
pytest --modal                         # as many sandboxes as shorten the run
pytest --modal --modal-shards=8
pytest --modal --modal-group-by=test   # split inside files too
pytest --modal --modal-backend=local   # subprocesses instead of sandboxes, needs no Modal account
```

The terminal output, the exit code, `-k`, `-x` and `--junitxml` behave as in a plain run.

## How it works

1. The local pytest process collects as usual and runs no tests.
2. It splits the collected tests into shards of similar expected time. Durations come from earlier runs
   (`.pytest_cache`). A file stays in one shard unless `--modal-group-by=test`; tests sharing an
   `xdist_group` mark always stay together.
3. Each shard is one sandbox: the image plus the project directory. It runs pytest on its test ids and
   writes every test report to stdout as a JSON line.
4. The local process feeds those reports to pytest's own report hooks, so the terminal reporter, junitxml
   and other reporting plugins see them as if the tests had run locally.
5. A test with no result (its sandbox died, or the sandbox did not collect it) is reported as an error.

## The image

By default tests run in `debian_slim` with the local Python minor version and the local pytest version.
A project sets its own in `conftest.py`:

```python
def pytest_modal_image(config):
    import modal

    return modal.Image.debian_slim(python_version="3.12").pip_install("pytest==9.2.0", "numpy")


def pytest_modal_sandbox(config):
    return {"gpu": "T4"}  # keyword arguments for modal.Sandbox.create
```

Keep `import modal` inside the hook: the sandbox loads `conftest.py` too and has no Modal client.

## Example

`pytest example --modal` runs 28 tests that hold 24.2 s of test time. Measured on 2026-11-24 from a cloud
VM, default sandboxes, one run each:

| Shards | Wall |
| --- | --- |
| 5 (one per file), image not yet built | 14.3 s |
| 5 (one per file) | 7.8 s |
| 25 (`--modal-group-by=test`) | 4.7 s |

## Layout

The usual shape of a pytest plugin:

- `pyproject.toml` registers `pytest_modal.plugin` under the `pytest11` entry point, which is how pytest
  finds an installed plugin.
- `src/pytest_modal/plugin.py`: options and the controller (hooks on the local process).
- `src/pytest_modal/worker.py`: the hooks loaded inside each shard. Needs only pytest.
- `src/pytest_modal/plan.py`: the shard plan. `backends.py`: sandboxes or subprocesses.
- `src/pytest_modal/hookspecs.py`: the two hooks a project can implement.
- `tests/` drives real pytest runs through the `pytester` fixture with the local backend.

`uv run pytest` runs the tests; `uv run ruff check` and `uv run ruff format` lint.

## Not done

- Collection is local, so the tests' imports must work on the machine that runs `pytest --modal`.
- No start from a snapshot of the current disk; the project directory is uploaded.
- Coverage data is not merged. Session-scoped fixtures run once per shard.
- A lost sandbox is not retried. A controller that is killed outright leaves its sandboxes until their
  30 minute timeout.
- Options are passed to the shards unchanged, including ones that name local paths.
- One thread per shard; the sandbox runs `python`.
