#!/bin/zsh
# Run only docs/TEST_ALLOWLIST.txt, with startup guards and a protected-data tripwire.
set -u

GUARD_DIR=${0:A:h}
WT=${GUARD_DIR:h:h}
ALLOWLIST=$WT/docs/TEST_ALLOWLIST.txt
PY=${ANYUSE_TEST_PYTHON:-}
REAL_HOME=$HOME

if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "ABORT: set ANYUSE_TEST_PYTHON to a scratch-venv Python"
  exit 2
fi

cd "$WT" || exit 2
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 2
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || exit 2
[ "$ROOT" = "$WT" ] || { echo "ABORT: runner is not in the checkout root"; exit 7; }
# Optional branch gate: ANYUSE_GUARD_BRANCHES="main feat/x" refuses any other branch.
if [ -n "${ANYUSE_GUARD_BRANCHES:-}" ]; then
  case " $ANYUSE_GUARD_BRANCHES " in *" $BRANCH "*) ;; *) echo "ABORT: wrong branch: $BRANCH"; exit 7;; esac
fi

SCRATCH=${ANYUSE_GUARD_SCRATCH:-$(mktemp -d "${TMPDIR:-/tmp}/anyuse-guard.XXXXXX")}
mkdir -p "$SCRATCH/home/.anyuse"

# A bare `from anyuse... import ...`
# resolves through whatever "anyuse"-named directory comes FIRST on
# sys.path/PYTHONPATH. ${WT:h} below is this checkout's PARENT directory —
# needed so a test file's own per-file pkgroot trick can still put an
# alternate resolution ahead of it, and so anything genuinely meant to reach
# a sibling package still can — but when this checkout is not itself named
# `anyuse` (a linked worktree such as `anyuse-desktop` or `anyuse-unified`),
# and a checkout that IS named `anyuse` happens to sit next to it on disk,
# ${WT:h} contains a live, independently-changing "anyuse" directory that
# would otherwise shadow THIS checkout for every test file that lacks its
# own pkgroot symlink (D9 was fixed file-by-file on the desktop branch, not
# for the whole suite). A single symlink here, placed AHEAD of ${WT:h},
# fixes it once for every test file: `import anyuse` always resolves to
# $WT first, regardless of this checkout's own directory name or of what a
# sibling worktree currently contains.
mkdir -p "$SCRATCH/pkgroot"
ln -sfn "$WT" "$SCRATCH/pkgroot/anyuse"
export PYTHONPATH="$SCRATCH/pkgroot:$GUARD_DIR:${WT:h}:$WT"
export PYTHONDONTWRITEBYTECODE=1
export PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring
unset ANYUSE_LIVE_OVERLAY_TEST ANYUSE_LIVE_SCREENSHOT_TEST

# Prove sitecustomize loaded and each startup guard is effective before tests.
"$PY" -c "import sitecustomize; assert sitecustomize.MARK == 'DESKTOP GUARD'" || exit 3
probe=$("$PY" -c "import AppKit" 2>&1)
echo "$probe" | grep -q "DESKTOP GUARD" || { echo "ABORT: desktop import guard inactive"; exit 3; }
probe=$("$PY" -c "import subprocess; subprocess.run(['/usr/sbin/screencapture','-x'])" 2>&1)
echo "$probe" | grep -q "DESKTOP GUARD" || { echo "ABORT: spawn guard inactive"; exit 3; }
probe=$("$PY" -c "import socket; socket.create_connection(('127.0.0.1', 9))" 2>&1)
echo "$probe" | grep -q "DESKTOP GUARD" || { echo "ABORT: network guard inactive"; exit 3; }
"$PY" -c "
try:
 import keyring, keyring.backends.null
 assert isinstance(keyring.get_keyring(), keyring.backends.null.Keyring)
except ModuleNotFoundError:
 pass" || { echo "ABORT: keyring is not null"; exit 4; }
"$PY" "$GUARD_DIR/tripwire.py" --selftest || exit 5

SNAP=$(mktemp "$SCRATCH/tripwire.XXXXXX")
HOME=$REAL_HOME "$PY" "$GUARD_DIR/tripwire.py" snapshot "$SNAP" || exit 6

export HOME=$SCRATCH/home
export XDG_CONFIG_HOME=$HOME/.config XDG_DATA_HOME=$HOME/.local/share XDG_CACHE_HOME=$HOME/.cache
export ANYUSE_DATA_DIR=$HOME/.anyuse ANYUSE_SECRETS_DIR=$HOME/.anyuse
export ANYUSE_VAULT_KEYFILE=$HOME/.anyuse/entity_vault.keys
export ANYUSE_API_HOSTS_FILE=$HOME/.anyuse/api_hosts.json
export ANYUSE_BLOCKED_HOSTS_FILE=$HOME/.anyuse/api_blocked_hosts.json
# No test reaches a model: model checks off, and no OpenJev server, judge, confirmer or Laya
# directory from the caller's environment. A test that needs one sets it, or fakes it.
export ANYUSE_MODEL_CHECKS=none
unset ANYUSE_OPENJEV_URL ANYUSE_JUDGE ANYUSE_CONFIRMER ANYUSE_CONFIRMER_LAYA_DIR

pass=0
fail=0
matched=0
while read -r file; do
  case "$file" in \#*|"") continue;; esac
  if [ $# -gt 0 ]; then
    selected=0
    for requested in "$@"; do [ "$file" = "$requested" ] && selected=1; done
    [ $selected -eq 0 ] && continue
  fi
  matched=$((matched + 1))
  # each file runs as a script; without the pytest entry point it would exit 0 having run nothing
  if ! grep -q '^if __name__ == "__main__":' "$file"; then
    fail=$((fail + 1)); echo "FAIL $file: no 'if __name__ == \"__main__\":' pytest entry point; its tests would not run"
    continue
  fi
  output=$("$PY" "$file" 2>&1)
  rc=$?
  if echo "$output" | grep -q "DESKTOP GUARD"; then
    fail=$((fail + 1)); echo "GUARD-TRIPPED $file"
  elif [ $rc -eq 0 ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1)); echo "FAIL $file"; echo "$output"
  fi
done < "$ALLOWLIST"

if [ $# -gt 0 ] && [ $matched -ne $# ]; then
  echo "ABORT: every requested file must appear in docs/TEST_ALLOWLIST.txt"
  fail=$((fail + 1))
fi
HOME=$REAL_HOME "$PY" "$GUARD_DIR/tripwire.py" check "$SNAP"
tripwire_rc=$?
echo "=== ALLOWLISTED: $pass ok, $fail failing ==="
[ $tripwire_rc -eq 0 ] || exit 9
[ $fail -eq 0 ] || exit 1
