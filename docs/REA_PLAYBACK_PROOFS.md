# Fixed playback operations in the REA catalog

The `guest_fixed_state_v1` backend saves a measured native operation with no
caller parameters. A playback case identifies the current process, window and
local media separately from `inputs: {}`. Its proof covers a fixed transition
across measured trials and a witnessed restart; it does not establish argument
generalization or discovery across arbitrary apps.

An agent still discovers the method, typed arguments and receiver path from
the target build. The shared runner executes that declarative actor. The
playback observer independently reads a frozen public AX control and captures
actual display pixels from a frozen region of the foreground video window.
It requires nonblack moving video before the action and nonblack stable video
afterward, with the same process epoch, window, media, geometry and display.
It decodes retained PNGs again when validating or publishing a proof.
The first observer version requires one display; multiple video windows on
that display are supported. It rejects a desktop with multiple displays.
AX/Quartz window binding, per-window alpha captures and display frames are
sequential measurements checked with metadata bookends. They do not provide
an atomic compositor guarantee. A visible overlapping window is accepted only
when its retained alpha pixels are exactly transparent throughout the video region.

## Freeze the campaign

Use `anyuse map rea agent begin` with the ordinary task spec and `params: []`.
The versioned plan contains prospective trial IDs, empty inputs and explicit
restart requirements. For example, create its plan with:

```python
plan = {"format": "anyuse.rea-fixed-state-campaign-plan/1"}
for phase in ("learning", "verification", "heldout"):
    plan[phase] = [
        {"id": f"{phase}-{i}", "inputs": {},
         "restart": phase == "heldout" and i == 0}
        for i in range(3)
    ]
```

Each phase needs 3–32 actual trials. IDs are bookkeeping, not artificial input
values. Do not prefill hashes for evidence that has not been produced.

Propose a profile with format `anyuse.rea-fixed-state-profile/1`, an ID and
label, effect `fixed_state_transition`, `params: []`, and scope
`fixed_transition_across_declared_trials_with_heldout_restart`. Its `policy`
uses `anyuse.rea-playback-task/1`: freeze the target fingerprint, phase minima
of three each, before `playing`, after `paused`, and a temporal playback
observer. The observer freezes:

- The public control's exact AX path, role, identifier and playing/paused titles.
- A normalized video rectangle, 3–8 frames, sampling interval, nonblack threshold
  and separated motion/stillness thresholds.

Measure these on your target; a path or title from another app is not a usable
observer. Profile evidence uses the normal agent citations to retained session
artifacts. The callable actor remains separate from the observer.

Freeze a content region that excludes transient pause overlays and playback
controls before recording. Check the fixture's entire brightness cycle: every
sampled phase must remain safely nonblack. A validation run correctly refused
a dark fixture phase. Keep that failure and the verifier thresholds; changed
fixtures need a new prospective campaign.

## Prepare macOS staging storage

