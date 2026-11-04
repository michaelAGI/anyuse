# Declared document restoration

`execution.rea_document_restoration` provides a saved-document recovery contract
independent of app-specific private selectors. It creates a private working copy,
keeps a separate immutable byte baseline, observes its exact document window and
then restores that declared scope. It uses existing drive authorization,
foreground/permission guards and ordinary Accessibility/NSWorkspace operations.
It does not request permissions, execute generated commands or terminate an app.

Earlier live checks on 22 October 2026 verified declared saved-copy recovery in
TextEdit and Preview. TextEdit covered open and initially closed plain-text
copies and refusal after an external file edit; Preview covered open and
initially closed PDFs and ordinary cleanup of its owned app instance. These
checks used file hashes and independent Accessibility observations. Retained
screenshots provide additional, partly occluded visual evidence.

Those checks predate the explicit text-buffer contract below. Its live refusal
check and retained-evidence validation are separate from the earlier saved-copy
results. None of these checks establishes restoration of arbitrary private app
state or support for Photoshop, Acrobat or arbitrary native apps.

## Supported scope

A contract covers one caller-pinned regular document, copied into a fresh private
workspace; its saved bytes, open/saved status and window geometry; and up to
sixteen explicitly declared public `AXValue` fields. Each field identifies a
bounded child path, measured role and nonempty AX identifier. The adapter checks
ownership, scalar type and settable state and reads the value back after writing.
Complete canonical public-state observations are limited to256KiB, including all
declared values and geometry, so both capture and final recovery receipts have
space within their2MiB envelope. A field with `restore=False` is still required to equal its checkpoint; the
framework will report incomplete recovery if it changed.

The exact process epoch is pinned, but the process may already be running: only
the unique window whose `AXDocument` URI names the task copy is closed/reopened.
Other documents/windows are explicitly excluded and never closed. The contract
therefore reports recovery of its declared document/fields, not recovery of every
window a task might open. Tasks needing a larger document set must declare/use
separate contracts; this first coordinator integration accepts one contract.

Receipts always name unsupported `private_memory`, `unsaved_document_buffers`,
`undeclared_app_state` and `other_document_windows`. Selections/zoom or similar
values are supported only when explicitly represented by a measured public field
or an authorized typed adapter. Undo history and arbitrary hidden app state have
no generic adapter. An unavailable AXDocument, AXEdited, geometry, dirty-state
check, close completion or readback causes refusal/incomplete recovery rather
than an inferred success.

## Python workflow

Run the public adapter only in the authorized test guest. The caller obtains the
actual complete process identity and app fingerprint through the existing runtime
and mapping APIs, and opens the returned working document normally before capture.
The original input remains a read-only source and is never a restoration target.

```python
from anyuse.execution import rea_document_restoration as D

workspace = D.create_workspace(D.pin(original), fresh_workspace)
# Open workspace.json's document.file in the declared app through a public route.
checkpoint = D.capture(workspace, actual_identity, target_fingerprint,
                       measured_public_fields, fresh_checkpoint_folder)
# Perform and finish the task, including normal Save when content was edited.
post_task = D.current_document(checkpoint)
result = D.restore(checkpoint, post_task, fresh_recovery_folder)
```

Use `fields=[]` when no additional app values are declared and the complete
owned-window hierarchy exposes no `AXTextArea`. Saved bytes,
open/saved status and geometry remain mandatory observations; this is not a
vacuous all-state success. A declaration has exactly `id`, `path`, `role`,
`identifier`, `restore`; for example a *measured* slider may use
`{"id":"zoom","path":[0,2],"role":"AXSlider","identifier":"actual-zoom-id","restore":true}`.
Do not guess or reuse another app's field path/identifier.

When the owned document exposes an `AXTextArea`, the public adapter also requires
an explicit plain-text buffer contract. A false public dirty flag alone does not
establish that the visible buffer matches disk. Declare its actual field using
`restore=False`, then bind that field to exact UTF-8 text:

```python
fields = [{"id": "document.text", "path": measured_path,
           "role": "AXTextArea", "identifier": measured_identifier,
           "restore": False}]
text_buffer = {"field": "document.text", "encoding": "utf-8", "newlines": "exact"}
checkpoint = D.capture(workspace, actual_identity, target_fingerprint, fields,
                       fresh_checkpoint_folder, text_buffer=text_buffer)
```

Exactly one text area must be observed and it must match the declared path,
identifier, process and exact `AXDocument` window. Missing or ambiguous fields,
secure fields, unknown encoding or failed reads refuse. The supported format is
strict UTF-8 without BOM or NUL, bounded to128KiB, within the existing aggregate
state bound. There is no trimming, Unicode, BOM or newline normalization:
CRLF bytes and an LF-only visible value differ and refuse. Other encodings,
rich-text serialization and inaccessible document state remain unsupported.
The declared buffer has this separate string bound; other public scalar limits
remain unchanged. Only this declared `AXTextArea` can report the known
unsupported-subrole event (`-25205`); other field checks retain their strict
nonsecure-subrole requirement.

