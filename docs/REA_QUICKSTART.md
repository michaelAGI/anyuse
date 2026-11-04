# REA harness quickstart

Start with a read-only view of a real app and a saved mapping. Then, if you
want to investigate a new operation, continue to REA/Ghidra and the agent loop.
The first steps require no model key, Ghidra, Frida or macOS consent dialog.

The REA harness is a research preview: it maps operations of native apps that
expose no interface, using [REA](https://github.com/morluto/rea) and Ghidra.
Read [access routes and current limits](REA_ACCESS_ROUTES.md) before choosing an
execution route. REA, Ghidra, Frida and the apps you analyse keep their own
licences.

## Registered actions in computer use

`AgentOrchestrator` accepts `action="rea_plan"` with `steps`, or
`action="rea_action"` with `steps`. Each step contains a registered `name` and
its typed `inputs`; `CatalogStore.execute_plan(steps)` exposes the same sequence
to trusted Python callers. Plans accept 1–32 steps and at most 256 KiB of literal
JSON. Only deterministic registrations are eligible. Result references and live
handles such as `$v0` are refused.

The store preflights every step, freezes catalog hashes and process epochs, then
reopens the ordinary proof gate for every execution. A failure stops the plan
with `completed_steps`, a zero-based `failed_step_index`, and ordered
`executions`. Completed effects remain applied: this is a fail-fast sequence,
without rollback or isolation from other executors. Each step retains its fresh
restoration requirement; a restoration policy may therefore undo state that a
later step expected. System prompt resolution belongs before planning. A prompt
appearing inside the sequence refuses the next primitive without being answered.

`action="rea_observe"` takes a registered `name` and typed `inputs`.
`CatalogStore.observe(name, inputs)` reads the same pinned observer without
dispatching its actor. Currently only registrations advertising
`observation_kind="learned_ax"` or `"learned_svg"` support this operation. SVG
reads retain all typed inputs, including the independent observer declaration
and reference paths. Complete current labels may differ from the reference;
`oracle_matches` reports that comparison without proving an action. The SVG
reader uses bounded DOM reads with zero app evaluations, method calls or getters,
and binds the expected main process, owned endpoint/renderer set and resources
throughout capture. Fixed playback, visual, and arbitrary native/inspector read
profiles still require an admitted read-only contract. Observations are evidence,
without task proof.
Observer and actor source pins remain enforced. Historical proof validation
admits the exact reviewed SVG observer revision `f7168a23712e2a7868bcde686dc5f3cfc90198bf0f9773f0b109f4bae2b84c88`
against the reviewed epoch-bound collector, optionally with the exact pre-cache
mapping dependency. All other pins, paths, case files and retained observations
must match. Stored proofs are unchanged; replay, observation and fresh verification
use only current source pins. Other source changes need new evidence or review.
Execution receipts reuse an existing deterministic `seen` observation as
`state_summary`, without another runtime attach. If restoration ran, its phase
is explicitly `post_action_before_restoration`.

Successful REA actions and plans can be recorded alongside UI steps. Both
workflow replay paths retain literal inputs and frozen catalog hashes; they
recheck proof, process and restoration conditions at execution. REA failure
stops replay without a UI or model alternative. Workflow variables do not
substitute into REA inputs.

`ANYUSE_REA_GUI_CAPTURE` selects `off`, `windows`, `live`, or `auto` (default).
Auto requests live collection for REA mapping effort, window recording when the
task has reached its fallback threshold, and otherwise leaves capture off.
Successful GUI fallback actions retain redacted records in `workflows/rea_candidates`
under the data directory. Inspect one with
`anyuse map rea workflow ui-trace /absolute/trace.json`. Each artifact holds at
most 256 actions and 256 KiB, with at most 64 artifacts retained. Typed text,
key strings, app display names and task text are omitted or redacted.

Live collection requires a trusted passive `differential_probe` injected into
`AgentOrchestrator` or `ExecutionOrchestrator`. There is no default passive native
collector and this setting never attaches instrumentation or invokes app getters.
Without a probe, live requests retain windows with an explicit unavailable status.
Opaque custom actuators also retain windows until they provide an admitted capture
boundary. The built-in `ActionExecutor` starts capture after drive/BOUNDARY admission,
rechecks focus and target guards after setup, and dispatches the GUI action once.
The collector records a contemporaneous baseline, arms across dispatch, drains
afterward and confirms cleanup. Methods must be nonblocking and honor finite
deadlines; the adapter never abandons a timeout thread that might remain attached.
Unconfirmed cleanup disables that injected probe for subsequent runs by its owner.

The live record stores at most eight ranked, pointer-free differential candidates
per action, with historical process-epoch and module-hash context. Raw call rows,
scalar arguments, receiver addresses and full app/module paths are never persisted.
Candidate metadata may be dropped to keep ordinary GUI windows within the artifact
budget. `action_window` is the GUI dispatch span; `collector_window` spans arming
through confirmed release, including final target guards and bounded draining of
asynchronous work. Neither gives individual native-call timestamps or exclusive
causal attribution. Candidates establish no callable interface, ABI, lifetime or
task proof.

## Generic agent workflow (development)

`map rea workflow` connects the existing reasoning driver, provider registry,
independent visual campaign and catalog. Its configuration contains task data,
without app-specific Python callbacks. The caller is an ordinary agent that can
use public computer-use tools. Investigation and public setup are separate
phases: hidden trial values never enter the discovery model's view.

After installing the runtime prerequisites below, freeze the desired effect in
a JSON task specification. For example, replace these placeholders with the
actual task and a configured vision model:

```json
{
  "format": "anyuse.rea-generic-workflow/1",
  "id": "enable_control",
  "task": "Enable the requested visible control",
  "before": "The requested control is visibly disabled",
  "after": "The requested control is visibly enabled",
  "owned": true,
  "restart_allowed": true,
  "observer_model": {
    "provider": "local",
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "your-vision-model",
    "api_key_env": ""
  }
}
```

`owned` and `restart_allowed` authorize the declared test context and normal app
restart; use task-owned documents whose state can be discarded. They do not
authorize permission grants, a forced quit or access to other apps. The default
unparameterized plan has three learning, three fresh and three held-out trials,
one normal restart and two subsequent replays (`--effort rea-fast` synthesizes
a `1/1/1` plan with restart and enables declared-native receiver discovery;
`--trials-learn`, `--trials-fresh`, `--trials-heldout` and
`--require-restart/--no-require-restart` override individual phase minimums).
Parameterized tasks supply `params`, the independent `plan` and `replays`
explicitly. Optional `resources` are exact file/size/SHA-256 pins for the public
setup agent. The existing global drive authorization and desktop guards still
apply.

You can also generate a validated unparameterized specification directly from
the CLI without hand-editing JSON:

```sh
anyuse map rea workflow init /path/to/task.json \
  --task "Enable the requested visible control" \
  --before "The requested control is visibly disabled" \
  --after "The requested control is visibly enabled" \
  --observer-model your-vision-model --effort rea-fast
anyuse map rea workflow begin /path/to/App.app /path/to/task.json \
  --rea "$REA_CLI" --out /new/workflow
anyuse map rea workflow advance /new/workflow
```

`workflow init` defaults to the local Ollama endpoint and `llava`. Selecting
`--observer-provider openai`, `anthropic` or `gemini` uses that provider's presets
for omitted connection and key-environment settings; OpenAI also requires
`--observer-model`. For `openai_compatible`, supply both `--observer-base-url` and
`--observer-model`, plus `--observer-api-key-env` if the endpoint needs a key.
Hosted observers also require `--observer-price-in` and `--observer-price-out`
with their configured USD prices per million tokens. Unknown hosted prices are
rejected before a request; loopback observers retain their free local default.
Explicit observer options override the presets. Specification generation does
not contact the provider or read API-key values.

`advance` uses the ordinary configured reasoning model and the generic
`tool_object_v1` transport. This transport reaches all registered provider
families; `full_native_v1` retains its narrower Objective-C wire contract.
Strict provider schemas cannot express arbitrary nested runtime data, so the
generic transport uses configured JSON-object output or strictly decoded JSON
from a prompted response. Nested requests still go through provider validation.
Reasoning supports local or hosted HTTP models and the tool-free `claude-cli`
adapter, which runs with tools disabled and safe mode. Direct `codex-cli` use in
this driver is refused because that CLI remains an agent with tools. This
restriction does not change other harness paths that use Codex. Closed driver
protocols retain their HTTP-only rule. The separate `observer_model` must support
images; a text reasoning provider does not replace the before/after vision judge.

At `needs_setup`, the caller follows `public_setup` in the JSON result:

```sh
anyuse map rea workflow public-start /new/workflow
# Use ordinary computer-use tools to prepare the declared visible starting state.
anyuse map rea workflow public-finish /new/workflow \
  --evidence /path/to/actual-public-observation.json
```

The observation can be an actual screenshot or a retained public UI trace. It
asserts readiness only: the campaign captures and independently judges the
before and after states itself. Setup must not invoke the native candidate.
Each next trial or replay gets a new setup request. At `needs_restart`, use one
normal owned-app quit/launch with the independent process-restart witness:

```sh
anyuse map rea workflow restart /new/workflow --timeout 30
anyuse map rea workflow context /new/workflow
```

Use `context` after reconnecting too. If `public_handoff` is present, resume that
existing claim and submit its actual observations with `public-finish`; do not
claim or repeat setup automatically. Repeated `advance` calls preserve a pending
request. Only `discovery_incomplete` or `discovery_stopped` can explicitly continue
healthy investigation with its verified memory:

```sh
anyuse map rea workflow advance /new/workflow --continue-discovery
```

The original step/time bounds remain. Failed tools, uncertain native effects,
interrupted actions and failed restarts stop without an automatic retry. A
completed result names the local catalog; its evidence covers the frozen task,
app build and admitted operation.

The generic static-evidence tool retains original data or instruction ranges
and extracts class-local Qt revision-13 metadata from compatible thin plain-arm64
binaries. `qt_code` derives a fixed Ghidra extraction from that metadata, retaining
actual imported pointers, original instructions and decompiled text. Given the
original uint16 table and supported public Qt sources, `qt_selection` assembles
the four pinned static artifacts and runs the existing static validator.
The separately admitted live receiver, primary QObject base and lifetime
contract are still required. Qt dispatch accepts a constructor-observed root or
a fresh getter-borrowed root on the main queue. The getter route requires one
unadjusted primary QObject at offset zero, its pinned vptr layout, no path edges
or guards, and separate source/type/lifetime admission. Getter and slot share
one bounded transaction; the pointer is never saved for replay. This is focused
bridge coverage, not a completed cold Qt discovery/proof campaign.

Prepare the reusable public-source cache once, or select an existing cache:

```sh
anyuse map rea qt-sources
# For a nondefault cache, set ANYUSE_REA_QT_SOURCE_CACHE to its absolute path.
```

This downloads seven source files at the exact supported Qt commit and checks
every expected SHA-256. It executes none of those sources. Discovery only reads
the cache; a missing cache returns a concrete prerequisite. The fixed Ghidra
extractor uses `GHIDRA_INSTALL_DIR`, a private image copy and private tool home,
finite time/output bounds and a 2 GiB free-space reserve. A real Ghidra check on
an inert NOP/RET fixture verified instruction bytes and decompilation; it did
not attach to or execute an app.

For an existing supported typed native path, `build_native_path` can assemble
source admission from registered original headers/source, original-build
observations and a separate reviewer. Freeze `native_path_review` before
`workflow begin`; its `model` is a complete `ModelConfig`, not the abbreviated
observer-model object above. For example, this edits task data only:

```python
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from anyuse.execution.model_adapter import ModelConfig

task_file = Path("/path/to/task.json")
task = json.loads(task_file.read_text())
header = Path("/path/to/authentic-original.h").resolve(strict=True)
raw = header.read_bytes()
task["native_path_review"] = {
    "format": "anyuse.rea-native-path-review-policy/1",
    "mode": "separate_model", "max_calls": 1,
    "model": asdict(ModelConfig("local", "http://127.0.0.1:11434/v1",
                              "your-source-review-model", "", timeout=60,
                              max_tokens=2048, retries=0)),
    "source_pins": [{"role": "header", "file": str(header),
                     "bytes": len(raw), "sha256": sha256(raw).hexdigest()}]
}
task_file.write_text(json.dumps(task, indent=2) + "\n")
```

`begin` copies and registers those exact original bytes; discovery can read the
copy when `build_native_path` is admitted. Roles also include `source`, `dwarf`
and `original_ir`; the role label alone does not establish authentic type or
lifetime evidence. The separate fallible reviewer sees source/recipe claims,
without held-out task answers. It can admit supported source-correlated claims
or return `needs_capability`; decompiled text alone supplies no source authority.
`max_calls: 1` is per builder step, within the existing session bounds. A source
review is separate from runtime validation and the independent task-effect proof.

Optional `restoration` in the task can bind an already captured
[saved-copy/public-state checkpoint](REA_DOCUMENT_RESTORATION.md):

```json
{"restoration":{"checkpoint":{"file":"/absolute/checkpoint.json",
 "bytes":123,"sha256":"replace-with-actual-sha256"},"seconds":30}}
```

This is an addition to the task, using the actual checkpoint pin. The 1–60 second
reserve stays inside the original deadline. Required recovery runs once before
publication, or after a failure whose native cleanup is confirmed; unknown
effects forbid recovery actions. Each subsequent replay captures and restores a
fresh checkpoint. Historical publication recovery never substitutes for that
fresh capture. Recovery covers declared saved copies, public fields and window
geometry, without arbitrary internal-state or unsaved-buffer rollback.

Native ABI hypotheses are separately opt-in with
`"native_contract_mode": "declared"` in the task specification. `declare_native`
then records one native function's proposed types against exact original-code
evidence. It generates JSON, never source code. The record explicitly says
`model_declared_not_independently_reviewed`; it does not claim ABI recovery or
review. The exact entry, constants, receiver recipes, task and source provenance
are checked again before reuse. Existing pointer ownership, lifetime and calling
family restrictions remain, and publication requires the model-judged effect
campaign. The default `attributable` mode retains the existing ABI evidence
requirements. A type hypothesis can be wrong and can crash the task-owned app;
use a disposable test context and treat it as experimental.

For an opaque-pointer API, the existing declared-native route can compose two
declarations: declare a zero-argument pointer getter against its original code,
then use its complete signed actor as a consumer pointer argument's
`value.getter` and declare that consumer against its own original code. The
getter resolves afresh. This source-free composition does not establish receiver
compatibility or lifetime.

For additional binary vptr correlation and an enforced single-use receiver,
source-free C++ receiver experiments use a second explicit setting:

```python
task["native_contract_mode"] = "declared"
task["native_receiver_mode"] = "binary_runtime_hypothesis"
```

For direct `agent begin`, the corresponding options are
`--native-contract-mode declared --native-receiver-mode binary_runtime_hypothesis`.
The generic reasoning driver then exposes `declare_native_receiver`. The
Objective-C-only `full_native_v1` wire schema is unchanged.

The agent first retains `static_evidence` original-code observations beginning
at the measured getter and consumer entries (`unit_bytes: 4`), and an original
data range covering the measured vtable address point (`unit_bytes: 8`). It
declares a zero-argument pointer getter, a scalar-only consumer whose first
argument is `{"type":"pointer","value":{"receiver":"fresh"}}`, the vptr image
and data offset, and three registered citations. Their roles are `getter_code`,
`consumer_code` and `vptr_data`, each selecting `/range` in its actual producing
artifact. The returned complete actor can be proposed through the ordinary
generic workflow; this declaration does not call the app.

Every invocation acquires and consumes the receiver once, without saved
addresses or extra operations. Binary correlation does not establish an object
type or lifetime. Receipts and action cards retain `model_declared` ABI,
`binary_correlated_hypothesis` receiver and `assumed_not_established` lifetime
labels. Keep the independent visual proof/restart/replay workflow; deterministic
publication is refused for this tier. A source-free reasoning-agent discovery in
a compiled C++ VM fixture completed nine prospective visual-effect trials,
witnessed restart, local publication and two fresh-input replays.
All eleven executions retained these evidence labels. The owned fixture used
an explicit debugging entitlement; sources, templates and build receipts stayed
outside discovery evidence.

### Multi-hop and virtual-dispatch C++ receivers

Keep the same two task settings above (or pass `--effort rea-fast` /
`--effort rea-strict`) and use the generic workflow's default
`tool_object_v1` reasoning protocol. It also exposes `declare_native_receiver_v2`
and `correlate_native_trace_v2`; `full_native_v1` remains Objective-C-only. The
reasoning model receives `native_receiver_guide` with data-only syntax examples.
The guide is available only for caller-admitted receiver tools. Replace every
synthetic path, hash, entry, field and parameter name with current-session
observations; an example is not a mapping or a live execution transcript.

A multi-hop / virtual-dispatch declaration has the following parts:

| Field | Supported contract |
| --- | --- |
| `getter` | Zero-argument default-ABI native function returning a pointer |
| `hops` | Zero to three `field_pointer`, `step_getter`, `array_element` or `vector_element` hops; array/vector elements are pointers |
| `consumer` | `dispatch: direct` with `target`, or `dispatch: vtable_slot` with `target_entry` and `vtable_slot_offset`; arg0 is the fresh receiver |
| `vptr` | Original image/data claim, optional measured `subobject_offset` and explicit `pac_mode` |
| `citations` | Ordered `getter_code`, `consumer_code`, `vptr_data` and, for virtual dispatch, `vtable_slot_data`; each selects `/range` in its registered original observation |
| `queue_policy` | `main` only |

The consumer accepts at most seven tail arguments: supported scalars, bounded
UTF-8 strings, buffers or POD structs with scalar fields. Returns are scalar or
void. Receiver adjustment is 8-byte aligned in `[-4096, 4096]`; slot offsets are
8-byte aligned in `[0, 1024]`. Code observations use `original_code` with
`unit_bytes: 4`; vptr and slot data use `original_range` with `unit_bytes: 8`.
These declarations retain hypotheses about ABI and lifetime, not independent
source admission. Arm64e requires `vptr.pac_mode: arm64e_strip` and the same
consumer setting for virtual dispatch; its live execution remains unvalidated.

`correlate_native_trace_v2` accepts already-observed `baseline_calls`,
`actuated_calls` and `observed_getters`, plus optional `target_input_value` and
`module_sha256`. It ranks supplied observations only: it does not collect traces,
perform public setup, attach to the app or create original-code evidence.
Malformed rows are rejected before dispatch by the generic reasoning driver, so
its existing bounded decision-correction loop can request corrected input.
Each input list is limited to 256 rows. Results retain at most 256 candidates
alongside `matched_candidate_count` and `candidates_truncated`.

The session retains a crash blocklist for failed or uncertain native hypotheses
and checks it before reuse. A confirmed zero-call setup/transport failure does
not block a signature; uncertain effects still stop the workflow without an
automatic retry. Reusable recipes reacquire their receiver and check the current
binary and pointer chain. Trace pointer values never become saved receivers.

Keep the independent visual trials, normal restart, publication and fresh
replays. These receiver declarations retain `binary_correlated_hypothesis_v2`
and `assumed_not_established` labels throughout. Current validation covers guarded
workflow integration, runtime fixtures and real compiled-binary preparation.
The earlier completed live C++ campaign used direct getter-to-consumer calls
without intermediate hops or receiver adjustment. A later component check passed
three actual virtual-slot calls on the owned number fixture, including normal
restart and receiver reacquisition. Public AX reads checked the requested number
and local before/after screenshots were retained; every call detached cleanly.
This reused an existing receiver correlation, with zero hops and zero adjustment.
It creates no task proof or catalog entry and does not establish cold discovery
or a model-led proof/replay campaign using the extended receiver contracts.
Live multi-hop, receiver adjustment and arm64e support also remain unvalidated.

The inert workflow tests exercise the real session, promotion and catalog
validators separately from that controlled live measurement.
Completed live measurements and remaining boundaries are listed separately in
[access routes](REA_ACCESS_ROUTES.md).

## 1. Open a Terminal inside a macOS VM

Use a disposable macOS VM with Python 3.11 or newer, and run every command on
this page inside it, including the install. Keep app binaries, analysis tools
and evidence in guest storage. AnyUse does not create or manage the VM.

The `anyuse map rea` commands that execute or analyse refuse to run outside a
VM. To run them on your own Mac anyway, pass `--allow-host` for that one
invocation (`anyuse map rea --allow-host analyze …`); it prints a warning, and
no environment variable can turn it on.

A fresh macOS has no git. Install Apple's Command Line Tools first and confirm
the dialog; they provide git and the compiler REA needs later:

```sh
xcode-select --install
```

The `python3` they include can be older than 3.11. If `python3 --version`
reports less, install a newer Python in the guest before creating the venv.

```sh
git clone https://github.com/michaelAGI/anyuse "$HOME/rea-preview/anyuse"
cd "$HOME/rea-preview/anyuse"
python3 --version
python3 -m venv "$HOME/rea-preview/venv"
. "$HOME/rea-preview/venv/bin/activate"
python -m pip install -e .
anyuse map rea --help
```

The base install needs only `click` and `httpx` and includes no instrumentation
stack or analysis engine. The next two steps are read-only and need no network.

## 2. Find the interfaces a real app already declares

```sh
anyuse map surfaces TextEdit \
  --bundle /System/Applications/TextEdit.app --json
```

This reads TextEdit's bundle metadata. It does not open TextEdit, send input,
attach to its process or invoke a discovered operation. The result lists the
surfaces found in that app build; the count can differ across macOS versions.
An empty result means the scanner found no supported declarations. It does
not mean the application has no internal capabilities.

This is the declared-interface baseline. REA investigation starts where the
available interfaces do not provide the operation you need.

## 3. Inspect a saved mapping without running it

The repository contains a saved Dictionary mapping and a native receiver
candidate example:

```sh
anyuse map rea inspect \
  examples/rea/dictionary-candidate.json \
  --proof examples/rea/dictionary-proof.json
anyuse map rea inspect \
  examples/rea/native-receiver-candidate.json
```

The output includes `receipt_matches_recorded_target` and
`current_app_checked: false`. It checks whether the saved recipe and saved
receipt agree with their recorded app identity. It does not inspect your
current Dictionary, revalidate the original observations or authorize replay.
The guest paths in those JSON files need not exist for inspection. A saved
proof covers the app build it was made on.

## 4. Investigate an operation with REA

This stage needs REA with a ready Ghidra provider inside the guest. One command
installs both, then you load the tools and rerun the readiness check:

```sh
anyuse map rea setup
. "$HOME/anyuse-rea/env.sh"
"$REA_CLI" doctor --provider ghidra --json
```

The setup first checks for git and a compiler (the Command Line Tools; REA's
Ghidra provider needs a compiler) and, if they are missing, prints how to
install them and stops. It then downloads Node, a JDK, Ghidra and REA, checks
each against a pinned SHA-256 or npm integrity value, and installs them into
one folder, `~/anyuse-rea` by default (`--prefix DIR` picks another). Ghidra's
archive ships the decompiler's sources but no Apple silicon binary, so the setup
compiles it with the Command Line Tools' `clang++` (a few minutes). It writes
`env.sh` there, which sets `PATH`, `JAVA_HOME`, `GHIDRA_INSTALL_DIR` and
`REA_CLI`, and runs REA's Ghidra doctor check; the command fails if that check
does not pass. Running it again installs only what is missing.

- Pinned versions: Node 24.22.0, Temurin JDK 21.0.12.1+1, Ghidra 12.1.4 and
  REA (`rea-agents`) 6.7.0.
- The downloads total about 850 MB.
- The pins cover macOS on Apple silicon only; on other machines the setup stops
  before downloading anything.
- It uses no sudo or Homebrew and does not change your shell profile, install
  Frida, register an MCP server or touch any agent configuration.
- REA is resolved into a lockfile and checked against the pinned integrity
  before anything is unpacked, then installed from that lockfile with no npm
  install scripts.
- The folder must be new, empty, or one this setup created; it never writes into
  a folder that holds other files.
- To uninstall, delete the folder.

Like the other `anyuse map rea` commands, the setup refuses to run outside a VM.
To install the toolchain on your own Mac instead, pass `--allow-host`; it then
installs into `~/.anyuse/rea-tools`:

```sh
anyuse map rea --allow-host setup
. "$HOME/.anyuse/rea-tools/env.sh"
```

With REA installed some other way, set `REA_CLI` to its executable and run the
same doctor check. Continue only after the readiness check passes.

Source the environment from the prefix you intend to use; an older `rea` on
`PATH` can select another installation. `"$REA_CLI" --version` reports the
selected version (the current setup pins 6.7.0). GUI and background launchers
must inherit that environment too.

If the Command Line Tools already exist in a portable, nondefault location,
declare that location for this shell before setup or analysis:

```sh
export DEVELOPER_DIR="/absolute/path/to/CommandLineTools"
xcrun --find swiftc
```

Use your actual installed directory. Adding its `usr/bin` to `PATH` alone is
insufficient: REA's macOS provider also invokes `/usr/bin/xcrun swiftc` for its
process-ownership helper. This process-local setting does not change the
system's developer-tool selection. A static synthetic Mach-O analysis passed
with REA 6.7.0, the pinned Node/JDK/Ghidra environment and this setting; app
attachment and each operation's proof remain separate checks.

The examples below use Dictionary; any app you are allowed to analyse works
the same way. `--query` searches the binary's string data:

```sh
anyuse map rea analyze /System/Applications/Dictionary.app \
  --rea "$REA_CLI" --query search \
  --out "$HOME/rea-preview/dictionary-analysis-01"
```

Add `--search-kind procedures` to search function names instead:

```sh
anyuse map rea analyze /System/Applications/Dictionary.app \
  --rea "$REA_CLI" --query search --search-kind procedures \
  --out "$HOME/rea-preview/dictionary-procedures-01"
```

Each output directory must be new. If provider readiness or cleanup fails,
retain the failure and review its resources before another investigation.
Do not treat an analysis exit code as evidence that an operation works.

Choose an exact function or address from the resulting evidence before a
focused decompilation. Replace `EXACT_FUNCTION_FROM_EVIDENCE`; do not guess a
function address from a different app build:

```sh
anyuse map rea analyze /System/Applications/Dictionary.app \
  --rea "$REA_CLI" --function EXACT_FUNCTION_FROM_EVIDENCE \
  --out "$HOME/rea-preview/dictionary-function-01"
```

`--function` takes a function name or an address. For an Objective-C method,
give either the Ghidra name `Class::method:` or the Objective-C form
`-[Class method:]`, which is mapped to `Class::method:` for Ghidra. Quote
Objective-C names in the shell, and take the class and method from the
evidence:

```sh
anyuse map rea analyze /System/Applications/Dictionary.app \
  --rea "$REA_CLI" --function '-[ClassFromEvidence methodFromEvidence:]' \
  --out "$HOME/rea-preview/dictionary-function-02"
```

## 5. Turn evidence into a reusable operation

An investigation still needs a callable route, typed arguments, a way to find
the current receiver and an independent definition of success. A reasoning
agent can investigate these facts through the shared
agent loop (`anyuse map rea agent`). It supplies
declarative recipes and citations; generated pseudocode does not become an
executable adapter automatically.

For the existing bundle-CLI candidate family, the explicit public commands
are below. `candidate.json`, `cases.json` and `inputs.json` must be authored
for your target, following the format of `examples/rea/dictionary-candidate.json`.
These filenames are placeholders, not bundled working mappings:

```sh
anyuse map rea verify candidate.json cases.json \
  --bundle /path/to/Target.app --out /guest/local/proof-01
anyuse map rea replay candidate.json \
  --proof /guest/local/proof-01/proof.json --inputs inputs.json \
  --bundle /path/to/Target.app --out /guest/local/replay-01
```

Upstream REA (`rea-agents`) supplies investigation tooling. AnyUse's native/Qt
runtime requires permitted process access, Python Frida and the fixed compiled
adapter with its matching manifest. Its Chromium/Electron route uses CDP through
a separately enabled, owned inspector endpoint. These execution routes need
separate operator setup; a base `pip install` and the REA setup do not provide
those runtime prerequisites.

For the macOS arm64 native route, install the Python extra in your environment,
then create fresh compiler dependencies and build the bundled adapter:

```sh
python -m pip install -e '.[rea-native]'
anyuse map rea runtime-setup --node /path/to/rea-tools/node/bin/node \
  --out /new/native-runtime
anyuse map rea runtime-build --node /path/to/rea-tools/node/bin/node \
  --modules /new/native-runtime/tools/node_modules --out /new/native-adapter
export ANYUSE_REA_RUNTIME_AGENT=/new/native-adapter/rea-runtime-agent.bundle.js
```

Use the Node installed by `rea setup` and keep its downloaded archive: the
runtime installer authenticates the Node/npm tree against that archive. It
uses a new prefix, exact compiler/bridge versions, a retained registry lock
and a checksummed Frida binding. It preserves the caller's `HOME` and retains
failure and cleanup receipts. This supplies runtime dependencies; app-specific
access, calling-contract evidence and an independent observer are still required.
This sequence passed in a new toolchain prefix and Python environment in the
owned macOS arm64 VM: install, imports, compiler dependencies, one adapter build
and both runtime doctors. The check used existing Python and Command Line
Tools. Target-process access and operation discovery are assessed separately.

For a fixed native playback operation with no actor inputs, the agent loop
also supports a [playback proof campaign](REA_PLAYBACK_PROOFS.md). It checks
public playback controls and actual temporal video pixels, requires separate
learning, fresh and held-out trials plus a witnessed restart, and publishes
through the same action catalog.

## What to expect when something stops

| Message or situation | Next step |
| --- | --- |
| `No module named anyuse` or command not found | Activate the guest venv and install from the checkout root; the repository root itself is the Python package |
| Python older than 3.11 | Select a supported guest Python before creating the venv |
| VM-only refusal | Run inside a macOS VM, or pass `--allow-host` for that invocation |
| Setup reports git and a compiler missing | Install the Command Line Tools as it describes, then run the setup again |
| Setup reports a checksum or integrity mismatch | The download did not match the pinned release and the setup stopped without writing `env.sh`; run it again later |
| Ghidra/JDK not ready | Run the setup again; its doctor report is in `install/ghidra-doctor.json` under the install folder |
| Locked desktop or missing consent for live GUI work | Unlock the guest and grant the required permission there; the read-only steps above need neither |
| Changed app, missing receiver or incomplete result | Keep the failure; investigate or create a new mapping rather than reusing its proof |

Tests run through the guarded runner ([CONTRIBUTING.md](../CONTRIBUTING.md));
tests that need the VM are listed in `docs/TEST_ALLOWLIST_VM.txt`.

For the route inventory and its limits, continue with
[REA_ACCESS_ROUTES.md](REA_ACCESS_ROUTES.md).
