# Quickstart: offline, five minutes

No network, account, model key or browser needed. A small package-search site
ships with the repository (`examples/quickstart/fixture_site.py`) and runs on
`127.0.0.1` only. You record a search, compile it to an HTTP request, prove it
on inputs it has not seen, replay it and call it as an MCP tool. Then you change
the site's API and watch the tool heal itself, and remove the data and watch it
fail explicitly.

## Install

```sh
git clone https://github.com/michaelAGI/anyuse && cd anyuse
python3 -m venv .venv && . .venv/bin/activate      # Python >= 3.11
pip install -e '.[mcp,registry]'
```

`pip install -e .` alone gives the compile path (`httpx`, `click`). The `mcp`
extra adds the MCP server; `registry` adds `cryptography`, which the fixture
uses for its loopback certificate. Works on Linux and macOS.

## Run it

```sh
sh examples/quickstart/run.sh
```

The local site listens on port 8443; if that port is taken, pick another with
`QS_PORT=18443 sh examples/quickstart/run.sh`.

The script runs every step below and exits non-zero if one does not behave as
described. Everything goes to a new temporary directory (`QS_DIR`, printed
first): your real `~/.anyuse` is never read or written. The script also sets
`ANYUSE_LOCAL_FIXTURE_ORIGINS` (AnyUse refuses non-public destinations unless
an origin is named here) and `SSL_CERT_FILE` (to trust the fixture's
certificate).

### 1. Record the search three times

```
recorded 'river'    -> recordings/fixture_search.rec.json  (6 results on screen)
recorded 'stone'    -> recordings/fixture_search.fresh1.json  (6 results on screen)
recorded 'cloud'    -> recordings/fixture_search.fresh2.json  (6 results on screen)
```

The compiler only looks at `river`. `stone` and `cloud` are held out and only
used to check the result. On a real site you record in a headless browser or
with `anyuse attach` ([QUICKSTART_COMPILE.md](QUICKSTART_COMPILE.md),
[ATTACH.md](ATTACH.md)); here `examples/quickstart/record_fixture.py` writes the
same recording format.

### 2. Compile and prove

```
$ anyuse workflow compile fixture_search --recording recordings --explain
Prove against 127.0.0.1:8443 now? This sends 4 requests to 127.0.0.1:8443 (robots.txt allows /api/search). [Y/n]: y
  GET https://127.0.0.1:8443/api/search?q={q}
Fresh-input proof: PASSED (2/2 cases) — passed on 2 fresh value(s) not present in the recording (coverage >= 0.8, UI order preserved)
  [ok  ] 'stone': coverage=1.00 order_ok=True ui=6 api=6
  [ok  ] 'cloud': coverage=1.00 order_ok=True ui=6 api=6
✓ Persisted as the preferred implementation
```

Compiling is local: AnyUse finds the request the page made for `river` and turns
it into a template. The proof then replays that template for `stone` and `cloud`.
For a new host, AnyUse reads its `robots.txt`, then asks before the proof replays
the template against it; `--yes` answers for scripts, and the script passes it.

"Proved" means: replayed for values that were never in the recording, and for
each, at least 80 % of the result labels came back in the same order. It does
not mean every input behaves this way.

### 3. Replay with a new input

```
$ anyuse workflow play fixture_search --param q=maple --no-fallback
✓ fixture_search: served by api (1_proved_request), 13 ms, 0 model calls
  items: ['maple-kit', 'maple-lite', 'maple-sdk', 'maple-tools', 'maple-core', 'maple-cli']
```

### 4. Call it over MCP

`examples/quickstart/mcp_call.py` does what an MCP client does: it starts
`python -m anyuse.mcpserver` over stdio, lists the tools and calls one.

```
tools/call 127_0_0_1__fixture_search {"q": "harbor"}:
  ok: True
  served_by: api
  model_calls: 0
  result: {'items': ['harbor-cli', 'harbor-kit', 'harbor-lite', 'harbor-sdk', 'harbor-tools', 'harbor-core']}
```