Checkpoint capture requires buffer/disk equality. Recovery and direct public
close require both `AXEdited=False` and that equality, with full expected
post-task file identity checked before close. The buffer and current disk are
read again immediately before the public close action. A differing buffer
refuses before close/overwrite even when the app reports `AXEdited=False`;
the recovery receipt retains that raw flag and the bounded buffer/disk guard
evidence. Direct `PublicAXDocument.observe` also refuses a differing buffer.
Contexts passed directly to public `observe` or `close` must carry the same
`fields` and `text_buffer` declaration. This detects unsupported unsaved state;
it does not restore that buffer or authorize a whole-value text write.

Old checkpoints keep their original scope and never gain a buffer-safety claim.
The public adapter refuses a visible text area without the declaration, so an
old empty-field checkpoint cannot bypass the guard. Documents whose complete
owned-window tree has no text area retain the existing saved-status path.

A directly supplied `TrustedHooks` value supports app-owned structured APIs or
inert fixtures with the same closed observation/readback protocol. Its four
explicit Python callbacks and producer file pins are passed by authorized caller
code. No callback/import/command is loaded from a JSON contract. Public AX support
is deliberately restricted to measured nonsecure scalar controls.

## Recovery and failure behavior

Recovery verifies the private workspace directory and immutable baseline identities,
freezes current bytes plus live inode/mtime identity, and checks them again after
verified normal close. Symlinks, hardlinks, replacement workspace directories,
an observed intervening file edit or an unwitnessed process replacement refuse
recovery. A normal Save that replaced the working inode before the post-task
observation is supported. The baseline replaces only the private working leaf
atomically, after retaining the post-task bytes. Source originals are never
written. Advisory file locking and private directory ownership coordinate trusted
writers; this is not a quota or protection against hostile same-user processes
racing filesystem syscalls.

The adapter must observe a readable `AXEdited=False`, no document sheet and no
app dialog before closing, plus the declared buffer comparison when a text area
is present. The public dirty flag may be exposed on the window or
on its exact `AXCloseButton`: the latter must have the same process owner,
`AXButton` role, `AXCloseButton` subrole and matching `AXWindow` association.
Both supported placements must agree. Unsupported/no-value attributes permit
checking the other placement; transport/permission errors, non-booleans or a
missing dirty flag refuse recovery. File hashes, menu enablement and window
titles never substitute for the dirty flag.

Every enumerated app window must also expose an actual boolean `AXModal=False`;
missing/unsupported, malformed or failed modal reads remain unknown and refuse
admission. A modal standard/floating window is refused even if it is unrelated
to the task document and has no dialog subrole.

Sheet/dialog admission uses the published `AXWindows` and complete bounded
`AXChildren` hierarchy with actual roles/subroles and any supported `AXModal`
value, rather than requiring a nonstandard `AXSheets` attribute. Explicitly
unsupported/no-value child attributes are leaves in this public hierarchy;
unreadable required roles, failed reads, cycles, unknown roles, modal nodes or
an exceeded traversal bound refuse recovery. This observes the app's exposed
Accessibility hierarchy; it does not establish the state of inaccessible private
UI. The exact document/window and dirty flag are checked again after observation.

The hierarchy query plan requests subroles for actual `AXWindow` elements,
including nested windows, where the subrole distinguishes a dialog. It does not
query unrelated group/button subroles just because an app advertises them.
Required close-button association/subrole and declared nonsecure field checks
remain separate. No AX read error is converted into a successful observation;
required role/modal/children reads and any requested subrole still refuse on
failure. A failed Preview capture that exposed this query-planning gap remains
failed; the change requires a new live attempt.

If the task or a witnessed restart already closed the
working document, it first reopens that exact path in the validated process and
verifies its saved state; absence alone never authorizes file replacement. It verifies disappearance of that exact document/window
before file replacement. It then reopens normally, verifies the process and exact
AXDocument, restores declared settable state, and independently checks reopened
state and the file hash. One final observation checks all geometry and declared
fields together, so coupled setters cannot conceal a changed earlier value. A
successful command return alone cannot produce
`restored`. A failed reopen/readback after byte replacement reports `partial`.
The result names every required/excluded scope and retains the primary failure.
`KeyboardInterrupt`/`SystemExit` retain the recovery receipt and propagate; no
automatic retry occurs. An attempt folder is exclusive and cannot be reused.

### Optional witnessed Darwin metadata boundary

