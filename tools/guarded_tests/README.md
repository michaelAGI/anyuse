# Guarded tests

Run repository tests only through `run_allowed.sh`. It asserts the checkout and
branch, loads `sitecustomize.py` at interpreter startup, blocks desktop/keychain/
browser/network entry points, clears both live-test flags, redirects all AnyUse
data to scratch, turns model checks off (`ANYUSE_MODEL_CHECKS=none`, and
`ANYUSE_OPENJEV_URL`, `ANYUSE_JUDGE`, `ANYUSE_CONFIRMER` and
`ANYUSE_CONFIRMER_LAYA_DIR` unset), and compares a stat-only tripwire before and
after the run.

Point it at a Python executable in a scratch venv containing the project's test
dependencies:

```sh
ANYUSE_TEST_PYTHON=/private/tmp/anyuse-testenv/bin/python \
  tools/guarded_tests/run_allowed.sh
```

Pass one or more exact allowlisted filenames for a targeted run. Never invoke a
bare `pytest`, a test glob, or either live-test flag on a working desktop.

The one network exception is an offline release-style fixture: a test may set
`ANYUSE_LOCAL_FIXTURE_ORIGINS` to its exact ephemeral
`http://127.0.0.1:<port>` origin. The guard permits only that loopback port and
continues to reject every other connection; the runner's startup probe runs
with no exception configured.