The default profile lists proved tools (`<host>__<workflow>`, the host's dots as
underscores), read-only helpers and `agent_run` (the agent harness). Raw desktop
actuators are not listed. To connect Claude Code or Codex, see
[INSTALL_FOR_AGENTS.md](INSTALL_FOR_AGENTS.md). A registered agent lists the tools in
your own data directory (`~/.anyuse`). The quickstart's
tool lived in a throwaway folder and its site stops with the script, so the agent
lists none until you compile a real site ([QUICKSTART_COMPILE.md](QUICKSTART_COMPILE.md)).

### 5. Break it

The script restarts the fixture with a different JSON shape
(`packages[*].title` instead of `results[*].name`). The page looks the same.

```
$ anyuse workflow play fixture_search --param q=maple --no-fallback
✗ fixture_search: postcondition failed: path 'results' is NoneType, expected a list
```

The tool fails with zero items. It never returns an empty list that looks like
"no matches", and never stale data. Without `--no-fallback`, `play` tries the next
steps in order: the browser replay (when the tool has one and a container browser is
configured), then the agent (when a model and a container browser are configured),
then a heal from the tool's result page (next section), then it stops and gives the
reason to you. It lists each step it skipped and why. A web tool with a browser
replay recompiles itself when the replay answers
([COMPILE.md](COMPILE.md#automatic-recompilation)).

### 6. Watch it heal

The script calls the tool over MCP again, with the default fall-back on.

```
tools/call 127_0_0_1__fixture_search {"q": "harbor"}:
  ok: True
  served_by: api
  model_calls: 0
  healed: swapped=True, rung=step_repair, localised=extraction_region, relearned=['extraction'], requests=7, model_calls=0, browser=False
  result: {'items': ['harbor-cli', 'harbor-kit', 'harbor-lite', 'harbor-sdk', 'harbor-tools', 'harbor-core']}
```

The call heals the tool by itself. There is no browser and no model here, but the
page is rendered by the server, so a plain HTTP read of it shows the results. AnyUse
reads the page for the tool's last good answer (`harbor` from step 4) and checks
that it still lists those names. It finds where they moved in the new JSON
(`packages[*].title`) and proves the repaired request again on two inputs it did
not use for the repair. Only then does it swap the request in and answer the call.
That took 7 requests to the site. The old record is kept as
`compiled_api.previous.json`. The next `play` is served by the repaired request.
Details: [COMPILE.md](COMPILE.md#healing-without-a-browser).

### 7. Break it for good

The script restarts the fixture once more: the API now answers `410 Gone` and the
page shows a notice instead of results.

```
tools/call 127_0_0_1__fixture_search {"q": "harbor"}:
  ok: False
  error: non-2xx status 410
  heal: swapped=False, reason=it failed again soon after a heal, so it is not healed again before ...; if the site changed for good, record the search again and recompile it
  result: None
```

The tool fails with zero items and says why. It was healed seconds ago, so AnyUse
does not try again for 30 minutes: a site that keeps changing cannot keep a tool
healing. Had it tried, the page would have shown no result list to heal from, and
the call would have failed the same way. To use the tool again, record the search
again and recompile it.

## Desktop (macOS)

The desktop path clicks and types in a real macOS session, so it is not part of
this quickstart.

* **Install:** `pip install '.[desktop]'` (macOS only).
* **Permissions:** grant Accessibility to the terminal or app that runs AnyUse
  (System Settings → Privacy & Security → Accessibility); Screen Recording only
  for the visual tier.
* **Where:** use a throwaway macOS VM, not the machine you work on
  ([VM_RUNBOOK.md](VM_RUNBOOK.md)).
* **What compiles:** a desktop recording compiles into one of a few operation
  classes (`arithmetic`, `file_write`, `directory_op`, `file_search`,
  `read_value`, Blender `app_script`), each proved on inputs the recording did
  not use. Anything else stays a checked replay or is refused. Details:
  [DESKTOP_TOOLS.md](DESKTOP_TOOLS.md).

## Next

* [QUICKSTART_COMPILE.md](QUICKSTART_COMPILE.md): the same path on a real public site
* [INSTALL_FOR_AGENTS.md](INSTALL_FOR_AGENTS.md): use the tools from Claude Code or Codex
* [EVIDENCE.md](EVIDENCE.md): measured results, with sample sizes
