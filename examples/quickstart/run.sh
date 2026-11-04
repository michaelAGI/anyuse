#!/bin/sh
# Offline quickstart, end to end (docs/QUICKSTART.md).
#
# Needs: an activated venv with `pip install '.[mcp,registry]'` done (the
# `anyuse` command and `python` on PATH). Needs NO network: the only traffic is
# to the fixture on 127.0.0.1. Writes only under $QS_DIR (a new temp directory
# by default); your real ~/.anyuse is never read or written.
#
#   sh examples/quickstart/run.sh
#
# Exit 0 only if every step behaved as the quickstart says it should.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
PY=${PYTHON:-python}
PORT=${QS_PORT:-8443}
QS_DIR=${QS_DIR:-$(mktemp -d)}
ORIGIN="https://127.0.0.1:$PORT"
NAME=fixture_search
TOOL="127_0_0_1__$NAME"

# Everything AnyUse writes goes to the scratch directory.
export ANYUSE_DATA_DIR="$QS_DIR/data"
export ANYUSE_SECRETS_DIR="$QS_DIR/secrets"
export ANYUSE_API_HOSTS_FILE="$QS_DIR/data/api_hosts.json"
# The compiler refuses non-public destinations; this names the ONE loopback
# origin that is allowed, for this fixture only.
export ANYUSE_LOCAL_FIXTURE_ORIGINS="$ORIGIN"
# Trust the fixture's on-the-spot certificate (made by fixture_site.py).
export SSL_CERT_FILE="$QS_DIR/cert/cert.pem"

FIXTURE_PID=""
stop_fixture() { [ -n "$FIXTURE_PID" ] && kill "$FIXTURE_PID" 2>/dev/null; wait "$FIXTURE_PID" 2>/dev/null || true; FIXTURE_PID=""; }
trap stop_fixture EXIT INT TERM
start_fixture() {
  "$PY" "$HERE/fixture_site.py" --port "$PORT" --certdir "$QS_DIR/cert" --shape "$1" &
  FIXTURE_PID=$!
  i=0; while ! "$PY" -c "import socket; socket.create_connection(('127.0.0.1', $PORT), 1)" 2>/dev/null; do
    i=$((i+1)); [ $i -gt 50 ] && { echo "fixture did not start"; exit 1; }; sleep 0.2; done
}
step() { printf '\n=== %s\n' "$*"; }
fail() { echo "QUICKSTART FAILED: $*"; exit 1; }

# Something already answering on the port would make the fixture fail to start mid-run.
if "$PY" -c "import socket; socket.create_connection(('127.0.0.1', $PORT), 1)" 2>/dev/null; then
  [ "$PORT" = 18443 ] && OTHER=28443 || OTHER=18443
  echo "port $PORT is in use; rerun with QS_PORT=$OTHER sh examples/quickstart/run.sh"
  exit 1
fi

step "0. Where things go: $QS_DIR"
anyuse version

step "1. Start the fixture site (loopback only) and record the search three times"
start_fixture original
"$PY" "$HERE/record_fixture.py" "$ORIGIN" "$QS_DIR/recordings" "$NAME" river stone cloud

step "2. Compile, prove on the two held-out inputs and persist (--yes answers the new-host question)"
anyuse workflow compile "$NAME" --recording "$QS_DIR/recordings" --explain --yes > "$QS_DIR/compile.txt" 2>&1 || { cat "$QS_DIR/compile.txt"; fail "compile errored"; }
cat "$QS_DIR/compile.txt"
grep -qF "Prove against 127.0.0.1:$PORT now? This sends 4 requests to 127.0.0.1:$PORT (robots.txt allows /api/search). yes (--yes)" "$QS_DIR/compile.txt" || fail "expected the new-host question, answered by --yes"
grep -q "Fresh-input proof: PASSED (2/2" "$QS_DIR/compile.txt" || fail "proof did not pass 2/2"
grep -q "Persisted as the preferred implementation" "$QS_DIR/compile.txt" || fail "not persisted"
grep -q "confirmed at compile" "$ANYUSE_API_HOSTS_FILE" || fail "the acknowledgement was not recorded"

step "3. Replay with an input nobody recorded"
anyuse workflow play "$NAME" --param q=maple --no-fallback > "$QS_DIR/play_ok.txt" 2>&1 || { cat "$QS_DIR/play_ok.txt"; fail "replay failed"; }
cat "$QS_DIR/play_ok.txt"
grep -q "0 model calls" "$QS_DIR/play_ok.txt" || fail "replay did not report 0 model calls"
grep -q "maple-" "$QS_DIR/play_ok.txt" || fail "replay returned no maple results"

