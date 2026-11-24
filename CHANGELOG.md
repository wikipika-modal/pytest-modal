# Changelog

What changed in each release of pytest-modal, newest first. While the version starts with 0, any release
may change options and behaviour.

## 0.0.1

The first public alpha.

- `pytest --modal` splits a session's tests into shards, runs each shard in a Modal sandbox and reports
  through pytest's own hooks, so the terminal output, the exit code, `-k`, `-m`, `-x`, `--lf`,
  `--junitxml`, `--cov` and the warnings summary behave as in a plain run.
- The sandboxes get the project by upload, with a default image that mirrors the packages of the local
  environment, from a snapshot of the sandbox pytest runs in, or from a sandbox or image named by id
  (`--modal-from`).
- `--modal-shards=auto` sizes the run from the time each test took before; `--modal-group-by=test`
  splits inside files; `modal_isolate` and `xdist_group` keep tests together. The times are kept in
  `.pytest_cache` and in a Modal Dict, for machines that keep no cache between runs; there they are named
  after the repository the project was cloned from (`modal_project` names them otherwise).
- A number given to `--modal-shards` is used as given, up to a sandbox for each test; auto asks for at
  most 1,024 (`--modal-max-shards`) and fills a shard to no less than one costs to start. Shards start 128
  at a time, the heaviest first. One that Modal has no room for waits for another of the run to finish,
  and one far slower to start than the rest is replaced.
- With `-s`, what a test wrote is shown when the test finishes. `--pdb` and `--trace` are refused.
- `--junitxml`, `--basetemp` and `--cov-report` name files on the machine pytest was started on, and are
  honoured there, once.
- The summary gives the run in a line and, when one file took most of it, names the file and the option
  that splits it.
- A run stops with a usage error, before collecting, when it has no Modal credentials, and when the shards
  run another pytest version than the controller.
- Each run is one app, whose id is printed when it starts; its sandboxes are tagged and its disk snapshot
  is deleted when it ends.
- `--modal-collect=remote` lists the tests where they run, so the controller imports no test code.
- The controller signs in before it imports the project and keeps the token out of the shards.
- A sandbox that dies fails the test it was running and the rest of its shard runs elsewhere; Modal stops
  the sandboxes of a controller that died.
- `--modal-backend=local` runs the shards as local subprocesses, with no Modal account.
- Requires Python 3.10 to 3.15, pytest 8.0 or later and modal 1.6.2 or later.
