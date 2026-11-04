# REA harness: access routes and current limits

The REA harness helps an agent investigate an operation in a native app once,
save it as a typed recipe, check it against an independent expected result and
reuse it. Reuse resolves the current receiver; it never stores an old process
pointer. A recipe is specific to the app build, its resources, the execution
route and the observed contract.

This page covers the REA routes only. The web, Accessibility and AppleScript
paths have their own documentation.

## Choose a route

| Route | What it can provide | Tried on | Main boundary |
| --- | --- | --- | --- |
| Declared interfaces | Bundle CLIs, URL schemes, Services, scripting declarations and similar existing surfaces | The surface scanner and AnyUse's other desktop routes | Discovery is not an execution test; these are the baseline, not REA coverage |
| REA/Ghidra static analysis | Symbols, strings, selected-function decompilation and evidence for hidden interfaces | Native and JavaScript apps, in a VM | An address or pseudocode does not establish an argument type, a live object or a callable operation |
| Recovered Apple Events and document formats | Invoke a recovered event or load a state through the app's own document parser | Dictionary lookup, TextEdit document opening, Chess saved positions | One format or event covers the operation it was measured on; it is not arbitrary private-function access |
| Renderer inspector | Resolve JavaScript objects and call a declared method through a local inspector | Internal editing operations in unchanged MarkText and draw.io, including after app restarts | The app needs an inspector that is available and explicitly enabled; other renderers and packaged versions need new investigation |
| Native Objective-C bridge | Find supported objects, inspect bounded state and invoke selected declared selectors | Controlled runtime examples, a Chess copy and an IINA playback action through a copy with a debugging signature | Needs ordinary attach permission, supported signatures, a live receiver and the right thread |
| Native C/C++/Swift bridge | Call supported ABI families and resolve reviewed typed object paths | Controlled ABI, read and path tests; one Qt operation in unchanged Flameshot | Each route needs an admitted receiver type, lifetime and calling contract; stripped prototypes and layouts are not inferred |
| Binary-correlated C++ receivers with multi-hop and virtual dispatch | Declare direct or bounded vtable-slot calls, up to three pointer hops and an explicit receiver adjustment | Guarded integration, runtime fixtures, compiled-binary preparation and three live virtual-slot executions on the owned number fixture, including a normal restart | ABI and lifetime remain hypotheses; the live component used zero hops and zero adjustment; live multi-hop/arm64e execution and a full discovery campaign using these contracts remain unvalidated |
| Raw native memory investigation | Bounded range metadata, bounded byte reads and following one pointer slot | Flameshot | Readable bytes do not establish an object type, state, lifetime or a safe receiver |
| Qt MOC dispatch | Invoke an admitted class-local slot on a constructor-observed or freshly getter-borrowed object | Flameshot 14.0.0 `CaptureWidget::togglePanel()` with an existing mapping; fresh getter scope has focused bridge tests | Requires static selection and separate receiver/type/lifetime admission; plain arm64, Qt MOC revision 13, unadjusted primary QObject, no arguments, void return; complete cold discovery remains unproved |
| Instrumented app copy | An explicit debugging route where the unchanged app cannot be attached | Chess and IINA copies running their original compiled instructions | Evidence for that copy, not access to the protected stock app |

These routes are shared mechanisms configured with app-specific data. Each new
app and operation still needs an investigation. A new method that fits an
existing family needs no new code; a new protocol, ownership rule or ABI family
does.

Native families have separate contracts. The original borrowed C++ receiver
family, for example, accepts an unadjusted `this` with scalar arguments and
results. The multi-hop and virtual-dispatch hypothesis contracts below support
bounded additional paths and receiver adjustments without establishing object
lifetime. The Qt family accepts an admitted constructor-observed or fresh
getter-borrowed QObject base at offset zero and a no-argument void method. The getter route runs on the
main queue with no intervening path edges or guards; getter and slot remain in
one bounded transaction. Its separate source, primary-vptr and lifetime
contracts still apply. A readable pointer or matching vtable does not establish
them.

## Bounded generic workflow (development)

`map rea workflow` connects discovery, ordinary caller setup, independent visual
proof and local catalog reuse without app-specific Python callbacks. The caller
freezes the task and observer before discovery. Unparameterized tasks default to
three learning, three fresh and three held-out cases, a witnessed normal restart,
publication and two additional replays; parameterized tasks provide their plan
and replay inputs explicitly. Public setup must prepare each next case without
calling the native candidate. Trial evidence remains `model_judged` when the
independent before/after visual judge supplies the result.