step "4. Start the MCP server, list the tools, call the typed tool with another new input"
"$PY" "$HERE/mcp_call.py" "$TOOL" '{"q": "harbor"}' "$QS_DIR/mcp_ok.json" > "$QS_DIR/mcp_ok.txt" || { cat "$QS_DIR/mcp_ok.txt"; fail "MCP call did not return ok"; }
cat "$QS_DIR/mcp_ok.txt"
grep -q "harbor-" "$QS_DIR/mcp_ok.txt" || fail "MCP call returned no harbor results"

step "5. Break it: the site changes its API shape (the page looks the same)"
stop_fixture
start_fixture changed
set +e
anyuse workflow play "$NAME" --param q=maple --no-fallback > "$QS_DIR/play_broken.txt" 2>&1
play_rc=$?
set -e
cat "$QS_DIR/play_broken.txt"
[ $play_rc -ne 0 ] || fail "play reported success against a changed API"
grep -q "postcondition failed" "$QS_DIR/play_broken.txt" || fail "play did not say why it failed"

step "6. Call it over MCP: the tool heals itself from the result page (no browser, no model)"
"$PY" "$HERE/mcp_call.py" "$TOOL" '{"q": "harbor"}' "$QS_DIR/mcp_healed.json" > "$QS_DIR/mcp_healed.txt" || { cat "$QS_DIR/mcp_healed.txt"; fail "the MCP call did not heal the tool"; }
sed -n '/tools\/call/,$p' "$QS_DIR/mcp_healed.txt"
"$PY" - "$QS_DIR/mcp_healed.json" <<'PYEOF' || fail "the heal did not do what the quickstart says"
import json, sys
payload = json.load(open(sys.argv[1]))
healed = payload.get("healed") or {}
items = (payload.get("result") or {}).get("items") or []
print(f"healed: served_by={payload.get('served_by')} step={healed.get('localised')} "
      f"requests={healed.get('requests')} model_calls={healed.get('model_calls')} "
      f"browser={healed.get('browser')} items={len(items)}")
sys.exit(0 if (payload.get("ok") and payload.get("served_by") == "api" and healed.get("swapped")
               and healed.get("localised") == "extraction_region" and healed.get("model_calls") == 0
               and healed.get("browser") is False and payload.get("model_calls") == 0
               and items and all(i.startswith("harbor-") for i in items)) else 1)
PYEOF
anyuse workflow play "$NAME" --param q=maple --no-fallback > "$QS_DIR/play_healed.txt" 2>&1 || { cat "$QS_DIR/play_healed.txt"; fail "play failed after the heal"; }
cat "$QS_DIR/play_healed.txt"
grep -q "maple-" "$QS_DIR/play_healed.txt" || fail "play after the heal returned no maple results"

step "7. Break it for good: the site removes the data"
stop_fixture
start_fixture gone
set +e
"$PY" "$HERE/mcp_call.py" "$TOOL" '{"q": "harbor"}' "$QS_DIR/mcp_broken.json" > "$QS_DIR/mcp_broken.txt"
mcp_rc=$?
set -e
sed -n '/tools\/call/,$p' "$QS_DIR/mcp_broken.txt"
[ $mcp_rc -eq 3 ] || fail "MCP call did not answer with an explicit failure (rc=$mcp_rc)"
"$PY" - "$QS_DIR/mcp_broken.json" <<'PYEOF' || fail "the failed MCP call returned items or swapped a request"
import json, sys
payload = json.load(open(sys.argv[1]))
items = [v for v in (payload.get("result") or {}).values() if v]
heal = payload.get("heal") or {}
print(f"failure: ok={payload.get('ok')} items_returned={len(items)} error={payload.get('error')!r}")
print(f"not healed: {heal.get('reason')}")
sys.exit(1 if payload.get("ok") or items or heal.get("swapped") or not heal.get("reason") else 0)
PYEOF

step "QUICKSTART PASSED: compiled, proved on 2 held-out inputs, replayed and called over MCP with new inputs (0 model calls), healed itself from the result page when the API changed (no browser, no model), and failed explicitly with zero items when the data was removed"
echo "This tool lived in $QS_DIR and its site is stopped, so your agent lists no tools yet: compile a real site next (docs/QUICKSTART_COMPILE.md) and the agent sees it."