The default contract requires exact content, inode, mtime and ctime identity
through close/reopen. Some public document opens update the filesystem's
`com.apple.lastuseddate#PS` attribute. A separate controlled TextEdit probe
observed a 16-byte addition with unchanged content, inode, mtime and security
fields. That observation is not proof that any later metadata change was caused
by the app. The failed earlier recovery remains failed.

An authorized caller can declare this narrow policy when creating a **new**
checkpoint with the fixed public AX adapter:

```python
checkpoint = D.capture(workspace, actual_identity, target_fingerprint, fields,
                       fresh_checkpoint_folder,
                       metadata_policy=D.DARWIN_LASTUSED)
```

This declaration is frozen in the checkpoint and inherited by Q's optional
required-restoration workflow. It does not authorize arbitrary callbacks,
`TrustedHooks`, a custom adapter subclass, or a later policy change. Existing
checkpoints and the already-open document path retain their strict behavior.
Combining this metadata exception with `text_buffer` is currently unsupported
and is refused before any app observation/action. Exact UTF-8 recovery without
metadata drift remains available; metadata changes are never silently rebased
to make a buffer comparison pass.

For an initially closed document only, recovery holds one `O_NOFOLLOW` file
descriptor across its own preliminary reopen and verified saved close. Before
the first public observation, the held file and path must exactly match the
original `expected_current`. Bounded Darwin `flistxattr`/`fgetxattr` reads include
compression attributes; canonical `acl_get_fd_np`/`acl_copy_ext` reads capture
ACLs independently. These are public filesystem reads with ordinary
authorization, with no child process or security-bypass flag. The complete
stat/security/content observation must remain stable during each sample.

