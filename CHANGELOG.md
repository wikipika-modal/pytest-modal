# Changelog

What changed in each release of pytest-modal, newest first. While the version starts with 0, any release
may change options and behaviour.

## 0.0.1 (not yet released)

The first public alpha.

- `pytest --modal` splits a session's tests into shards, runs each shard in a Modal sandbox and reports
  through pytest's own hooks, so the terminal output, the exit code, `-k`, `-m`, `-x`, `--lf`,
  `--junitxml`, `--cov` and the warnings summary behave as in a plain run.
- The sandboxes get the project by upload, with a default image that mirrors the packages of the local
  environment, from a snapshot of the sandbox pytest runs in, or from a sandbox or image named by id
  (`--modal-from`).
- `--modal-shards=auto` sizes the run from the time each test took before; `--modal-group-by=test`
  splits inside files; `modal_isolate` and `xdist_group` keep tests together. The times are kept in
  `.pytest_cache` and in a Modal Dict, for machines that keep no cache between runs.
- With `-s`, what a test wrote is shown when the test finishes. `--pdb` and `--trace` are refused.
- Each run is one app, whose id is printed when it starts; its sandboxes are tagged and its disk snapshot
  is deleted when it ends.
- `--modal-collect=remote` lists the tests where they run, so the controller imports no test code.
- The controller signs in before it imports the project and keeps the token out of the shards.
- A sandbox that dies fails the test it was running and the rest of its shard runs elsewhere; Modal stops
  the sandboxes of a controller that died.
- `--modal-backend=local` runs the shards as local subprocesses, with no Modal account.
- Requires Python 3.10 to 3.15, pytest 8.0 or later and modal 1.6.2 or later.