For a nonroot process on macOS, Frida 17.23.3 stages its helper and agent under
`g_get_user_cache_dir()/frida`, independently of `TMPDIR`.
See the [versioned Frida implementation](https://github.com/frida/frida-core/blob/17.23.3/src/darwin/system-darwin.m#L593).
GLib uses `XDG_CACHE_HOME`; its XDG default gives `~/.cache/frida`.
See the [GLib cache API](https://docs.gtk.org/glib/func.get_user_cache_dir.html)
and [XDG cache-directory default](https://specifications.freedesktop.org/basedir/latest/#environment-variables).

Choose writable dedicated directories with room for staged binaries, media and
retained screenshots. Export both paths before starting Python or Frida; GLib
caches its directory choice:

```sh
run_storage="/path/to/writable/run"
mkdir -p "$run_storage/cache" "$run_storage/tmp"
export XDG_CACHE_HOME="$run_storage/cache"
export TMPDIR="$run_storage/tmp/"
# Start the runner/Python process from this shell.
```

In a VM metadata check, the helper and agent were observed under the
configured `cache/frida` directory and disappeared during cleanup. The script
unloaded, the session detached and the manager closed, with zero goal
invocations. That check covered staging and metadata cleanup only.

## Coordinate public setup and restarts

After the agent has proposed its approved fixed-state hypothesis, a deterministic
coordinator can prepare the clips, run the trials, learn, witness declared
restarts, verify and publish without further model decisions:

```sh
anyuse map rea agent coordinate /path/to/session /path/to/configuration.json \
  --out /new/campaign
```

The caller-private configuration uses format
`anyuse.rea-campaign-coordinator/1`. It freezes the absolute `session`, exact
`target`, `plan_sha256`, `observer_profile_sha256`, `owned: true`, full current
`expected_identity`, and one `{id, media: {file, bytes, sha256}}` per planned
case, in learning, verification and held-out order. All media bytes and paths
must be distinct. These are measured caller values, not model guesses.
It also declares:

- `launch_environment`: an empty object, or explicit existing writable
  `CFFIXED_USER_HOME`, `TMPDIR` and `XDG_CACHE_HOME` directories. The process
  launch preserves `HOME`.
- `limits`: integer `seconds` (30–7200), `setup_seconds` (1–35),
  `restart_seconds` (1–60), `trial_seconds` (5–60) and `max_actions` (1–256).
- `publication`: bounded `summary` and `details` strings.
- Optional `storage`: closed integer `reserve_bytes`, `action_headroom_bytes`
  and `max_observed_decline_bytes` (each 1 MiB–1 TiB, with headroom no larger
  than the campaign decline bound). Defaults are 512 MiB, 128 MiB and 4 GiB.

Before each action, the coordinator requires the reserve plus the estimated
per-action headroom on the filesystems containing its output, the G session,
and explicit launch-storage directories. It retains available-byte samples
before and after callbacks, including failed callbacks, and counts positive
free-space declines across action boundaries and resumes. The campaign decline
bound applies to their sum across all watched filesystems. A crossed reserve,
headroom overrun, exhausted decline budget, unknown measurement, or changed
storage directory/device stops the campaign without another action or retry.
The journal binds the policy and directory identities. These checks are
admission and checkpoint observations, not a hard disk quota: an action can
consume more than its estimate before returning, and concurrent writes also
contribute to observed declines. Arbitrary backend storage outside these
paths is not measured. Callers should choose headroom for their evidence
size and copying costs. A full disk can also prevent retaining a final error;
an unfinished journal remains non-resumable.

The fixed backend uses ordinary app launch/open, public Accessibility Play,
and a single normal owned-app quit at each declared restart. It does not use
the discovered native goal to set up a trial. Each trial receives a pinned,
single-use private preparation receipt; G independently checks its process,
media, hypothesis, observer, producer and restart chronology before execution.
No serialized callback, shell or model-generated setup is interpreted.

Use `--actions-per-run` to stop at a completed action boundary. Continue that
same configuration/output with `--resume`; completed setup and restarts are
not repeated. Failed, interrupted or uncertain actions cannot resume or retry,
and changing the output folder cannot clear the session binding. These paths
pass guarded tests with inert lower-level effects; combined live validation
of the new coordinator remains pending.

## Record and promote measured trials

Ordinary public setup must leave the intended local clip playing in the
foreground main window. A playback trial does not launch, play, reset, or
activate the app. Each operation runs one case, in frozen order:

```json
{"op": "record", "case": 0}
```

Prepare the next playing state publicly, then record cases 1 and 2. Call
`{"op":"learn"}` after all learning cases. Repeat public setup and
`{"op":"verify","case":0}` for each fresh case. Fresh verification retains
a `fresh_verified` receipt; that receipt cannot publish an action.

Before the declared restart held-out case, retain a real process restart
witness using `execution.rea_process_restart.run_restart`. Its stop and launch
hooks are trusted local setup code, never serialized model callbacks. The
witness measures the old process, its absence and the actual newly launched
process. It must occur after an earlier measured case and before the held-out
trial. Pass exact file/size/SHA-256 pins for its intent and receipt:

```json
{
  "op": "heldout",
  "case": 0,
  "restart": {
    "intent": {"file": "/guest/run/restart/intent.json", "sha256": "MEASURED_SHA", "bytes": 123},
    "receipt": {"file": "/guest/run/restart/receipt.json", "sha256": "MEASURED_SHA", "bytes": 456},
    "predecessor": "verification-2"
  }
}
```

The example pins are placeholders. Remaining held-outs use their frozen case
indices and omit `restart` unless their plan declares another restart. The
consumer reopens the restart witness and binds its complete old/new identities,
kernel epochs and times to the actual trials. A changed PID alone is insufficient.

After all held-outs pass, use the existing `publish` tool with a bounded summary
and details. The resulting card and catalog entry bind the policy, actor,
candidate, complete proof and app build. A verification or held-out failure
closes that evaluation. Retain it and start a new campaign for a changed recipe.

## Reuse

The existing action chooser can select the catalog entry. Execute it through
`anyuse map rea execute` with `inputs.json` containing `{}`. Replay checks the
proof and current resources, finds the current foreground media window anew,
checks its playing state, invokes the saved actor once, and independently
checks the result. Replay makes no model calls; a model may be used separately
to choose the action.

A paused starting window, black/frozen video, missing or obscured window,
changed media/process, altered evidence, or failed cleanup cannot yield a
successful action. No automatic public-control fallback or hidden setup is
performed. The measurement establishes public playback state and the declared
video region's temporal pixels, not every aspect of the application.

## Measured native validation

The October 2026 validation used IINA 1.5.0 build 180 in an owned macOS VM,
through a copy with a debugging signature and unchanged compiled code/data
sections. One saved native pause mapping passed nine distinct synthetic
clips/windows: three learning, three fresh and three held-out trials, with a
separately witnessed normal app restart between fresh and held-out phases.
The complete proof was promoted through the normal agent workflow into a local
card and catalog. A fresh agent read that card's description and contract,
selected it, and executed the same mapping on a tenth new clip/window. That
replay made one pause invocation and zero model-provider calls. A subsequent
already-paused replay refused before native dispatch, without public setup or
a second pause call.

The independent audit re-decoded the actual display and overlapping-window
alpha PNGs and reopened the native receipts, frozen sources, media, restart
witness and catalog bindings. The measurement used a single display, a fixed
lower-middle video region and empty caller inputs. Earlier failed evaluations
were retained separately. It establishes reuse of this discovered action;
broader discovery odds, arbitrary apps and argument generalization were not
measured.

The isolated IINA copy used the public mpv option `gpu-dumb-mode=yes` with
Apple's software OpenGL renderer. Fresh video windows had rendered black before
that setting; the successful campaign used it throughout. This is a measured
VM fixture setting, not a requirement established for other machines or apps.