Across that observed boundary, only a new or changed
`com.apple.lastuseddate#PS` value of exactly 16 bytes may accompany a ctime
change. Bytes, device/inode, size, mtime, ownership, permissions, links, flags,
generation, birthtime, allocation fields, ACL and every other xattr must match.
Atime is excluded because the verification reads themselves can update it.
Deletion, ctime-only drift, an unknown value, any other delta, unsupported ACL
or metadata reads, or an exceeded bound refuses replacement. Complete reads
allow at most 32 xattrs, 64 KiB per value and 256 KiB total, plus a 64 KiB ACL.
Darwin reports a missing `FILESEC_ACL` property as `NULL` with `ENOENT`;
the reader records explicit ACL absence while independently checking that the
held descriptor and canonical path still identify the same file. Other ACL
errors refuse recovery. See Apple's [ACL reader](https://github.com/apple-oss-distributions/Libc/blob/main/posix1e/acl_file.c)
and [file-security property reader](https://github.com/apple-oss-distributions/Libc/blob/main/gen/filesec.c).

The receipt retains the original post-task identity and both bounded metadata
observations with an explicit admitted delta. The accepted post-boundary full
identity and metadata are checked again under the advisory lock and immediately
before atomic replacement; later drift is refused. This is a declared lifecycle
admission, not external-file rebasing or universal metadata restoration. The
scope remains saved bytes and the declared public document state; private
memory, undeclared state and other windows remain unsupported/excluded.

## Proof/catalog integration

### Fresh restoration around a saved-action replay

The Python `rea_actions.execute` API separately supports an explicit fresh
capture declaration for that invocation:

```python
result = A.execute(catalog, selection, inputs, fresh_transaction_folder,
    timeout=30,
    restoration={"workspace": workspace, "identity": actual_current_identity,
                 "fields": measured_public_fields, "seconds": 30})
```

`workspace` is the task-owned saved-copy pin returned by `D.create_workspace`;
the caller must operate on that copy. `identity` is the complete current process
identity for the selected action's app. The optional `text_buffer` and
`metadata_policy` declarations retain their existing supported scopes and
mutual exclusion. Explicit Python callers can supply an existing authorized
`restoration_backend`; serialized declarations cannot name or load an adapter.
Historical catalog checkpoints are never used as fresh capture authority.

Before replay, the wrapper captures and validates a new checkpoint and checks
the current process epoch. Unsupported, unsaved, stale or failed capture stops
before replay. The captured epoch is passed through the replay to its native
worker, fixed playback trial or visual trial; a later process with the same
bundle is refused.
This option supports native learned actors, fixed playback and model-judged
visual replay, including inspector actors. Other replay routes refuse before
capture. The pinned runtime context requires an existing app and does not launch
or activate one. Capture
and restoration each have their own integer `seconds` bound (1–60); replay
retains its separate `timeout` (1–300). These are cooperative API deadlines,
not a hard total wall-clock limit.

With this option, the fresh output folder contains `checkpoint/`, `replay/`,
`restoration/` and an `execution.json` receipt. The returned transaction keeps
the original replay result in `operation.result` and cleanup status/receipt in
`restoration`. `completion_status` is `completed` only when replay passes its
declared verification tier and restoration succeeds. Visual results retain
`model_judged`. A passing replay with incomplete cleanup reports
`cleanup_incomplete`; a refuted or uncertain replay remains `primary_failed`
regardless of cleanup status. Both retain the original result. An original replay exception is re-raised
unchanged after attempting cleanup and retaining both outcomes. Uncertain runtime
effects or cleanup leave restoration `unknown` without new app actions.
A caller-private in-memory disposition is latched before task dispatch and
confirmed only by the existing runtime/cleanup validators. It survives an
interruption or a failure to retain replay logs; failure to establish clean
completion keeps restoration unknown. A clean worker followed by an independent
observer refusal can still permit restoration. This disposition and
`execution.json` are diagnostics, never proof or catalog authority.
There is no automatic retry or successor adoption. Recovery covers the declared
task-owned saved-copy bytes, open/saved status, document geometry and explicit
public fields. It does not restore private state, unsaved buffers or other
documents.

Without the option, `execute` retains its original replay output layout and
return value. This API addition has guarded inert regression coverage; live
saved-action transaction validation remains pending.

The optional coordinator configuration is:

```json
{"restoration":{"checkpoint":{"file":"/absolute/checkpoint.json",
 "bytes":123,"sha256":"actual-checkpoint-sha256"},"seconds":30}}
```

These are fields added to an otherwise complete Q configuration, not a standalone
configuration. `seconds` reserves 1–60 seconds *inside* the original campaign
limit; bounded setup/restart/trial timeouts are clamped to the work cutoff and
refused when the minimum trial allowance cannot fit. This does not extend the
authorization or lease. Trusted Python adapters must honor their supplied
deadlines; this is cooperative admission, not a hard scheduler/process quota. Required restoration
currently disallows partial-run/resume, because restoring a prepared case could
invalidate its frozen next-trial context. Existing campaigns without this field
keep their completed-boundary resume behavior.

Q runs restoration once before publication, and also after task failure or
interruption. A failed primary task remains failed after successful cleanup.
An interrupted G boundary, missing positive final disposition for a failed
native-capable action, unknown native cleanup, source/configuration drift, expired original deadline,
or unexplained process epoch leaves recovery unknown without new app actions.
A successor is accepted only through the existing independently validated restart
intent/receipt chain, never a new PID lookup or arbitrary identity substitution.

G refuses required publication until the retained recovery verifies. The catalog
binds the checkpoint and recovery receipt; catalog validation/selection/execution
recheck those immutable historical pins. They do not require the live document to
remain at its old baseline after a later legitimate task. Existing execution
freshness/effect checks continue independently. Removing/changing a required
receipt invalidates that catalog; incomplete recovery never becomes a published
verified operation. Journal `primary_outcome` and `restoration` remain separate; successful proof work
stays `proof_completed` if recovery/publication then fails.

For a declared `text_buffer`, required-scope verification also checks the
retained guard evidence. Each successful guard has the exact contract, matched
status, `matches=True`, `AXEdited=False`, a bounded buffer byte/hash pin and a
typed full disk leaf observation. Buffer bytes/hash must equal disk bytes/hash.
The framework records explicit observation phases, contiguous sequence numbers,
the action-list position and monotonic observation time; verification does not
infer these boundaries from an unlabelled list.

Exactly one initial (or preliminary-reopen), before-close, after-reopen and
final-saved-state witness is required in order. Before-close evidence binds the
full original `expected_current` leaf identity. After-reopen evidence binds the
restored document byte pin and immutable baseline text. The final-saved-state
witness repeats that check after all declared state adjustments.
Unknown/reversed phases, missing/duplicate required
witnesses, contradictory flags or pins, or missing/unlabelled guard logs refuse
publication/catalog verification even if the final field values match. This is
validation of retained historical observations, not a new live-state check or a
guarantee about unobserved intervals. Legacy checkpoints without a text-buffer
declaration retain their existing verification scope.


## Standalone storage admission

The standalone workspace, checkpoint and recovery APIs measure available bytes
at their actual destination parents before creating output or calling an app
adapter. Each filesystem retains512MiB plus its aggregate known remaining
allocation envelope. Workspace admission covers two source copies and a bounded
manifest; checkpoint admission covers its bounded receipt; recovery covers the
post-task backup and both attempt/final receipts on the output filesystem, plus
the baseline temporary on the working filesystem. A shared filesystem is summed,
not charged as separate reserves. Known completed allocations are removed before
subsequent checks; receipt allowance remains reserved until final retention.
Recovery rechecks before public actions and file replacement. Unknown/failed
free-space measurements refuse before action. Admission is cooperative and
observational; it is not a quota and cannot attribute or prevent unrelated disk
writes. Later failures preserve partial/unknown state and never authorize retry.
