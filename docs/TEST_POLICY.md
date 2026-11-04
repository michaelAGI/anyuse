# Running the tests without touching your machine

AnyUse automates a desktop, so a careless test run could draw on your screen,
photograph it or reach your keychain. The suite is built so that it cannot.

## The two rules

1. **Run the allowlist**, `docs/TEST_ALLOWLIST.txt`, not a glob. A test file joins it
   only after it has been executed once under the guard and passed: every desktop
   import in this codebase is lazy, so reading imports cannot tell whether a file
   would reach the desktop.
2. **Always use the guarded runner**:

   ```sh
   python3 -m venv /tmp/anyuse-testenv && /tmp/anyuse-testenv/bin/pip install -e '.[test]'
   ANYUSE_TEST_PYTHON=/tmp/anyuse-testenv/bin/python zsh tools/guarded_tests/run_allowed.sh
   ```

   Pass one or more allowlisted paths (`tests/test_x.py`) for a targeted run.

Tests that only run inside a macOS guest VM are listed separately in
`docs/TEST_ALLOWLIST_VM.txt`.

## What the guard does

The runner (`tools/guarded_tests/`) loads a startup hook into every test process
that makes desktop access impossible rather than unlikely:

- importing a GUI, Accessibility, screen-capture, audio or keychain framework raises
  `ImportError`;
- `screencapture`, `osascript` and `open` cannot be spawned
  through `subprocess`, `os.system`, `os.popen` or `os.exec*`;
- socket connections are refused, except an exact loopback fixture origin a test
  declares in `ANYUSE_LOCAL_FIXTURE_ORIGINS`;
- `keyring` is pinned to the null backend, so nothing reaches the login keychain.

Before the tests, the runner proves the guard is active (`import AppKit` must fail,
spawning `screencapture` must fail, a socket connection must fail, `keyring` must be
null). It points `HOME`, `ANYUSE_DATA_DIR`, `ANYUSE_SECRETS_DIR` and
`ANYUSE_VAULT_KEYFILE` at a scratch directory, clears the live-test flags, turns
model checks off (`ANYUSE_MODEL_CHECKS=none`, with `ANYUSE_OPENJEV_URL`, `ANYUSE_JUDGE`,
`ANYUSE_CONFIRMER` and `ANYUSE_CONFIRMER_LAYA_DIR` unset, so no test reaches a model
unless it fakes one), and fails
the run if a stat-only tripwire sees any change in your real AnyUse data directory,
the keychain or the extra paths in `ANYUSE_TRIPWIRE_DIRS`. `ANYUSE_GUARD_BRANCHES`
optionally limits it to listed branches.

## Writing a test

- Give desktop code an injectable fake (`runner=`, `spawn=`, `ocr_fn=`, `transport=`,
  `backend=`, `crypto=`, …) instead of calling the real framework.
- Make `import anyuse` resolve to this checkout: the tests use a temporary directory
  holding a symlink `anyuse -> <repo root>`, never the parent directory (a sibling
  folder named `anyuse` would be imported instead).
- Every allowlisted file needs an `if __name__ == "__main__":` block; the runner
  executes each file as a script and counts exit code 0 as a pass.

## The foreground interlock's test flag

Every real input primitive asks `execution/foreground_interlock.py` first and refuses
while a person can see the session. A test that drives fake Accessibility, Quartz or
pyautogui objects with `ANYUSE_DRIVE_ENABLED=1` sets
`ANYUSE_INTERLOCK_TEST_BYPASS=1`. The flag is honoured only when the runner's guard is
loaded in the process (so nothing real can be sent); outside the runner it is ignored
with an error line.

## Opt-in live tests

The screenshot test touches the machine only when explicitly requested:

```sh
ANYUSE_LIVE_SCREENSHOT_TEST=1 python tests/test_context_screenshot.py # photographs your screen
```

The screenshot path writes the capture to a temp file, reads it, deletes it in a
`finally` block and returns the bytes; the module opens no network connection. Tests
assert both.
