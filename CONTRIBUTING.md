# Contributing

Thanks for looking. AnyUse is alpha software; issues with a concrete repro are
the most useful contribution.

## Setup

```sh
python3 -m venv /tmp/anyuse-testenv
/tmp/anyuse-testenv/bin/pip install -e '.[test]'
```

The repository root is the `anyuse` package (see the note in `pyproject.toml`).

## Tests

Run tests only through the guarded runner. It blocks desktop, network and
process-spawn entry points, redirects AnyUse data to scratch and checks a
tripwire on your real data:

```sh
ANYUSE_TEST_PYTHON=/tmp/anyuse-testenv/bin/python zsh tools/guarded_tests/run_allowed.sh
```

Tests live in `tests/`. Add a new test file to `docs/TEST_ALLOWLIST.txt` as `tests/test_name.py`. Tests that can only run inside a
macOS guest VM go to `docs/TEST_ALLOWLIST_VM.txt`. Never run a bare `pytest` or a
live-test flag on a desktop you use.

## Claims and numbers

A number in the README or docs needs its denominator (see `docs/EVIDENCE.md`).
"Proved" means a tool passed on held-out values that were not used to build or
select it; anything weaker is "checked".