Run `workflow begin`, then `workflow advance`. Handle `needs_setup` with
`public-start`, ordinary computer use and `public-finish --evidence ...`; handle
`needs_restart` with `workflow restart`. `workflow context` exposes the current
request and any existing `public_handoff`: after reconnecting, finish that claim
rather than repeating setup. `advance --continue-discovery` resumes only healthy
bounded/stopped investigation with verified memory. Failed tools, interrupted
actions and uncertain native effects never restart automatically. Exact commands
and task fields are in the [generic quickstart](REA_QUICKSTART.md#generic-agent-workflow-development).

The generic `tool_object_v1` reasoning transport supports ordinary local/hosted
HTTP models, using prompt JSON or explicitly configured JSON-object output, and
the tool-free safe-mode Claude CLI adapter. Direct Codex CLI is refused in this
driver because it retains agent tools; other harness paths are unchanged.
Specialized closed protocols remain HTTP-only and keep their existing schemas.
Reasoning and the independently configured image-capable observer are separate.

An optional caller-frozen `native_path_review` contains a complete reviewer
`ModelConfig`, a single-call-per-builder bound and authentic original
`source_pins`. `begin` copies and registers those bytes for scoped discovery.
`build_native_path` can assemble supported type/receiver/lifetime admission using
that evidence and a separate fallible source reviewer. Source review admits
claims within existing calling families; it does not recover arbitrary ABIs or
prove task success. Missing source correlation or an unsupported contract returns
`needs_capability`. Separately, opt-in `native_contract_mode: declared` retains
native ABI hypotheses against exact original-code evidence, explicitly marked
`model_declared_not_independently_reviewed`; it is not source-reviewed admission.

The declared-native route already supports an opaque pointer getter nested in
a consumer argument as `value.getter`. Declare the getter and consumer against
their respective original-code evidence. This needs no app source or typed
`native_path`; receiver compatibility and lifetime remain unproved. The separate
receiver tier below adds vptr correlation, a single immediate consumer and
enforced two-call epochs with explicit evidence labels.

For a source-free C++ experiment, also freeze
`native_receiver_mode: binary_runtime_hypothesis`. The separate
`declare_native_receiver` tool binds original getter/consumer instructions and
vtable data to one complete recipe. It acquires one pointer through a zero-argument
getter and immediately passes it as unadjusted argument zero to one direct,
scalar-only call on the main queue. There are no path edges, saved pointers,
helpers or arbitrary virtual dispatch in this direct-receiver contract. Each
replay reacquires the receiver and rechecks its observed vptr and resources.

This tier records ABI and receiver compatibility as hypotheses and lifetime as
`assumed_not_established`. It never produces source admission or deterministic
proof. Independent visual trials can establish the measured task effect; they
do not prove memory safety or arbitrary object lifetime. Action cards retain
these evidence labels. An independent reasoning agent discovered
an operation in a compiled C++ fixture using only binary evidence. App sources,
templates and build receipts were excluded. The harness completed three learning,
three fresh and three held-out visual-effect trials, a witnessed normal restart,
local catalog publication and two fresh-input replays. The eleven executions
used twenty-two local visual judgments and retained the hypothesis labels.
The controlled fixture had an explicit debugging entitlement. Earlier failed
attempts remain recorded; the standalone-worker import defect they exposed was
fixed and covered by a real subprocess regression before the successful run.

These generic capabilities have inert integration, bridge and the controlled
live workflow coverage above. Eligibility and unsupported calling families
remain explicit boundaries for each target. The historical measurements below
retain their original scope.

## Multi-hop and virtual-dispatch C++ receivers: implementation and validation scope

The same explicit `native_contract_mode: declared` and
`native_receiver_mode: binary_runtime_hypothesis` settings expose
`declare_native_receiver_v2` and `correlate_native_trace_v2` to the generic
workflow's reasoning agent. The model receives a data-only `native_receiver_guide`
with nested declaration syntax and synthetic examples. Both tools and that guide
are omitted when the mode is disabled or the caller excludes them. Ordinary
LLM configuration, the original receiver tool and existing default routes remain
available. The `full_native_v1` protocol remains Objective-C-only; use the generic
workflow's `tool_object_v1` protocol for these receiver declarations.

These contracts admit direct calls or exact vtable-slot targets, at most three
pointer hops, bounded receiver adjustments, supported scalar/string/buffer/POD
arguments and the main queue. Each invocation reacquires the root, checks the
observed vptr and slot target, and seals the pointer chain, selected slots and vector headers
before the consumer runs. Arm64e image admission requires explicit PAC-strip
settings; this is not pointer-authentication, ABI or lifetime recovery. Receipts
retain `receiver_basis: binary_correlated_hypothesis_v2`, `abi_basis:
model_declared` and `lifetime_basis: assumed_not_established`.

The trace tool ranks caller-supplied baseline calls, actuated calls and observed
getters. It does not capture traces or invoke the app. It accepts at most 256
rows per list and returns at most 256 candidates, preserving the total match
count and an explicit truncation flag. Ephemeral receiver addresses are
investigation hints, never reusable receivers or static evidence. Automatic
UI-triggered trace acquisition is a separate capability, not supplied by this
ranking tool.

Native failures or uncertain dispatch can add the affected signature to the
session's crash blocklist. Proposal, resource reuse and current-session runtime
execution consult that ledger. Confirmed failures before native calls do not
block a valid hypothesis. Recording a failure never grants permission to retry
an interrupted task or clears the existing cleanup boundary.

The multi-hop and virtual-dispatch implementation has guarded workflow/driver
regression coverage, JavaScript execution fixtures and declaration/resource
preparation against an actual compiled C++ Mach-O. A live component
check on the existing owned number fixture also passed three actual `vtable_slot`
executions through the current
compiled adapter, including one after a witnessed normal app restart. Independent
public AX reads confirmed `0` to each requested value; six before/after PNGs
remained in the guest and their pixel changes were checked. Each execution
retained clean detachment, two worker native calls and the hypothesis labels.
The preservation check passed. No observer model was called and no task proof
or catalog entry was created by this component check.

That component reused receiver correlation from the earlier direct-call campaign
plus fresh original slot/code observations. It used `hops: []` and
`this_adjustment: 0`; it does not establish cold model discovery, live multi-hop/adjusted receivers or arm64e
support. A fresh model independently completing proof/catalog/replay using these
extended receiver contracts remains unvalidated. The completed source-free C++
live campaign above used direct getter-to-consumer calls without intermediate
hops or receiver adjustment. See the
[receiver quickstart](REA_QUICKSTART.md#multi-hop-and-virtual-dispatch-c-receivers)
for supported syntax and the boundary between discovery hypotheses and independent
task verification.

## Qt static builders and cold-discovery boundary

The existing Flameshot replay demonstrates a previously admitted mapping. It
does not demonstrate a fresh model building that mapping through the driver.

`rea_qt_moc` requires four distinct pinned artifacts before admission: a
`qt-moc-static-catalog`, `ghidra-code-at-offsets`, an `original-file-range`
containing the uint16 dispatch table, and `qt-moc-source-retention`. The latter
binds seven exact Qt 6.11.1 source files, including `qmetaobject.cpp`. Generic
`static_evidence` builders now retain original data/instructions, extract
class-local MOC metadata, run fixed Ghidra extraction and assemble this static
selection. `qt-sources` prepares the hash-checked public-source cache; discovery
reads it without downloading. These builders supply evidence, not a callable
receiver. Receiver admission separately needs the primary QObject layout,
lifetime scope and independent source review: constructor/destructor lifecycle
evidence for an observed root, or the supported fresh getter contract. Static
analysis or readable memory alone does not satisfy those preconditions.

An initial cold Flameshot discovery attempt stopped at Decision 0 with a
`JSONDecodeError`, before a tool dispatch. Instruction examples used JavaScript
object notation, while `request_json_v1` requires strict JSON inside its request
string and forbids nonzero decision corrections. The optional `tool_object_v1`
protocol now accepts a JSON request object directly, with explicitly bounded
pre-dispatch corrections. This removes nested JSON encoding. The later static
builders can now create the four artifacts; a cold run still reports
**`needs_capability` / `static_catalog_precondition`** when its required static
selection is absent. The separate `full_native_v1` actor schema remains
Objective-C-only (`objc_method`), without `qt_moc_method` support.

The initial layer-0 visual inspection also returned `owned_windows: []` for
Flameshot's overlay. Capture still defaults to layer 0; an explicit
`observer.window_layer` can select another owned layer while retaining identity
and visibility checks. This addition has focused test coverage, but the final
cold attempt still stopped at capture selection with no task trials. No cold
Qt discovery, nine-trial proof or catalog publication is claimed. The next
evaluation requires an admitted static selection, receiver/lifetime contracts
and an available declared capture surface before any task trial starts. Because
`togglePanel()` toggles, its starting visibility must also match the frozen goal.

## What works today

- **Internal JavaScript editing operations** in MarkText and draw.io passed
  learning, fresh verification and held-out checks, and later reuse after app
  restarts. The checks covered the rendered text and ordered SVG labels, not
  saving files or drawing geometry.
- **A native operation in an unchanged app:** a Qt slot in Flameshot, repeated
  in the same process with the saved mapping and once more after a normal app
  restart, with no new investigation or model call.
- **A native playback action in an IINA 1.5.0 build 180 debugging copy:** nine
  distinct synthetic clips/windows passed three learning, three fresh and three
  held-out checks, including a witnessed normal app restart. The workflow
  published the saved action locally; a fresh agent selected it from its card
  and replayed it on a tenth clip without rediscovery or replay model calls.
  An already-paused replay refused before native dispatch. The actor was the
  same saved mapping throughout these measurements.

One later Flameshot check failed because the Accessibility state after the call
could not be read completely; failures like that stay in the record.

For TextEdit (save), Dictionary (search) and Preview (export), REA found no
operation beyond what those apps already declare.

## Desktop restoration status

The desktop workflow orchestrator reports cleanup separately from its primary
operation. `success` and `outcome_kind` describe the operation; serialized
`restoration`, `cleanup_status` and `completion_status` show whether the captured
desktop controls were restored. These fields also survive conversion through
`AgentResult`, the agent replay route, `SmartPlayer` and MCP desktop responses.
Plan execution retains each sub-execution cleanup receipt in its aggregate.
Cleanup reports
`restored`, `partial`, `failed` or `unknown`, with a result for each captured
window, the frontmost app and DND. `partial` means at least one control was
restored and another was not confirmed; `failed` means a known failure with no
control confirmed restored.
When cleanup was not requested, its status is `not_requested`. A primary
exception still propagates unchanged; the orchestrator retains its last cleanup
report for inspection. Agent, SmartPlayer and MCP wrappers return a terminal
failure with that report when a started state-graph replay raises; they do not
repeat the task through another execution route. Unavailable setup before a
replay starts can still use the existing fallback.

Window restoration uses the original live process/window identity and checks
its size and position after writing. Lost identities remain unknown. There is
currently no supported authoritative modern Focus observer, so DND state is
unknown and default replay leaves its settings unchanged. A legacy defaults key
or a successful command alone does not establish active Focus state.

The default desktop cleanup captures desktop controls only. An additional
opt-in [document restoration contract](REA_DOCUMENT_RESTORATION.md) now recovers
saved task-owned document copies and explicitly declared public state. It does
not provide arbitrary app snapshots, private memory or unsaved-buffer rollback.
The REA playback coordinator's declared media setup and witnessed restart remain
separate operations with their own proof contracts. Earlier live checks verified
the declared saved-copy scope in TextEdit and Preview, including initially
closed copies. These results do not cover arbitrary private app state. The newer
exact UTF-8 buffer contract has its own refusal and retained-evidence checks;
see the document contract for the distinction between those scopes.

The development generic workflow can freeze an optional `restoration` checkpoint
and a 1–60 second reserve within its original deadline. Its coordinator attempts
required saved-copy/public-state recovery once before publication or after a
cleanly terminated failure; unknown native cleanup prevents recovery actions.
Required publication recovery and each replay's fresh capture/recovery are
separate gates. The publication receipt is historical evidence, never authority
to restore a later replay from an old snapshot. This covers declared saved
documents, public fields and geometry; arbitrary private state, undo history and
unsaved-buffer rollback remain unsupported.

## Observation and verification

Calling an operation and establishing its result are separate steps:

| Observation source | What it establishes | What it does not establish |
| --- | --- | --- |
| Independent file or reference data | Expected file bytes or another frozen task value | That an unrelated visible document changed or was saved |
| Accessibility | The complete app or window state the contract names | Invisible private state or the meaning of canvas pixels |
| Renderer DOM/SVG | The declared rendered text or SVG labels | Storage, full geometry or every internal editor object |
| Screenshot | An image the caller's agent can inspect | A standalone image does not establish task success, a native object or an ABI |
| Temporal playback control and pixels | The declared public playing→paused transition and nonblack moving→stable video region, bound to one process/window/media | Every pixel or internal state of the app; source-free ABI or receiver discovery |
| Native state or raw memory | The declared read under its access and layout contract | Task success because the actor reports the expected value |

A standalone screenshot remains investigation evidence. The versioned
[playback backend](REA_PLAYBACK_PROOFS.md) has a separate temporal validator
that decodes retained display PNGs and checks the frozen video region. It
currently requires a single-display desktop. The measured native campaign
uses one typed Objective-C action and the declared video region; its live
promotion and catalog reuse passed on the IINA debugging copy described above.

A mapping is validated before it is published, and replays reuse its result
checks. A user-facing switch for how replays verify (`off`, `native`,
`vision`) is not implemented.

Playback and restart evidence identify producer files by fixed module roles and
complete byte hashes, allowing a compatible checkout at a different path. The
original producer files must remain available and unchanged. The validator also
admits explicitly reviewed producer/core pairs: the frozen v1 closure only when
its other core modules still match exactly, and the complete `b2b6600` producer
closure only with the exact reviewed execution-restoration core. A separate
earlier playback/restart producer profile has its own exact core pairing.
Its fixed-playback historical validator retains the old class-name grammar
alongside current checks and reopens the old source, compiled asset and adjacent
build manifest with their recorded pins. This history-only check never admits
that asset for live execution: the live bridge still requires its current
source/build. It records the current validator separately. Other mixed
historical closures remain unsupported. It executes
no historical source. Restart evidence still requires the same pinned `/bin/ps`.
The original trial, candidate, proof and catalog files remain unchanged.

## Run the reasoning policy and choose saved actions

The bounded reasoning driver can investigate an existing `agent begin` session:

```sh
anyuse map rea agent run /path/to/session --out /new/investigation \
  --static-only --max-decisions 12 --timeout-seconds 900
```

For full discovery, the caller can freeze an independently authored observer
declaration when creating the session:

```sh
anyuse map rea agent begin /path/to/Test.app /path/to/task-spec.json \
  --rea /path/to/rea --observer-profile /path/to/observer-declaration.json \
  --out /new/session
```

The declaration contains no actor or evidence citations. The session binds its
hash; the agent proposes an actor using `observer_profile_sha256`, and the
harness supplies the frozen declaration and current-session citations. Changed
observer contracts refuse. Full model inputs expose the public contract, with
ordinary constant reference values and reference file paths withheld; static
inputs expose no observer declaration. Without an approved declaration the
driver can investigate, but cannot propose. Existing direct `agent step`
workflows can still supply a complete profile explicitly.

The static phase exposes inventory evidence, symbols, Ghidra analysis and bundle
resources. It withholds trial values and reference files, admits no runtime or GUI
tools, and reports evidence and unknowns rather than an executable operation.
The full phase uses the existing mapping gates and returns to the caller after
each trial tool call for independent setup. Fixed-state calls cover one case;
ordinary verification and held-out tools can cover a batch of cases. Raw model
decisions, tool requests, source hashes, failures and actual call counts are
retained. A model may still stop or
choose an unhelpful tool; the driver does not supply missing ABI or object facts.
Within one invocation, it retains the latest four successful investigation
turns, exposing at most three earlier results alongside the latest observation.
Earlier payloads share a 6,000-character budget; oversized payloads are omitted
whole with pinned reread references. Their registered feedback bytes are
rechecked, and model-authored reasons remain untrusted hypotheses. Trial and
proof results never enter this window. Symbol feedback includes actual search
counts and truncation flags, plus the registered receipt to read for details.
Native class, method, ivar and unique-instance discovery also returns bounded
metadata directly in tool feedback, bound to the raw worker artifact and hash.
The projection is at most 3,000 characters and reports its own omitted rows
separately from runtime truncation. Full module and layout facts remain in the
raw evidence. Temporary instance handles are omitted from this feedback;
summaries establish neither a callable contract nor a task result.
A failed native `discover` query containing only class, method or ivar metadata
can return `negative_metadata_observation`. This requires actual zero native
calls, unchanged process identity, expired handles and confirmed detachment.
The raw failure remains registered evidence and can enter investigation memory;
it proves no callable operation. Heap selection, getters, invocation, uncertain
cleanup and failed task trials retain their stop behavior. The next model
decision chooses another investigation; the failed request is never retried
automatically. Public preparation separately restores a declared playing state
for a new trial and verifies it before the saved native action.
An explicit `--resume-from /previous/investigation` restores that same bounded
window in a later invocation for the same session. The checkpoint retains up
to 64 references, not copied observation payloads. The driver reopens their
original model inputs, outputs, effective requests and registered feedback;
changed producer bytes, task, target, plan or history refuse. Model reasons
remain untrusted. A narrower investigation requires a separate session if prior
tools exceeded its scope; earlier static queries can depend on native results.
Restored turns also require the producer's model scope to fit the current scope.
Without this option each invocation starts with an empty window.

For native Objective-C investigation, opt into a closed JSON-object protocol:

```sh
anyuse map rea agent run /path/to/session --out /new/native-investigation \
  --decision-protocol full_native_v1 --max-decisions 32 \
  --timeout-seconds 3600 --model-timeout-seconds 300 \
  --model-max-tokens 16384 --max-context-chars 96000
```

These are explicit resource limits, not a discovery guarantee. The configured
reasoning provider still supplies the model; the protocol admits bounded
metadata, typed operations and a hash-bound proposal, with no trial or setup
tools. Legacy reasoning requests and the separate static protocol remain
available. The fixed-state [campaign coordinator](REA_PLAYBACK_PROOFS.md#coordinate-public-setup-and-restarts)
can run after an approved hypothesis without further model calls.

For an eligible Cocoa/AppKit target, optionally add
`--discovery-playbook objc_appkit_v1`. This requires `full_native_v1` and the
`discover`, `runtime` and `evidence` tools. The data-only guide distinguishes
standard platform names used as search hypotheses from measured class/method
facts. It suggests discovering framework roots with `owner: all`, measuring
exact getter signatures, reading their retained worker evidence to obtain the
actual returned controller class, then searching inherited methods with a few
task-derived literal substrings. Getters execute app code and retain the
existing execution and cleanup gates. Nil/ambiguous/unsupported routes stop;
there is no automatic getter call, task invocation, retry, GUI preparation or
new receiver/ABI capability. The existing query index is historical, and a new
process epoch requires fresh validation. The guide reserves existing decision
and time budgets and recommends STOP with explicit unknowns when exhausted.
The selected guide and hash are recorded with model-input provenance; default,
legacy and static prompts are unchanged. This is investigation guidance, not
evidence of successful cold discovery or support for arbitrary Swift/C++ apps.

Either selected AppKit option also adds a bounded investigation frontier beside
the full-native action instructions. It separates G steps from remaining model
decisions and time, class-name searches from exact method-query scopes, and an
approved observer declaration from task-effect proof. With a validated receiver,
an unqueried method scope can include an optional inherited-method metadata
query; it supplies no selector, actor or automatic dispatch. Query results remain
complete, truncated, failed or unknown only for their exact scope, and omitted
scopes are explicit. A zero-match filtered query does not exhaust a class. STOP
remains legal, and discovery effectiveness still requires a live model-led run.

The frontier also exposes the already measured class-root getter recipe with
each hop's raw metadata and observation citations. This is historical path
evidence, not a claim that its terminal class has a unique instance or that the
same object/window is current. Bounded method rows carry exact citations into
their registered raw worker artifact; their pointers do not refer to the
feedback summary. Whole paths/rows may be omitted explicitly to preserve the
6,000-character frontier bound. Raw sources are reopened before a correction
or dispatch. No task method, argument value or effect is inferred by this aid.

Rejected full-native enum values report their schema path and allowed members.
Objective-C argument-count errors distinguish implicit self/_cmd fields from
explicit arguments and report counts derived from submitted syntax. These
diagnostics preserve the validation rules and do not repair a request or supply
native evidence.

`--proposal-evidence native_objc_v1` separately opts into read-only proposal
preflight for `full_native_v1`, with `propose`, `evidence`, `discover` and
`runtime` admitted. It is independent of either AppKit option. For an
Objective-C method actor, registered raw current-session observations must
support its class or unique-instance root, each exact getter and non-nil
returned class, and the terminal method's class, dispatch and signature.
Framework-owned methods are eligible; task-specific ownership and effect
requirements remain separate gates. Other actor families retain their existing
handling. No class name, selector, argument, citation or native operation is
supplied by this check.

Missing actor facts can enter the existing explicitly bounded decision-correction
path. Clean negative metadata contributes no positive facts. Changed pins,
process/adapter drift, malformed observations and uncertain cleanup stop the
driver; they are never recast as missing facts. The consumed evidence and current
identity are rechecked before dispatch. This option does not change manual G
proposals, default/static/legacy drivers, independent proof or replay gates, or
the advice-only contract of `--discovery-playbook`. STOP remains legal.

An executable scaffold is separately opt-in:
`--discovery-routine objc_appkit_receiver_v1 --discovery-route main_window`.
It requires the same full-native investigation tools. Before model decisions,
the harness searches actual loaded `NSApplication` metadata, measures
`sharedApplication`, then the returned application's `mainWindow`, then that
window's `windowController`. `key_window` is an alternative declared route;
there is no automatic switch or fallback. The default route is `main_window`
only when the routine is explicitly selected. Without it, the model wire and
ordinary reasoning loop stay unchanged.

The routine executes up to seven G steps and six measured getter calls
(prefixes repeat: 1 + 2 + 3), within its 180-second bound and the caller's
existing session/driver deadlines. A native transaction is admitted only when
its full 15-second worker allowance and 5-second cleanup reserve fit; a late
request is refused rather than shortened to a two-second worker. It reserves
128 MiB of
evidence allowance plus 128 MiB free headroom on both session and output
filesystems before work; storage admission is
not an OS disk quota. A measured explicit nil at the new final getter ends the
declared route as `route_unavailable` with reason `measured_nil`. Its index
retains only the successful nonnil prefix, exact getter metadata and a raw null
citation; it supplies no receiver recipe for that null. After reopening the
source, process, registered raw requests/results, actual counts and clean
cleanup, the driver may continue independent model investigation within its
original bounds. It does not switch routes, retry getters or change focus.
Missing values, malformed objects, a nil or changed class in a repeated prefix,
ambiguous/truncated metadata, unsupported ABI, changed process/source, failed
getters or uncertain cleanup remain terminal without a model call or retry.
If a worker times out without producing JSON, the retained runtime timeout
and resource-review state are reported directly. An empty worker file is not
parsed as a replacement error, and a later budget fix does not clear an
earlier session's cleanup uncertainty.
Native reads check the previously
measured receiver class before the next getter. This stronger `expected_class`
precondition is validated by the native request schema; the model's existing
`full_native_v1` wire deliberately does not expose it. A private G caller
identity handoff also binds scaffold and subsequent model-driven native calls
to the same process before worker attachment.

`driver.json.discovery_routine` retains the selected route, process/source
pins, actual per-step native counters, ordinary G/runtime evidence and a pinned
`receiver-index.json`. The compact index supplies pointer-free recipes, actual
returned classes and citations to G-registered raw metadata/worker observations
to the model. These are labeled harness scaffold observations separately from
model decisions. Aliases expire in each transaction. Equal classes do not mean
equal objects or windows: recipes resolve current objects again, and actual
task context, target-method selection, proposal, independent effect checks and
the fresh/heldout/restart proof gates remain necessary. This implementation
adds a receiver-discovery aid; it does not establish live cold discovery,
arbitrary-app task control or document/private-state restoration.

The same selected routine can optionally add task-filtered class metadata:
`--discovery-class-query pause --discovery-class-query play --discovery-class-query video`.
The caller freezes one to four distinct printable queries, each at most 64
characters and a case-insensitive literal substring of the session's task
(`play` can match `playing`). This option requires the explicit receiver routine;
omitting it keeps that routine's earlier requests, playbook and result fields.
After a clean observed or measured-nil route, one additional metadata-only G
batch searches `classes` with `owner=app` and at most 32 returned rows per query.
It uses the same 180-second, storage, process, source and cleanup bounds. It adds
one scaffold dispatch and no getter, goal or model calls. An empty or truncated
search does not end independent investigation or imply an unsupported app.

`receiver-index.json.class_inventory` distinguishes the requested task terms
from actual exact class names, modules and raw registered row citations. It
retains each query's overlap and truncation separately, caps indexed rows at
128, and projects only whole rows within 64 KiB with explicit omission counts.
The complete visible index counts against the existing model context budget;
the driver refuses an oversized prompt rather than dropping evidence. The
batch and its actual zero native-call/clean-detach evidence are revalidated
before model decisions, tool dispatch and final completion. These rows are
harness metadata observations, never model-authored memory, live receivers,
task methods or proof. Guidance directs the model to exact observed class
strings before method/ivar searches; it does not force a proposal or change
the independent receiver, signature, ownership, effect or publication gates.

Each model input includes the exact decision schema and, when admitted, a valid
registered inventory-read example. Full investigations also receive a bounded
Objective-C syntax guide for metadata, receivers, typed reads, invocation and
saved actors. Its identifiers are fictitious; actual app facts still need
current-session evidence. A supported proposal also needs the caller's approved
independent observer profile, which the model must not invent from hidden cases.
The bounded driver accepts HTTP model providers; CLI-backed providers have
different tool access and timeout behavior and are refused within this loop.
Their support elsewhere in the harness is unchanged. Truncated answers or
failed spend accounting stop before tool dispatch, and the driver closes model
clients it created.

A guest can export descriptions after checking its actual catalog and proof;
a host can ask an explicitly configured model to choose, then return the saved
selection for normal guest execution:

```sh
# In the guest:
anyuse map rea choices /path/to/catalog.json --task 'Pause the current video' \
  --out /new/choice-view
# On the host, after copying view.json:
anyuse map rea choose-view /path/to/view.json --backend laya \
  --presentation semantic_v2 --fallback reasoning --out /new/selection
# In the guest, after copying selection.json:
anyuse map rea execute /path/to/catalog.json /path/to/selection.json \
  --inputs /path/to/empty-inputs.json --out /new/replay
```

`--fallback reasoning` is opt-in. It asks the configured HTTP reasoning model
after an uncertain or unavailable primary chooser, retains both attempts and
shares their budget. A confident `none` ends selection. Model choice grants no
proof: execution reopens the catalog, app, candidate and proof. Default `auto`
still uses configured Djev, otherwise [OpenJev](https://github.com/razorback16/openjev). Explicit backends also include
local [Laya](https://huggingface.co/convaiinnovations/laya) (`pip install laya`, Apache-2.0 by Convai Innovations; populate with `python tools/confirmer/fetch_laya.py`), hosted Jev (optional; not publicly available) and `reasoning`; these protocols are not interchangeable.
Laya is optional for selecting a saved action. Initial investigation remains a
reasoning-model workflow, and saved actor replay with deterministic verification
needs no model. Model configuration must keep these roles separate and preserve
ordinary LLM support.
This follows the same cascading idea as the web and desktop routes, with
different triggers: REA can escalate an uncertain selection to one configured
reasoner. It does not share their complete escalation policy, and a replay
refusal does not trigger another execution route.

Live local Laya tests did not establish a usable saved-action chooser: it chose
pause for all twelve independent prompts, including six requests outside that
action's contract. The unchanged confidence gate refused all twelve, including
the six valid requests. A first local reasoning-model QuickTime investigation
also stopped before using any tool; its failure is retained. Phase instructions
were corrected separately. A second attempt produced malformed nested JSON;
static decisions now use closed, structured tool objects instead.
A third attempt reached the symbol tool but encountered the system's developer
tool installer stub. Symbol readiness now inspects a real native executable
without launching it and never selects those stubs. A fourth attempt verified
the cached real tool but again stopped after one model call without inspecting
evidence. After adding the explicit model-visible decision schema, a fifth
attempt made twelve successful static tool calls before exhausting its bound.
Its symbol searches returned no matches; it repeatedly read inventory or
repeated the search without reading the symbol receipts. It did not reach
decompilation or a supported hypothesis. All five outcomes remain separate;
initial model-led native discovery has not passed.

A fresh post-patch interface check then exercised the bounded memory and direct
symbol feedback with the same local model and task limits. It read the symbol
receipt and broadened its search from zero pause matches to 22 literal play
matches in external references. After five successful static tool calls, its
sixth model response exhausted the token limit with no usable decision. The
driver retained the normalized provider result and stopped at its empty-answer
guard before another dispatch; the full HTTP body was not retained.
This confirms delivery of the new evidence
interfaces, without proving a causal improvement, a callable operation or
initial native discovery. The earlier five outcomes remain unchanged.

An explicit local Qwen reasoning chooser subsequently answered all twelve
previously measured requests correctly (six matching actions and six refusals).
Two further selections through the actual Laya-to-reasoner workflow returned the
correct selections, using four fresh model calls. The combined comparison used
fourteen reasoning calls and two Laya calls, with no retry, paid request or app
action. This establishes selection and fallback integration on those prompts,
not fresh held-out quality or initial native discovery. The reasoning confidence
is self-reported, not calibrated.

A subsequent live integration passed on the IINA debugging copy using the
unchanged saved pause mapping and original proof/catalog. A guest-validated
view went to local Laya, whose uncertainty triggered local Qwen; the selected
action returned to the guest for normal execution. Replay used zero model calls
and four native entries: three fresh receiver getters and one pause invocation.
Independent Accessibility and temporal video-region checks verified the result.
Immediately repeating the same selection while paused refused before any
runtime dispatch. A fresh request to resume then correctly selected no action,
and nothing executed. The two selection phases used four local model calls in
total, with no paid or hosted inference. This proves selection and live reuse
of that saved action, not model-led initial discovery or arbitrary app control.

## Build the fixed native adapter offline

With an existing Node executable and local `node_modules` cache, check and build
the bundled adapter without opening an app:

```sh
anyuse map rea runtime-doctor --node /absolute/path/to/node \
  --modules /absolute/path/to/node_modules
anyuse map rea runtime-build --node /absolute/path/to/node \
  --modules /absolute/path/to/node_modules --out /new/runtime-build
anyuse map rea runtime-doctor --node /absolute/path/to/node \
  --modules /absolute/path/to/node_modules \
  --adapter /new/runtime-build/rea-runtime-agent.bundle.js
export ANYUSE_REA_RUNTIME_AGENT=/new/runtime-build/rea-runtime-agent.bundle.js
```

The cache must already contain `frida-compile` 19.1.0,
`frida-objc-bridge` 8.0.6 and `frida-swift-bridge` 3.0.2, with the admitted
compiler entry. These commands do not install or download them. The build uses
the fixed bundled source, takes no app-specific script or compiler flags, and
retains the complete bounded cache/tool/source hashes, raw compiler output,
source-map input checks and adjacent runtime manifest in a new directory. Input
drift, incomplete output or unconfirmed compiler cleanup fails the build. The
cache stays unchanged; cached bytes are recorded rather than authenticated
against a package registry. Source-map checks do not trace every operating-system
file read or establish bit-for-bit builds across different directories.

These commands can run without a guest desktop. A passing check establishes
offline build prerequisites only: Python Frida, permitted target-process access,
app evidence and independent task verification remain separate requirements.

The offline sequence passed with the existing cached tools: two adapter checks
and one actual compiler run, with unchanged source and cache bytes. Its first
attempt refused a 94 MiB cached native dependency before compilation; the cache
reader now hashes files incrementally with a 128 MiB per-file and 512 MiB total
limit. The failed attempt remains separate from the successful build. This does
not establish dependency installation on a fresh machine or target-process access.

A subsequent fresh-prefix check installed Node, JDK, Ghidra and REA, compiled
the arm64 decompiler, and passed its doctor. A new Python environment installed
the exact wheel with `rea-native`; the first-party `runtime-setup` installed
46 registry packages with a retained lock and authenticated Frida binding.
One new fixed adapter build and both runtime doctors passed. All 421 installed
package files and 929 frozen source files matched before and after; Node and
its retained archive stayed unchanged. Existing Python and Command Line Tools
were prerequisites. This installation check made no app, attachment, native or
model calls; app-level access and cold discovery remain separate checks.

The same implementation passed the full 394-file guarded suite with all 124
protected paths unchanged. A new installed-wheel environment also passed the
offline quickstart: two held-out inputs, new-value replay and MCP, and explicit
zero-item failure after the fixture API changed, all with zero model calls.

A separate source-available Objective-C fixture passed live bool, signed and
unsigned integer, double, UTF-8, nil and void/counter checks. The same serialized
receiver recipes and requests passed after a witnessed normal process restart,
with zero model calls. These are finite ABI and restart-reuse checks, rather
than source-free discovery in another vendor app. A local model also resumed a
fresh investigation in a second Python process using its verified memory;
that run stopped before proposing an operation when it queried an absent class.

## Remaining scope

A reasoning model independently discovered a parameterized inspector operation
in draw.io. Its saved recipe then passed through the product visual coordinator:
three learning trials, three fresh checks and three held-out trials, including
an independently witnessed normal restart, followed by catalog publication and
two new-input replays. The receiver recipe, method, function hash, argument types
and input bindings remained those from the model's discovery; only current
runtime evidence was revalidated. Each trial retained local before/after model
judgments. This is `model_judged` evidence for that operation, app build and
input domain, rather than deterministic proof or general native discovery.

The catalog persists as local, pinned artifacts. Explicit computer-use callers
can use `rea_actions.prepare_selection` and `rea_actions.execute`, or the CLI
chooser and execution commands, with the normal proof and process checks. The
coordinator requests public setup and restart observations through its data-only
checkpoints.

The current integration candidate adds explicit persistent registration:

```sh
anyuse map rea registered add diagram /absolute/path/to/catalog.json
anyuse map rea registered list
```

Register inside the VM. Registration reopens the existing proof; it does not
discover or prove an action. It pins the catalog, candidate, proof and card.
Normal agent toolboxes advertise `run_rea_action` with a registered tool name and
typed inputs; Python `Agent.run` advertises the equivalent `rea_action` choice.
MCP default/full profiles expose `rea.<registration>.<action>`; the fixed meta
profile exposes the same operation under site `rea.<registration>`. All use
`rea_actions.execute`, with a fresh process identity and the normal proof gate.
They retain the complete REA result in `rea_executions` or the MCP result,
including `model_judged`, observer call counts and cleanup failures. A refused
or uncertain execution ends the run without UI fallback. Native actions keep the
ordinary agent commit gate and cannot become synthetic click/key recordings.

There is no model-facing registration tool, catalog directory scan, or host
native fallback. An empty registry creates no files and leaves existing tools
unchanged. A registration with required restoration needs a trusted
`CatalogStore(restoration_provider=...)` that supplies a new declaration for
each execution; a historical successful cleanup receipt does not satisfy it.
Registrations also reject inherited `--allow-host`. Focused guarded tests cover
persistence, changed evidence, collisions, MCP profile parity, both agent routes
and terminal failures. Live component checks also passed: a local model selected
the registered action through the ordinary agent harness and changed draw.io
zoom to 110%; a real stdio MCP meta call changed it to 120%. Each operation
retained two observer judgments and confirmed one internal method dispatch with
complete inspector cleanup. Both preserved the owned document and all 53 saved
files. These component checks used the new adapters with the immutable source19
proof producer; the first MCP attempt lacked its optional SDK and stopped before
MCP dispatch, then the separate MCP check passed using already-validated SDK
dependencies. The new distribution gate is separate from these live checks.

A fresh native run also completed on the owned IINA 1.5.0 debugging copy.
Codex (`xhigh`) formed the operation in three model calls from current
Objective-C metadata and measured getters, without app source code or a saved
task mapping. The harness accepted three learning, three disjoint fresh and
three disjoint held-out trials, including an independently witnessed normal
process restart. It published the catalog and replayed the operation on two
additional inputs. All eleven task dispatches retained clean native cleanup;
verification used 22 local before/after judgments and remains `model_judged`.

A separate ordinary `AgentOrchestrator` run registered that catalog and used
the VM-local model to select its `rea_action` on a further unseen media input.
It completed one native task dispatch, two independent observer judgments and
clean detachment, using two ordinary-agent decisions. Its first attempt
exposed a contradictory UI-only action example in the prompt; the corrected
example admits registered REA actions. The failed no-dispatch attempt remains
retained, and ordinary LLM and UI actions remain available.

The frozen observer first passed a separate public hidden/visible calibration.
Public file opening, closing prior task-owned windows, sizing and positioning
were caller setup; none invoked the native task actor. Earlier static-analysis,
public-setup and visual-precondition failures remain retained. Exact-function
Ghidra analysis of this app timed out; the successful route used runtime
metadata and empirical effect checks, without claiming a decompiled body.

| Remaining work or limitation | Scope |
| --- | --- |
| Fresh model-led native discovery in more unchanged vendor apps | The completed inspector discovery and proof do not establish automatic recovery of native calling contracts, live receivers or execution contexts across apps |
| Generic workflow and distribution validation | Explicit registration passed live ordinary-agent and stdio MCP component checks; each distribution needs its own package gate. The development front door now journals investigation, caller-performed public setup, witnessed normal restart, proof and reuse, with inert integration coverage; this adds no new live-app proof |
| More unchanged apps and restart repetitions | A Qt operation, JavaScript apps and an Objective-C debugging copy do not establish control of arbitrary apps; access and each operation still need their own measured evidence |
| Adobe Reader native discovery | An explicitly authorized debugging copy attached and exposed 191 app-owned Objective-C classes. Its receiver scaffold measured the application and main window, then a null window controller. A later metadata worker timed out with uncertain cleanup, so that session stopped before any model decision or task invocation; no Adobe operation proof or replay is claimed |
| Stock VLC native access | Normal attachment to unchanged VLC 3.0.24 returned the OS/Frida `PermissionDeniedError` before any native task or model call; no VLC operation proof or replay was created |
| Restoration of undeclared or private app state | Supported recovery covers saved task-owned document copies and declared public state/window controls; the exact UTF-8 guard detects unsupported unsaved buffers rather than rolling them back |
| General prototype, layout and ownership recovery | Readable memory and decompilation do not supply every calling contract |
| Broader ABI support | Generic, async or throwing Swift and arbitrary ABI/layout recovery remain unsupported. Live virtual-slot dispatch passed on an owned zero-hop fixture; live multi-hop, receiver adjustment and arm64e PAC-strip admission remain unvalidated |
| Free-form editing, pixel-only apps and real-time games | These need their own transports, reliable observers and timing loops |

Protected apps may refuse normal attachment. An inspector, an Apple Event or
document route, or an explicitly instrumented copy can be an alternative where
it fits. There is no general way around process protections, and a working copy
does not prove access to the unchanged app.

## What the harness adds around REA

Upstream REA (`rea-agents`) offers investigation tooling. AnyUse separately
implements typed native execution through its fixed Frida adapter and renderer
execution through Chromium CDP. The harness turns an investigation into a
reusable action: frozen task inputs, typed recipes,
references to the evidence, a fresh receiver on every run, independent result
checks, retained failures and action descriptions bound to a proof.

An agent reasons through the first mapping. Compatible saved recipes avoid
rediscovery. Deterministic replay uses no model; successful visual replay makes
two observer-model calls and retains `verification_tier: model_judged`. The local
configuration below keeps both calls local. Choosing an action does not establish
task success, and visual judgments remain a separate evidence tier.

Some facts stay app-specific: which operation matters, where it is, its argument
types and the context it runs in. What generalises is the shared discovery,
execution and validation machinery, not automatic understanding of every app.

Start with the [quickstart](REA_QUICKSTART.md).

### Visible effects without a deterministic observer

The shared native and inspector mapping workflow also admits the separate
`guest_model_judged_state_v1` backend. A caller freezes a
`anyuse.rea-visual-effect-profile/1` declaration containing the expected visible
starting state, requested visible change, typed parameters, exact target build,
window selection, confidence threshold and an explicit image-capable HTTP model
configuration. Discovery and action selection retain their existing model
interfaces; the visual judge does not silently choose a provider or enable an
inspector.

`rea_agent.begin(..., visual_fallback=profile)` selects that declaration when
the caller supplies no deterministic `observer_profile`. Its trial plan uses
`anyuse.rea-visual-effect-plan/1`. A declared deterministic observer takes
precedence. An invalid declaration, failed deterministic check, process-access
denial or uncertain runtime effect cannot be replaced by a favorable screenshot
judgment.

The CLI can use the same declaration with `agent begin --observer-profile`;
there is no separate CLI fallback flag. The following complete data example
freezes a generic visible numeric-value task. Run it in the configured guest.
Replace the app path, task/visible contract and local model name with the actual
target and image-capable server before beginning; it supplies no actor, receiver
or app-specific method. The app must already have its eligible execution route.

```python
import json
from pathlib import Path
from anyuse.execution import rea_mapping as R

bundle = "/path/to/Test.app"
params = [{"name": "level", "type": "string", "source": "text"}]
plan = {"format": "anyuse.rea-visual-effect-plan/1"}
for phase, start in zip(("learning", "verification", "heldout"), (1, 4, 7)):
    plan[phase] = [
        {"id": f"{phase}-{i}", "inputs": {"level": f"0.{start + i}0"},
         "restart": phase == "heldout" and i == 0}
        for i in range(3)
    ]
policy = {
    "format": "anyuse.rea-visual-effect-task/1",
    "id": "visible_level", "target": R.fingerprint(bundle),
    "kind": "visible_state_transition", "params": params,
    "observer": {"window_title": None},
    "before": "The owned task window is foreground, its Level control is readable, "
              "and no modal dialog or error covers the task content.",
    "after": "The same window's Level control shows the numeric value in inputs.level; "
             "equivalent decimal formatting is allowed. Existing task content remains "
             "present. Setting an already satisfied value is valid.",
    "minimum_confidence": 0.9,
    "input_constraints": {
        "level": {"kind": "decimal", "minimum": "0.10", "maximum": "0.90"}
    },
    "model": {
        "provider": "local", "base_url": "http://127.0.0.1:8000/v1",
        "model": "your-local-vision-model", "api_key_env": "",
        "retries": 0, "timeout": 60, "max_tokens": 1024,
        "price_in": 0, "price_out": 0,
        "structured_output_mode": "json_schema"
    }
}
profile = {
    "format": "anyuse.rea-visual-effect-profile/1", "id": "visible_level",
    "label": "Set the visible level", "effect": "visible_state_transition",
    "params": params, "policy": policy,
    "scope": "model_judged_visible_effect_across_declared_trials_with_restart"
}
spec = {
    "task": "Set the current task's visible Level to the requested level.",
    "params": params,
    "success_criteria": ["The visible value matches the input and task content is preserved."],
    "plan": plan
}
Path("visual-profile.json").write_text(json.dumps(profile, indent=2))
Path("visual-task.json").write_text(json.dumps(spec, indent=2))
```

The explicit `policy.model` controls this judge. `ANYUSE_VISUAL_MODEL_*` and
`ANYUSE_JUDGE_MODEL_*` do not fill or override that frozen configuration. Supply
`retries: 0`: the general `ModelConfig` default is two retries, which this
single-attempt policy rejects. The endpoint must accept image inputs and the
chosen structured-output mode; the `local` label alone establishes neither.
The example selects loopback and needs no API key. Discovery uses its separately
configured `AGENT` role, for example:

```sh
export ANYUSE_AGENT_MODEL_PROVIDER=local
export ANYUSE_AGENT_MODEL_BASE_URL=http://127.0.0.1:8000/v1
export ANYUSE_AGENT_MODEL_NAME=your-local-reasoning-model
export ANYUSE_AGENT_MODEL_API_KEY_ENV=
anyuse map rea agent begin /path/to/Test.app visual-task.json \
  --rea /path/to/rea --observer-profile visual-profile.json \
  --max-steps 64 --budget-seconds 7200 --out /new/visual-session
anyuse map rea agent run /new/visual-session --out /new/visual-discovery \
  --decision-protocol inspector_structured_v1 --max-decisions 32 \
  --timeout-seconds 1800 --model-timeout-seconds 180 \
  --model-max-tokens 8192 --max-context-chars 96000
```

The inspector command automatically selects its inspector-only scope and tool
set. For a native route, choose that route's explicit discovery protocol instead.
These resource bounds do not guarantee that the model discovers an operation.
With `window_title: null`, exactly one visible owned window must match. Supply
a measured literal title when needed, and prepare that window in the foreground.

Each visual trial captures the owned window, asks the configured model to check
the frozen precondition, invokes one typed native or inspector method, captures
the same window again, and asks whether the requested visible transition
occurred. It retains the screenshots, complete model inputs and outputs,
configuration, actual call counts, runtime receipts, source pins, process/kernel
identity and ordering. Model output uses a strict JSON schema; an uncertain,
malformed or insufficient-confidence answer stops the trial. The images and app
text remain untrusted data. The judge cannot establish an invisible internal
state.

Publication requires at least three actual trials in each of the learning,
fresh and held-out phases, including an independently witnessed app restart.
Cards, chooser inputs, campaign receipts and replay results retain
`verification_tier: model_judged`.
The replay verdict is `model_judged`, rather than `verified`; this records the
evidence type even when all receipt and ownership checks pass. A compatible
saved recipe avoids rediscovery, but this verification route makes two
observer-model calls on each successful replay. The documented local policy
keeps those calls local. Existing deterministic routes keep their own contracts
and zero-model replay behavior.
CLI `execute` and `replay` preserve that verdict/tier in their JSON and return a
successful exit status for a passing model-judged visual replay.

Use the G tool lifecycle for this visual campaign. After the accepted hypothesis,
prepare each declared initial state through ordinary public UI operations, then
call `record` for learning cases 0–2, `learn`, `verify` for fresh cases 0–2, and
`heldout` for cases 0–2. A trial does not open, activate or reset the app for you.
For example, after preparing learning case 0:

```python
from anyuse.execution import rea_agent as G

# Full identity from the current owned launch receipt, not a guessed PID.
G.step("/new/visual-session", {"op": "record", "case": 0, "timeout": 240},
       expected_runtime_identity=owned_identity)
```

Prepare a separate declared case before each next trial. Before `heldout` case 0,
use `rea_process_restart.run_restart` with trusted ordinary stop/launch hooks.
Pass its actual intent/receipt pins and `predecessor: "verification-2"` in the
trial's `restart` field, and use the witnessed new identity. The shared
[restart witness example](REA_PLAYBACK_PROOFS.md#record-and-promote-measured-trials)
shows that field's shape and chronology requirements. After all held-outs pass,
call G's `publish` tool with `summary` and `details`, then use the resulting
catalog with the existing chooser/`execute` or Python `rea_actions` APIs. For a
60-second judge, pass `--timeout 240` to CLI `execute` or `replay` so the trial
can reserve time for both judgments and its capture/dispatch checks.

G delegates visual learning and validation through the shared harness H into
`rea_visual_campaign` (C). This is the G/C lifecycle, rather than the older
`rea_harness.run`/`map rea harness` input-only batch API.

For product-managed sequencing, `agent coordinate` also accepts a separate
`anyuse.rea-visual-campaign-coordinator/1` configuration. It requests public
setup one case at a time, sequences learning, fresh checks, held-outs and
publication, and can reuse the resulting action through `rea_actions.execute`.
It never loads setup code from configuration. For the example above, after a
current-session hypothesis is accepted:

```python
from anyuse.execution import rea_agent as G, rea_campaign_coordinator as Q

session = "/new/visual-session"
context = G.context(session)
configuration = {
    "format": Q.VISUAL_CONFIG, "session": session, "owned": True,
    "target": context["target"], "plan_sha256": R.digest(context["plan"]),
    "observer_profile_sha256": context["observer_profile_sha256"],
    "expected_identity": owned_identity,  # Exact current owned launch receipt.
    "resources": [],  # Add exact {file, bytes, sha256} pins for owned documents.
    "limits": {"seconds": 3600, "checkpoint_seconds": 300,
               "trial_seconds": 240, "max_actions": 64},
    "publication": {"summary": "Set the visible level",
                    "details": "Model-judged visible level within the declared domain."},
    "selection_task": "Set the current task's visible Level.",
    "replays": [{"id": "reuse-0", "inputs": {"level": "0.15"}}]
}
Path("visual-coordinator.json").write_text(json.dumps(configuration, indent=2))
```

```bash
anyuse map rea agent coordinate /new/visual-session visual-coordinator.json \
  --out /new/visual-coordinator
```

The returned journal has `status: needs_setup` or `needs_restart` and an exact
`pending` request. An external computer-use caller performs that public setup
and writes a checkpoint containing `format: anyuse.rea-public-setup-checkpoint/1`,
`request_sha256: R.digest(pending)`, the requested `kind`, `status: ready`, the
actual current `identity`, 1–16 pinned observation files in `evidence`, and the
actual `started_monotonic`/`finished_monotonic` times. For `kind: restart`, also
include the existing RR `restart: {intent, receipt, predecessor}` entry. The
witness must follow the immediate prior measured trial and this request; the
coordinator then requests separate public setup in the witnessed new epoch.
It supplies neither the public setup nor ordinary restart hooks itself.

```bash
anyuse map rea agent coordinate /new/visual-session visual-coordinator.json \
  --out /new/visual-coordinator --resume --checkpoint /path/to/checkpoint.json
```

Each checkpoint is consumed once. Calling `--resume` without a checkpoint
returns the same pending request and original expiry. Caller readiness remains
an assertion; the visual producer still judges the actual starting window
before calling the actor. Failed or interrupted actions never resume or retry.
The journal retains the catalog, explicit selection, replay results, actual
observer-call counts, storage accounting and `verification_tier: model_judged`.
Fixed playback configurations retain their existing automatic coordinator.

The optional `policy.input_constraints` supplies literal string enums or
inclusive decimal ranges. These are checked before model or actor execution.
Decimal bounds use exact decimal text. Finite-enum trial plans may explicitly
request `input_mode: finite_enum_coverage`; every phase must cover all allowed
combinations with separate actual executions. Other parameterized trial plans
retain disjoint learning, verification and held-out inputs. The candidate now
retains that declared plan mode through learning and promotion; a bounded enum
alone does not turn a disjoint plan into exhaustive coverage. Historical
candidates without the mode field retain their original shape and inference.
These receipts cover
the declared input domain and observed effects, not arbitrary native arguments
or all application behavior.

### Declared inspector call types

A cold inspector investigation can use `declare_inspector` after a completed
method observation. The tool accepts a bounded actor without
`signature_evidence`, plus registered citations to the actual method observation.
It writes JSON data containing the observed function hash, receiver recipe,
proposed argument/return types and session provenance. The returned actor uses
`signature_evidence.kind: declared_js`.

Those types are model-declared hypotheses, not automatically recovered types or
an independent review. Declaration invokes no method and creates no task proof.
Proposal admission reopens the current-session observation and declaration;
invocation resolves a fresh receiver and rechecks the function body. Existing
`reviewed_js` declarations retain their own admission path. Independent result
trials still decide whether the proposed call accomplishes the task.

For a configured Chromium inspector, use the closed typed protocol:

```sh
anyuse map rea agent run /path/to/session --out /new/inspector-discovery \
  --decision-protocol inspector_structured_v1 --max-context-chars 96000
```

This automatically selects `inspector_v1` and its bounded discovery tool set.
It uses a closed typed
`request` object for resource/evidence reads, inspector metadata discovery,
call declaration and proposal. The existing validators still admit the decoded
request. A larger explicit `--max-context-chars` can accommodate the schema and
retained metadata; the configured limit remains a hard bound.

The JSON-text alternative requires an explicitly admitted tool set:

```sh
anyuse map rea agent run /path/to/session --out /new/inspector-json-discovery \
  --decision-protocol request_json_v1 --discovery-playbook inspector_v1 \
  --tool resource --tool discover --tool evidence \
  --tool declare_inspector --tool propose
```

Python callers can pass the same `discovery_playbook="inspector_v1"` and
`allowed_tools` explicitly to the reasoning driver.

This scopes investigation to explicit `provider="inspector"` requests and
inspector actors. Another provider or an omitted provider is refused before
dispatch. It supplies no application receiver, method, argument values or task
result.

The `inspector_v1` playbook gives the canonical procedure for unique DOM roots,
descriptor paging and the transition from measured functions to receivers.
Measure an observed function on its containing receiver, then extend the recipe
to the function itself. For a constructor, follow its observed data-valued
prototype, inspect `prototype_instances`, and append `unique_instance` with the
measured constructor hash, a complete candidate budget and observed literal
invariants. Preview positions never identify instances; empty invariants require
the complete candidate count to equal one.

For captured state, inspect `function_scopes` on the measured function recipe
and append `scope_binding` for a supported observed data binding. Its `scope`
object copies the operation's `chain_sha256` and the selected scope's `index`,
`description` and `bindings_sha256`. After either route, describe the resolved
receiver and measure the intended method there before declaration. These are
existing bounded metadata routes; neither establishes callable types or task
proof. Scope and instance previews have no offset paging.

Inspector discovery feedback includes bounded metadata and exact raw artifact
pointers. Names and measured hashes precede optional source excerpts; summary
omissions, provider truncation and paging remain explicit. Function bodies and
app strings stay untrusted data, and complete raw observations remain available
through the ordinary evidence reader.

The reasoning driver's default context limit is 32,000 characters to accommodate
the shared capability descriptions. Explicit caller limits remain hard limits:
an oversized request is refused without truncating the task or sending it.

The full-native reasoning driver also supports an explicit `--decision-corrections 1` (maximum 3). A request rejected by the pre-dispatch request validator is retained and returned as rejected model output, with its actual validation error; it supplies no application evidence. A new decision consumes the existing decision, model, time, spend and context budgets and must pass the same schema and capability gates before dispatch. This does not repair model text, retry a tool, recover transport/output-integrity failures, or clear uncertain native effects. The default is zero corrections.

The typed native and inspector prompts include a historical index of admitted metadata queries (up to 32 whole operations and 6,000 JSON characters, further limited by remaining context). It retains exact query scopes and evidence links, marks missing outcomes unknown, and checks source pins again before dispatch. It neither treats old metadata as current process state nor suppresses repeated queries. If its empty envelope cannot fit, the entire optional index is omitted with counts and reason in the retained manifest; existing evidence and context limits remain unchanged.

When the model adapter is explicitly configured with `structured_output_mode=json_object`, the full-native wire schema permits omission of API fields already marked optional by the protocol. Omission and explicit `null` produce the same effective request without inserting defaults. Other nullable fields remain required, including root `request` and typed-nil `value`; selectors, signatures, return types, unknown-key refusal and the runtime/evidence/proof gates are unchanged. The default `json_schema` mode keeps strict explicit-null wire fields. Retained producer inputs preserve their original mode for investigation-memory validation.
