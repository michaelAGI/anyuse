"""Explicit recovery of task-owned document copies and declared public state.

Original files are read-only inputs. A workspace creates its own working copy;
recovery never accepts a destination outside that workspace. This is scoped
document/public-state recovery, not arbitrary application or memory rollback.
"""
from __future__ import annotations

import copy
import ctypes
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import time

from . import rea_mapping as R
from . import rea_ax_state as S
from . import rea_process_restart as RR

WORKSPACE = "anyuse.rea-document-workspace/1"
CHECKPOINT = "anyuse.rea-document-checkpoint/1"
OUTCOME = "anyuse.rea-document-restoration/1"
MAX_BYTES = 64 * 1024**2
MAX_STATE_BYTES = 256 * 1024
MAX_TEXT_BYTES = 128 * 1024
MAX_RECEIPT_BYTES = 2 * 1024**2
STORAGE_RESERVE_BYTES = 512 * 1024**2
DARWIN_LASTUSED = "darwin-public-preliminary-reopen-lastuseddate/1"
LASTUSED_ATTRIBUTE = "com.apple.lastuseddate#PS"
MAX_XATTRS = 32
MAX_METADATA_BYTES = 256 * 1024
UNSUPPORTED = ["private_memory", "unsaved_document_buffers", "undeclared_app_state", "other_document_windows"]


def need(value, message):
    if not value:
        raise ValueError(message)


def _budget(context):
    need(time.monotonic() < context["deadline"], "document recovery deadline expired")


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def _directory(path):
    path = Path(path)
    info = path.lstat()
    need(path.is_absolute() and path.resolve(strict=True) == path and stat.S_ISDIR(info.st_mode) and
         not path.is_symlink() and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0,
         "canonical private same-user task directory required")
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino}



def _storage_directory(path):
    path = Path(path)
    info = path.lstat()
    need(path.is_absolute() and path.resolve(strict=True) == path and stat.S_ISDIR(info.st_mode) and
         not path.is_symlink(), "canonical existing storage destination parent required")
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino}


def _storage_plan(*allocations):
    return [{"directory": _storage_directory(parent), "remaining_bytes": count} for parent, count in allocations]


def _storage_admit(plan):
    """Observed per-device free-space admission, never a hard filesystem quota."""
    need(type(plan) is list and 1 <= len(plan) <= 4, "bounded storage destination plan required")
    devices = {}
    for allocation in plan:
        need(type(allocation) is dict and set(allocation) == {"directory", "remaining_bytes"} and
             type(allocation["remaining_bytes"]) is int and 0 <= allocation["remaining_bytes"] <= 4 * MAX_BYTES,
             "bounded known remaining document allocation required")
        directory = allocation["directory"]
        need(_storage_directory(directory["path"]) == directory, "storage directory identity changed before sample")
        free = os.statvfs(directory["path"])
        need(type(free.f_bavail) is int and free.f_bavail >= 0 and type(free.f_frsize) is int and free.f_frsize > 0,
             "known available document storage measurement required")
        available = free.f_bavail * free.f_frsize
        need(_storage_directory(directory["path"]) == directory, "storage directory identity changed during sample")
        row = devices.setdefault(str(directory["device"]), {"device": directory["device"], "available_bytes": available,
                                                           "remaining_allocation_bytes": 0, "directories": []})
        row["available_bytes"] = min(row["available_bytes"], available)
        row["remaining_allocation_bytes"] += allocation["remaining_bytes"]
        row["directories"].append(copy.deepcopy(directory))
    for row in devices.values():
        need(row["available_bytes"] >= STORAGE_RESERVE_BYTES + row["remaining_allocation_bytes"],
             "insufficient document storage reserve plus remaining allocation envelope; no action")
    return {"observed_monotonic": time.monotonic(), "reserve_bytes": STORAGE_RESERVE_BYTES,
            "filesystems": list(devices.values())}


def _read(path, maximum=MAX_BYTES):
    path = Path(path)
    before = path.lstat()
    need(path.is_absolute() and path.resolve(strict=True) == path and stat.S_ISREG(before.st_mode) and
         not path.is_symlink() and before.st_nlink == 1 and before.st_uid == os.getuid() and
         0 <= before.st_size <= maximum, "bounded canonical same-user nonlinked document required")
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as stream:
        need(identity(os.fstat(stream.fileno())) == identity(before), "document changed before read")
        data = stream.read(maximum + 1)
        need(len(data) == before.st_size and identity(os.fstat(stream.fileno())) == identity(before),
             "document changed during read")
    need(identity(path.lstat()) == identity(before) and path.resolve(strict=True) == path,
         "document path changed during read")
    return data, {"file": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}, identity(before)


def pin(path):
    return _read(path)[1]


def _verified(expected):
    need(type(expected) is dict and set(expected) == {"file", "bytes", "sha256"} and
         type(expected["file"]) is str and type(expected["bytes"]) is int and 0 <= expected["bytes"] <= MAX_BYTES and
         type(expected["sha256"]) is str and R.SHA.fullmatch(expected["sha256"]), "closed bounded document pin required")
    raw, actual, _ = _read(expected["file"])
    need(actual == expected, "frozen document bytes changed")
    return raw


def _save(path, value):
    raw = _json(value) + b"\n"
    need(len(raw) <= MAX_RECEIPT_BYTES, "bounded recovery receipt required")
    with Path(path).open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    return pin(path)


def create_workspace(source, out):
    """Copy one pinned input into a fresh recovery-owned directory.

    A caller must open and operate on the returned ``document`` copy. The
    source pin is retained as provenance and is never a recovery destination.
    """
    raw = _verified(source)
    root = Path(out)
    need(root.is_absolute() and root.parent.resolve(strict=True) == root.parent and
         not root.exists() and not root.is_symlink(), "fresh absolute task workspace required")
    storage = _storage_plan((root.parent, 2 * len(raw) + MAX_RECEIPT_BYTES))
    samples = [_storage_admit(storage)]  # Before mkdir/copies or any app action.
    root.mkdir(mode=0o700)
    working, baseline = root / "working", root / "baseline"
    working.mkdir(mode=0o700); baseline.mkdir(mode=0o700)
    # Separate owned workspaces can be open together. Give their working
    # copies distinct public filenames so title-based window identity stays
    # unambiguous even when the source and window geometry are identical.
    name = "document-" + hashlib.sha256(os.fsencode(root)).hexdigest()[:16] + Path(source["file"]).suffix
    for file in (working / name, baseline / name):
        samples.append(_storage_admit(storage))
        with file.open("xb") as stream:
            os.chmod(file, 0o600); stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        storage[0]["remaining_bytes"] -= len(raw)
    samples.append(_storage_admit(storage))
    value = {"format": WORKSPACE, "source": copy.deepcopy(source), "root": _directory(root),
             "working": _directory(working), "baseline_directory": _directory(baseline),
             "document": pin(working / name), "baseline": pin(baseline / name),
             "baseline_identity": list(_read(baseline / name)[2]), "storage_admissions": samples}
    return _save(root / "workspace.json", value)


def _workspace(expected):
    value = json.loads(_verified(expected))
    fields = {"format", "source", "root", "working", "baseline_directory", "document", "baseline", "baseline_identity"}
    need(type(value) is dict and set(value) in (fields, fields | {"storage_admissions"}) and
         value["format"] == WORKSPACE, "closed task-owned workspace required")
    root = Path(expected["file"]).parent
    need(Path(expected["file"]) == root / "workspace.json" and value["root"] == _directory(root) and
         value["working"] == _directory(root / "working") and
         value["baseline_directory"] == _directory(root / "baseline"), "workspace directory identity changed")
    document, baseline = Path(value["document"]["file"]), Path(value["baseline"]["file"])
    need(document.parent == root / "working" and baseline.parent == root / "baseline" and
         document.name == baseline.name and document != Path(value["source"]["file"]) and
         value["working"]["device"] == value["baseline_directory"]["device"], "task document destination changed")
    _verified(value["baseline"])
    need(value["baseline_identity"] == list(_read(baseline)[2]), "immutable baseline file identity changed")
    return value


def validate_fields(fields):
    need(type(fields) is list and len(fields) <= 16, "at most sixteen declared public fields required")
    seen = set()
    for row in fields:
        need(type(row) is dict and set(row) == {"id", "path", "role", "identifier", "restore"} and
             type(row["id"]) is str and 0 < len(row["id"]) <= 128 and not row["id"].startswith("window.") and row["id"] not in seen and
             type(row["path"]) is list and len(row["path"]) <= 8 and
             all(type(index) is int and 0 <= index < 128 for index in row["path"]) and
             type(row["role"]) is str and row["role"] in
                 {"AXSlider", "AXCheckBox", "AXRadioButton", "AXTextField", "AXComboBox", "AXTextArea"} and
             type(row["identifier"]) is str and 0 < len(row["identifier"]) <= 256 and
             type(row["restore"]) is bool, "closed measured public AXValue field required")
        seen.add(row["id"])
    return copy.deepcopy(fields)


def validate_text_buffer(value, fields):
    """One explicitly measured plain-text field; no encoding or newline guesses."""
    if value is None:
        return None
    need(type(value) is dict and set(value) == {"field", "encoding", "newlines"} and
         type(value["field"]) is str and value["encoding"] == "utf-8" and value["newlines"] == "exact",
         "declared exact UTF-8 text-buffer contract required")
    matches = [row for row in fields if row["id"] == value["field"]]
    need(len(matches) == 1 and matches[0]["role"] == "AXTextArea" and matches[0]["restore"] is False,
         "text buffer must name one declared non-restoring AXTextArea field")
    return copy.deepcopy(value)


def _plain_text(raw):
    need(len(raw) <= MAX_TEXT_BYTES and not raw.startswith(b"\xef\xbb\xbf") and b"\0" not in raw,
         "bounded UTF-8 plain text without BOM or NUL required; no normalization")
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("document encoding is not declared exact UTF-8 plain text") from error


def _text_buffer_guard(observed, context, *, expected=None, evidence=None, baseline=None):
    contract = validate_text_buffer(context.get("text_buffer"), context["fields"])
    if contract is None or not observed["open"]:
        return
    record = {} if evidence is None else evidence
    record.update(status="unknown", contract=contract, AXEdited=observed["edited"], matches=None)
    if baseline is None:
        _budget(context)
        raw, actual, identity = _read(context["document"], maximum=MAX_TEXT_BYTES)
        record["disk"] = {**actual, "identity": list(identity)}
        if expected is not None:
            _post_task_schema(expected, context)
            need(record["disk"] == expected, "post-task document bytes or leaf identity changed before buffer guard")
    else:
        raw = baseline
    text = observed["fields"][contract["field"]]["value"]
    need(type(text) is str, "known declared plain-text buffer value required")
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError("declared text buffer is not exact UTF-8") from error
    need(len(encoded) <= MAX_TEXT_BYTES, "declared text buffer exceeds its byte bound")
    record["buffer"] = {"bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
    record["matches"] = text == _plain_text(raw)
    need(record["matches"], "unsaved document buffer differs from exact UTF-8 disk text; no close or overwrite")
    if baseline is None:
        need(_read(context["document"], maximum=MAX_TEXT_BYTES)[1:] == (actual, identity),
             "document changed during text-buffer comparison")
        _budget(context)
    record["status"] = "matched_exact_disk_text"


def _document_scope(context):
    required = ["saved_document_bytes", "document_open", "saved_status", "window_geometry"] + [
        "field:" + row["id"] for row in context["fields"]]
    if context.get("text_buffer") is not None:
        required.append("declared_exact_utf8_buffer_matches_disk")
    return {"documents": [context["document"]], "required": required,
            "other_windows": "excluded; never closed or restored"}


def _verify_text_buffer_evidence(result, context, workspace, after):
    """Validate explicitly phased historical witnesses, never current app state."""
    contract = validate_text_buffer(context["text_buffer"], context["fields"])
    guards = result.get("text_buffer_observations")
    need(type(guards) is list and 4 <= len(guards) <= 128,
         "bounded phased text-buffer evidence required for declared recovery scope")
    actions = result.get("actions")
    closed = ["close_requested", "closed_verified", "bytes_restored_verified"]
    need(actions == closed or actions == ["preliminary_reopen_requested", "preliminary_reopen_verified"] + closed,
         "declared text-buffer recovery action boundaries changed")
    preliminary = len(actions) == 5
    initial_phase = "preliminary_reopen" if preliminary else "initial"
    phases = {initial_phase: 0, "before_close": 1, "after_reopen": 2,
              "state_adjustment": 3, "final_saved_state": 4}
    expected = result.get("expected_current")
    _post_task_schema(expected, context)
    final = result["document_after"]
    need(type(final) is dict and set(final) == {"file", "bytes", "sha256"} and
         final["file"] == context["document"] and type(final["bytes"]) is int and
         0 <= final["bytes"] <= MAX_TEXT_BYTES and type(final["sha256"]) is str and R.SHA.fullmatch(final["sha256"]),
         "closed restored document byte pin required for text-buffer evidence")
    start, finish = result.get("started_monotonic"), result.get("finished_monotonic")
    need(all(type(v) in {int, float} and math.isfinite(v) and v >= 0 for v in (start, finish)) and start <= finish,
         "bounded recovery chronology required for text-buffer evidence")
    counts, rank, previous_time = {}, -1, start
    required_keys = {"sequence", "phase", "action_index", "observed_monotonic", "status", "contract",
                     "AXEdited", "matches", "disk", "buffer"}
    for index, guard in enumerate(guards):
        need(type(guard) is dict and set(guard) == required_keys and
             type(guard["sequence"]) is int and guard["sequence"] == index and
             type(guard["phase"]) is str and guard["phase"] in phases,
             "closed ordered text-buffer witness required")
        phase = guard["phase"]
        need(phases[phase] >= rank, "text-buffer witness phases reversed")
        rank = phases[phase]; counts[phase] = counts.get(phase, 0) + 1
        observed = guard["observed_monotonic"]
        need(type(observed) in {int, float} and math.isfinite(observed) and previous_time <= observed <= finish,
             "text-buffer witness chronology differs from recovery")
        previous_time = observed
        expected_index = (1 if preliminary else 0) if phase == initial_phase else (
            actions.index("close_requested") if phase == "before_close" else len(actions))
        need(type(guard["action_index"]) is int and guard["action_index"] == expected_index,
             "text-buffer witness does not bind its declared action boundary")
        need(guard["contract"] == contract and guard["status"] == "matched_exact_disk_text" and
             guard["matches"] is True and guard["AXEdited"] is False,
             "text-buffer witness contradicts matched saved state")
        disk, buffer = guard["disk"], guard["buffer"]
        _post_task_schema(disk, context)
        need(disk["bytes"] <= MAX_TEXT_BYTES and disk["identity"][2] == disk["bytes"] and
             type(buffer) is dict and set(buffer) == {"bytes", "sha256"} and
             type(buffer["bytes"]) is int and 0 <= buffer["bytes"] <= MAX_TEXT_BYTES and
             type(buffer["sha256"]) is str and R.SHA.fullmatch(buffer["sha256"]) and
             buffer == {key: disk[key] for key in ("bytes", "sha256")},
             "text-buffer witness byte/hash or full leaf identity differs")
        if phases[phase] <= 1:
            need(disk == expected, "pre-close text-buffer witness differs from exact post-task leaf")
        else:
            need({key: disk[key] for key in ("file", "bytes", "sha256")} == final and
                 buffer == {key: workspace["baseline"][key] for key in ("bytes", "sha256")},
                 "post-reopen text-buffer witness differs from restored baseline bytes")
    need(all(counts.get(phase) == 1 for phase in
             (initial_phase, "before_close", "after_reopen", "final_saved_state")),
         "missing or duplicate required text-buffer witness")
    text = after["fields"][contract["field"]]["value"]
    need(type(text) is str and text == _plain_text(_verified(workspace["baseline"])),
         "final declared text buffer differs from verified baseline text")


@dataclass(frozen=True)
class TrustedHooks:
    """Explicit authorized Python adapters; never loaded or named from JSON."""
    observe: object
    close: object
    reopen: object
    set_field: object
    producers: dict

    def __post_init__(self):
        need(all(callable(getattr(self, key)) for key in ("observe", "close", "reopen", "set_field")),
             "four explicit recovery callbacks required")
        need(type(self.producers) is dict and set(self.producers) == {"observe", "close", "reopen", "set_field"},
             "exact recovery producer pins required")
        for expected in self.producers.values(): _verified(expected)


def _backend(value):
    value = PublicAXDocument() if value is None else value
    need(isinstance(value, (PublicAXDocument, TrustedHooks)), "fixed public AX adapter or explicit typed trusted hooks required")
    return value


def _state(value, context, *, opened=True):
    need(type(value) is dict and set(value) == {"identity", "document", "open", "edited", "window", "fields"} and
         value["identity"] == context["identity"] and value["document"] == context["document"] and
         type(value["open"]) is bool and value["open"] is opened, "actual owned document observation required")
    if opened:
        need(type(value["edited"]) is bool and type(value["window"]) is dict and
             set(value["window"]) == {"position", "size"}, "known document edited state and geometry required")
        S._vector(value["window"]["position"]); S._vector(value["window"]["size"], size=True)
        need(type(value["fields"]) is dict and set(value["fields"]) == {row["id"] for row in context["fields"]},
             "every declared public state field must be observed")
        for row in value["fields"].values():
            need(type(row) is dict and set(row) == {"value", "settable"} and type(row["settable"]) is bool and
                 type(row["value"]) in {str, bool, int, float} and len(_json(row["value"])) <= 256 * 1024,
                 "bounded typed actual public field value required")
    else:
        need(value["edited"] is None and value["window"] is None and value["fields"] == {},
             "closed document observation cannot invent state")
    need(len(_json(value)) <= MAX_STATE_BYTES, "aggregate document observation exceeds its byte bound")
    return copy.deepcopy(value)


def capture(workspace, identity, target, fields, out, *, backend=None, timeout=30, metadata_policy=None, text_buffer=None):
    """Checkpoint a saved working copy and declared public state before work."""
    R.assert_guest()
    S.validate_identity(identity); S.validate_target(target)
    need(identity["uid"] == os.getuid() == os.geteuid() and identity["bundle_id"] == target["bundle_id"],
         "same-user process and exact target required")
    need(type(timeout) is int and 1 <= timeout <= 60, "bounded checkpoint timeout required")
    value = _workspace(workspace); _verified(value["document"])
    need(not Path(value["root"]["path"]).is_relative_to(Path(identity["bundle"])), "task workspace must be outside application")
    backend = _backend(backend)
    need(metadata_policy is None or type(metadata_policy) is str and metadata_policy == DARWIN_LASTUSED and
         type(backend) is PublicAXDocument,
         "declared Darwin metadata policy requires the fixed public AX adapter")
    need(metadata_policy is None or text_buffer is None,
         "combined text-buffer and preliminary-reopen metadata policy is unsupported")
    folder = Path(out)
    need(folder.is_absolute() and folder.parent.resolve(strict=True) == folder.parent and not folder.exists(),
         "fresh checkpoint folder required")
    storage = _storage_plan((folder.parent, MAX_RECEIPT_BYTES))
    storage_samples = [_storage_admit(storage)]
    context = {"identity": copy.deepcopy(identity), "target": copy.deepcopy(target),
               "document": value["document"]["file"], "fields": validate_fields(fields),
               "deadline": time.monotonic() + timeout}
    if text_buffer is not None:
        context["text_buffer"] = validate_text_buffer(text_buffer, context["fields"])
    _budget(context)
    need(R.fingerprint(identity["bundle"]) == target, "application target changed before document checkpoint")
    _budget(context)
    observed = _state(backend.observe(copy.deepcopy(context)), context)
    _budget(context)
    need(observed["edited"] is False, "unsaved document buffers are unsupported; checkpoint refused")
    _text_buffer_guard(observed, context)
    need(all(not row["restore"] or observed["fields"][row["id"]]["settable"] for row in fields),
         "declared restoration field is not publicly settable")
    need(pin(value["document"]["file"]) == value["document"], "document changed during checkpoint")
    storage_samples.append(_storage_admit(storage))
    folder.mkdir(mode=0o700)
    result = {"format": CHECKPOINT, "workspace": workspace, "context": {key: val for key, val in context.items() if key != "deadline"},
              "baseline": observed, "producers": copy.deepcopy(backend.producers), "unsupported": list(UNSUPPORTED),
              "scope": _document_scope(context), "storage_admissions": storage_samples}
    if metadata_policy is not None:
        result["metadata_policy"] = metadata_policy
    return _save(folder / "checkpoint.json", result)


def inspect_checkpoint(checkpoint):
    value = json.loads(_verified(checkpoint))
    expected = {"format", "workspace", "context", "baseline", "producers", "unsupported", "scope"}
    need(type(value) is dict and expected <= set(value) <= expected | {"storage_admissions", "metadata_policy"} and
         value["format"] == CHECKPOINT and value["unsupported"] == UNSUPPORTED, "closed document checkpoint required")
    workspace = _workspace(value["workspace"])
    context = value["context"]
    required_context = {"identity", "target", "document", "fields"}
    need(type(context) is dict and required_context <= set(context) <= required_context | {"text_buffer"},
         "closed document context required")
    identity, target = S.validate_identity(context["identity"]), S.validate_target(context["target"])
    need(identity["uid"] == os.getuid() == os.geteuid() and identity["bundle_id"] == target["bundle_id"] and
         context["document"] == workspace["document"]["file"], "checkpoint process/document binding changed")
    fields = validate_fields(context["fields"])
    if "text_buffer" in context:
        need(context["text_buffer"] is not None, "explicit text-buffer declaration cannot be null")
        validate_text_buffer(context["text_buffer"], fields)
    baseline = _state(value["baseline"], context)
    need(baseline["edited"] is False and all(not row["restore"] or baseline["fields"][row["id"]]["settable"] for row in fields),
         "checkpoint must have known saved document and restorable declared fields")
    if context.get("text_buffer") is not None:
        _text_buffer_guard(baseline, context, baseline=_verified(workspace["baseline"]))
    need(type(value["producers"]) is dict and set(value["producers"]) == {"observe", "close", "reopen", "set_field"},
         "closed checkpoint adapter pins required")
    for expected in value["producers"].values(): _verified(expected)
    if "metadata_policy" in value:
        need(value["metadata_policy"] == DARWIN_LASTUSED and value["producers"] == PublicAXDocument().producers,
             "metadata policy must bind the fixed public AX producer")
        need(context.get("text_buffer") is None,
             "combined text-buffer and preliminary-reopen metadata policy is unsupported")
    need(value["scope"] == _document_scope(context),
         "explicit supported/excluded restoration scope changed")
    return value


def _successor(context, identity, restarts):
    current = copy.deepcopy(context["identity"])
    need(type(restarts) is list and len(restarts) <= 8, "bounded witnessed restart chain required")
    for entry in restarts:
        need(type(entry) is dict and set(entry) == {"intent", "receipt"}, "closed recovery restart witness required")
        intent = json.loads(_verified(entry["intent"]))
        witness = RR.validate_restart(intent, entry["receipt"])
        need(witness["old_identity"] == current and witness["target"] == context["target"],
             "recovery restart chain does not continue checkpoint epoch")
        current = S.validate_identity(witness["new_identity"])
    need(identity is None and not restarts or identity == current, "recovery process epoch changed; no unwitnessed adoption")
    context["identity"] = current


def verify_restored(checkpoint, receipt):
    """Validate historical required-scope recovery for publication/catalog use.

    This checks retained recovery evidence, not current application freshness.
    Selection/execution still perform their existing independent current checks.
    """
    value = inspect_checkpoint(checkpoint)
    result = json.loads(_verified(receipt))
    need(result.get("format") == OUTCOME and result.get("status") == "restored" and result.get("checkpoint") == checkpoint and
         result.get("files_restored") is True and result.get("app_state_restored") is True and result.get("error") is None and
         result.get("unsupported") == UNSUPPORTED and result.get("scope") == value["scope"],
         "required document restoration is incomplete; publication/reuse refused")
    if "metadata_policy" in value:
        need(result.get("metadata_policy") == value["metadata_policy"], "recovery metadata policy changed")
        transition = result.get("metadata_transition")
        if transition is not None:
            need(transition == _metadata_transition(transition["before"], transition["after"]) and
                 transition["before"]["document"] == result.get("expected_current"),
                 "recovery metadata transition evidence changed")
    context = copy.deepcopy(value["context"])
    _successor(context, result.get("identity"), result.get("restarts"))
    after = _state(result.get("observation_after"), context)
    need(after["edited"] is False and after["window"] == value["baseline"]["window"] and
         all(S._same(after["fields"][row["id"]]["value"], value["baseline"]["fields"][row["id"]]["value"])
             for row in context["fields"]), "recovery readback differs from checkpoint")
    workspace = _workspace(value["workspace"])
    final = result.get("document_after", {})
    need(final.get("file") == context["document"] and
         {key: final.get(key) for key in ("bytes", "sha256")} ==
         {key: workspace["baseline"][key] for key in ("bytes", "sha256")}, "recovery file readback differs from baseline")
    if context.get("text_buffer") is not None:
        _verify_text_buffer_evidence(result, context, workspace, after)
    return result


def current_document(checkpoint):
    """Freeze both content and live leaf identity immediately after task work."""
    value = inspect_checkpoint(checkpoint)
    _, actual, identity = _read(value["context"]["document"])
    return {**actual, "identity": list(identity)}


def _post_task_schema(expected, context):
    need(type(expected) is dict and set(expected) == {"file", "bytes", "sha256", "identity"} and
         expected["file"] == context["document"] and type(expected["bytes"]) is int and 0 <= expected["bytes"] <= MAX_BYTES and
         type(expected["sha256"]) is str and R.SHA.fullmatch(expected["sha256"]) and type(expected["identity"]) is list and
         len(expected["identity"]) == 5 and all(type(part) is int and part >= 0 for part in expected["identity"]),
         "complete typed post-task owned document observation required")


def _observed_document(expected, context):
    _post_task_schema(expected, context)
    raw, actual, identity = _read(context["document"])
    need(expected == {**actual, "identity": list(identity)}, "document modified externally after post-task observation")
    return raw, actual, identity


def _darwin_metadata_api():
    """Public Darwin FD APIs, with signatures from the macOS SDK headers.

    No security-bypass option, pathname metadata command, or child process is
    used. Unsupported platforms/symbols fail before an app action.
    """
    need(sys.platform == "darwin" and ctypes.sizeof(ctypes.c_void_p) == 8 and
         ctypes.sizeof(ctypes.c_size_t) == ctypes.sizeof(ctypes.c_ssize_t) == 8,
         "Darwin 64-bit public metadata ABI required")
    api = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    signatures = {
        "flistxattr": ([ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int], ctypes.c_ssize_t),
        "fgetxattr": ([ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                       ctypes.c_uint32, ctypes.c_int], ctypes.c_ssize_t),
        "acl_get_fd_np": ([ctypes.c_int, ctypes.c_int], ctypes.c_void_p),
        "acl_size": ([ctypes.c_void_p], ctypes.c_ssize_t),
        "acl_copy_ext": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t], ctypes.c_ssize_t),
        "acl_free": ([ctypes.c_void_p], ctypes.c_int),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def _metadata_values(fd, context):
    api = _darwin_metadata_api()
    # sys/xattr.h: SHOWCOMPRESSION includes compression attributes that the
    # ordinary list hides. This does not bypass filesystem authorization.
    options = 0x0020
    def call(name, *args):
        _budget(context)
        ctypes.set_errno(0)
        value = getattr(api, name)(*args)
        error_code = ctypes.get_errno()
        # The ACL allocation is owned immediately on return; its caller must
        # enter finally before a deadline exception can escape.
        if name != "acl_get_fd_np": _budget(context)
        # Darwin acl_get_fd_np uses FILESEC_ACL; a missing ACL property
        # returns NULL/ENOENT. The caller validates this held descriptor and
        # its unchanged canonical path before and after the complete sample.
        # Other NULL results remain read failures, including errno == 0.
        if name == "acl_get_fd_np" and value in (None, 0) and error_code == errno.ENOENT:
            return None
        valid = value not in (None, 0) if name == "acl_get_fd_np" else value != -1
        need(valid, "public FD metadata read failed: " + name + " errno=" + str(error_code))
        return value
    def names():
        count = call("flistxattr", fd, None, 0, options)
        need(0 <= count <= MAX_XATTRS * 128, "bounded complete xattr name list required")
        buffer = ctypes.create_string_buffer(max(1, count))
        actual = call("flistxattr", fd, buffer, count, options)
        need(actual == count, "xattr list changed during read")
        raw = buffer.raw[:count]
        need(not raw or raw.endswith(b"\0"), "complete NUL-delimited xattr names required")
        parts = raw[:-1].split(b"\0") if raw else []
        need(len(parts) <= MAX_XATTRS and len(set(parts)) == len(parts) and
             all(0 < len(name) <= 127 for name in parts), "bounded unique xattr names required")
        return sorted(parts)
    listed = names()
    values, total = {}, 0
    for name in listed:
        decoded = name.decode("utf-8", "strict")
        count = call("fgetxattr", fd, name, None, 0, 0, options)
        need(0 <= count <= 64 * 1024, "bounded xattr value required")
        total += count
        need(total <= MAX_METADATA_BYTES, "aggregate xattr bound exceeded")
        buffer = ctypes.create_string_buffer(max(1, count))
        need(call("fgetxattr", fd, name, buffer, count, 0, options) == count,
             "xattr value changed during read")
        values[decoded] = {"bytes": count, "sha256": hashlib.sha256(buffer.raw[:count]).hexdigest()}
    need(names() == listed, "xattr names changed during complete metadata read")
    # ACLs are separate filesystem security metadata, not inferred from xattrs
    # or Unix permission bits. acl_copy_ext is the public canonical big-endian
    # external representation (sys/acl.h, acl_copy_ext(3)).
    acl = call("acl_get_fd_np", fd, 0x00000100)  # ACL_TYPE_EXTENDED
    if acl is None:
        _budget(context)
        return values, {"status": "absent"}
    primary = None
    try:
        _budget(context)
        count = call("acl_size", acl)
        need(0 < count <= 64 * 1024, "bounded complete extended ACL required")
        buffer = ctypes.create_string_buffer(count)
        actual = call("acl_copy_ext", buffer, acl, count)
        need(0 < actual <= count, "complete external ACL representation required")
        acl_pin = {"bytes": actual, "sha256": hashlib.sha256(buffer.raw[:actual]).hexdigest()}
    except BaseException as error:
        primary = error
        raise
    finally:
        try: need(api.acl_free(acl) == 0, "cannot release public ACL observation")
        except BaseException as cleanup_error:
            if primary is not None: raise primary from cleanup_error
            raise
    return values, acl_pin


def _metadata_snapshot(fd, context):
    """One held leaf, bounded bytes + complete public metadata, no app calls."""
    def state(info):
        fields = ("st_mode", "st_uid", "st_gid", "st_nlink", "st_flags", "st_gen", "st_rdev",
                  "st_birthtime", "st_blocks", "st_blksize")
        need(all(hasattr(info, name) for name in fields), "complete Darwin security/stat observation required")
        security = {name: getattr(info, name) for name in fields}
        need(all(type(val) is int and val >= 0 for name, val in security.items() if name != "st_birthtime") and
             type(security["st_birthtime"]) in (int, float) and math.isfinite(security["st_birthtime"]) and
             security["st_birthtime"] >= 0,
             "known typed Darwin security/stat observation required")
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid(),
             "held task document ownership changed")
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns], security
    _budget(context)
    path = Path(context["document"])
    first, security = state(os.fstat(fd))
    need(path.resolve(strict=True) == path and not path.is_symlink() and state(path.lstat()) == (first, security),
         "held metadata leaf no longer matches task path")
    need(0 <= first[2] <= MAX_BYTES, "bounded held document required")
    os.lseek(fd, 0, os.SEEK_SET)
    digest, count = hashlib.sha256(), 0
    while True:
        _budget(context)
        data = os.read(fd, min(1024 * 1024, MAX_BYTES + 1 - count))
        if not data: break
        count += len(data); digest.update(data)
        need(count <= MAX_BYTES, "held document byte bound exceeded")
    xattrs, acl = _metadata_values(fd, context)
    need(count == first[2] and state(os.fstat(fd)) == (first, security) and
         path.resolve(strict=True) == path and not path.is_symlink() and state(path.lstat()) == (first, security),
         "document/stat/security changed during metadata observation")
    _budget(context)
    return {"document": {"file": str(path), "bytes": count, "sha256": digest.hexdigest(), "identity": first},
            "security": security, "xattrs": xattrs, "acl": acl}


def _metadata_transition(before, after):
    """Admit only the declared observation across our preliminary lifecycle."""
    first, last = before["document"], after["document"]
    need({key: first[key] for key in ("file", "bytes", "sha256")} ==
         {key: last[key] for key in ("file", "bytes", "sha256")} and
         first["identity"][:4] == last["identity"][:4] and before["security"] == after["security"] and
         before["acl"] == after["acl"], "document content/identity/security changed across public reopen/close")
    old, new = before["xattrs"], after["xattrs"]
    need({key: val for key, val in old.items() if key != LASTUSED_ATTRIBUTE} ==
         {key: val for key, val in new.items() if key != LASTUSED_ATTRIBUTE},
         "undeclared xattr changed across public reopen/close")
    if before == after:
        return {"policy": DARWIN_LASTUSED, "boundary": "verified_preliminary_reopen_saved_close",
                "before": before, "after": after, "admitted_delta": "none"}
    need(new.get(LASTUSED_ATTRIBUTE, {}).get("bytes") == 16 and
         (LASTUSED_ATTRIBUTE not in old or old[LASTUSED_ATTRIBUTE]["bytes"] == 16) and
         old.get(LASTUSED_ATTRIBUTE) != new[LASTUSED_ATTRIBUTE] and
         last["identity"][4] >= first["identity"][4],
         "only a witnessed 16-byte lastuseddate addition/update is admitted")
    return {"policy": DARWIN_LASTUSED, "boundary": "verified_preliminary_reopen_saved_close",
            "before": before, "after": after, "admitted_delta": LASTUSED_ATTRIBUTE}


def restore(checkpoint, expected_current, out, *, backend=None, identity=None, restarts=None, timeout=30):
    """Attempt recovery once; callers retain this receipt even after task failure.

    ``expected_current`` freezes the caller's post-task file observation. An
    intervening external edit is refused before close and again before write.
    An unwitnessed process replacement is never adopted here.
    """
    R.assert_guest()
    backend = _backend(backend); value = inspect_checkpoint(checkpoint)
    policy = value.get("metadata_policy")
    need(policy is None or type(backend) is PublicAXDocument,
         "metadata policy cannot authorize trusted hooks or custom public subclasses")
    context = copy.deepcopy(value["context"])
    restarts = [] if restarts is None else copy.deepcopy(restarts)
    _successor(context, identity, restarts)
    need(type(timeout) is int and 1 <= timeout <= 60, "bounded recovery timeout required")
    context["deadline"] = time.monotonic() + timeout
    if context.get("text_buffer") is not None:
        context["_text_buffer_expected"] = copy.deepcopy(expected_current)
    folder = Path(out)
    need(folder.is_absolute() and folder.parent.resolve(strict=True) == folder.parent and not folder.exists(),
         "fresh recovery attempt folder required; no automatic retry")
    workspace = _workspace(value["workspace"])
    _post_task_schema(expected_current, context)  # Size only; exact drift refusal is retained below.
    storage = _storage_plan((folder.parent, expected_current["bytes"] + 2 * MAX_RECEIPT_BYTES),
                            (Path(context["document"]).parent, workspace["baseline"]["bytes"]))
    initial_storage = _storage_admit(storage)  # No mkdir or callbacks on refusal.
    folder.mkdir(mode=0o700)
    result = {"format": OUTCOME, "status": "unknown", "checkpoint": checkpoint, "expected_current": expected_current,
              "started_monotonic": time.monotonic(), "files_restored": False, "app_state_restored": False,
              "actions": [], "error": None, "unsupported": list(UNSUPPORTED), "scope": value["scope"],
              "identity": context["identity"], "restarts": restarts, "automatic_retry": False,
              "storage_admissions": [initial_storage]}
    if policy is not None:
        result.update(metadata_policy=policy, metadata_transition=None)
    _save(folder / "attempt.json", result)
    storage[0]["remaining_bytes"] -= MAX_RECEIPT_BYTES
    def admit():
        result["storage_admissions"].append(_storage_admit(storage))
    failure = None
    metadata_fd = None
    metadata_after = None
    preliminary = False
    accepted_current = copy.deepcopy(expected_current)
    def observe(opened=True, *, phase="state_adjustment"):
        _budget(context)
        def retain_guard(guard):
            guards = result.setdefault("text_buffer_observations", [])
            need(len(guards) < 128, "bounded text-buffer observation log exhausted")
            record = copy.deepcopy(guard)
            record.update(sequence=len(guards), phase=phase, action_index=len(result["actions"]),
                          observed_monotonic=time.monotonic())
            guards.append(record)
            return record
        try:
            raw = backend.observe(copy.deepcopy(context))
        except BaseException:
            guard = getattr(backend, "_last_text_buffer_guard", None)
            if context.get("text_buffer") is not None and guard:
                retain_guard(guard)
            raise
        need(type(raw) is dict and type(raw.get("open")) is bool, "known actual document open state required")
        observed = _state(raw, context, opened=raw["open"] if opened is None else opened)
        result["last_observation"] = observed
        if context.get("text_buffer") is not None and observed["open"]:
            record = retain_guard({})
            _text_buffer_guard(observed, context, expected=context["_text_buffer_expected"], evidence=record)
        _budget(context)
        return observed
    def act(name, *args):
        _budget(context)
        admit()
        getattr(backend, name)(copy.deepcopy(context), *copy.deepcopy(args))
        _budget(context)
    try:
        _budget(context)
        need(backend.producers == value["producers"], "recovery adapter/producer changed")
        for expected in backend.producers.values(): _verified(expected)
        workspace = _workspace(value["workspace"])
        need(R.fingerprint(context["identity"]["bundle"]) == context["target"], "application target changed before document recovery")
        _budget(context)
        _observed_document(expected_current, context)
        if policy is not None:
            metadata_fd = os.open(context["document"], os.O_RDONLY | os.O_NOFOLLOW)
            metadata_before = _metadata_snapshot(metadata_fd, context)
            need(metadata_before["document"] == expected_current,
                 "post-task document changed before public metadata observation")
        before = observe(opened=None, phase="initial")
        if not before["open"]:
            if policy is not None:
                preliminary = True
                result["metadata_transition"] = {"policy": policy,
                    "boundary": "verified_preliminary_reopen_saved_close", "before": metadata_before,
                    "after": None, "admitted_delta": None}
                need(_metadata_snapshot(metadata_fd, context) == metadata_before,
                     "metadata changed before preliminary reopen action")
            # A witnessed ordinary restart or task-close may have closed it.
            # Reopen before any replacement: absence alone never proves that
            # replacing bytes is safe for the application's document state.
            result["actions"].append("preliminary_reopen_requested")
            act("reopen")
            before = observe(phase="preliminary_reopen")
            result["actions"].append("preliminary_reopen_verified")
        need(before["edited"] is False, "unsaved document buffers unsupported; no close or file overwrite")
        if context.get("text_buffer") is not None:
            # Re-observe the visible field and the exact post-task leaf again
            # immediately before asking any backend to close the document.
            need(observe(phase="before_close")["edited"] is False,
                 "unsaved document buffers unsupported; no close or file overwrite")
        result["actions"].append("close_requested")
        act("close")
        observe(opened=False)
        result["actions"].append("closed_verified")
        if preliminary:
            metadata_after = _metadata_snapshot(metadata_fd, context)
            result["metadata_transition"]["after"] = metadata_after
            transition = _metadata_transition(metadata_before, metadata_after)
            result["metadata_transition"] = transition
            accepted_current = copy.deepcopy(metadata_after["document"])
        _workspace(value["workspace"])
        raw, current, identity_before = _observed_document(accepted_current, context)
        baseline = _verified(workspace["baseline"])
        # Retain the post-task bytes before replacing the working copy.
        admit()
        with (folder / "before-document").open("xb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        storage[0]["remaining_bytes"] -= len(raw)
        file = Path(context["document"])
        parent_fd = os.open(file.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        pending = ".restore-" + hashlib.sha256(_json(checkpoint)).hexdigest()[:24]
        created_pending = False
        try:
            info = os.fstat(parent_fd)
            need({"path": str(file.parent), "device": info.st_dev, "inode": info.st_ino} == workspace["working"],
                 "working directory changed before replacement")
            live_fd = os.open(file.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            try:
                fcntl.flock(live_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                info = os.fstat(live_fd)
                need((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) == identity_before and
                     info.st_nlink == 1 and info.st_uid == os.getuid(), "document identity changed before replacement")
                if metadata_after is not None:
                    need(_metadata_snapshot(live_fd, context) == metadata_after,
                         "accepted metadata changed before locked replacement")
                admit()
                new_fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
                created_pending = True
                with os.fdopen(new_fd, "wb") as stream:
                    stream.write(baseline); stream.flush(); os.fsync(stream.fileno())
                storage[1]["remaining_bytes"] -= len(baseline)
                _budget(context)
                admit()
                need(_directory(file.parent) == workspace["working"], "working directory changed before atomic replacement")
                _observed_document(accepted_current, context)
                if metadata_after is not None:
                    need(_metadata_snapshot(live_fd, context) == metadata_after,
                         "accepted metadata changed immediately before replacement")
                os.replace(pending, file.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(live_fd)
        finally:
            # Delete only our exclusive pending leaf, if replacement did not consume it.
            if created_pending:
                try: os.unlink(pending, dir_fd=parent_fd)
                except FileNotFoundError: pass
            os.close(parent_fd)
        need(_directory(file.parent) == workspace["working"], "working directory changed after replacement")
        restored_pin = pin(file)
        need({k: restored_pin[k] for k in ("bytes", "sha256")} ==
             {k: workspace["baseline"][k] for k in ("bytes", "sha256")}, "recovered file bytes differ")
        result.update(files_restored=True, document_after=restored_pin)
        if context.get("text_buffer") is not None:
            # The destructive boundary is over. Normal reopen can update file
            # metadata; each later comparison still freezes its own exact leaf
            # and the final existing check requires the restored byte pin.
            context["_text_buffer_expected"] = None
        result["actions"].append("bytes_restored_verified")
        act("reopen")
        after = observe(phase="after_reopen")
        for key in ("position", "size"):
            expected = value["baseline"]["window"][key]
            if not S._same(after["window"][key], expected):
                act("set_field", {"id": "window." + key}, expected)
                after = observe()
                need(S._same(after["window"][key], expected), "public window state restoration not observed")
        for field in context["fields"]:
            expected = value["baseline"]["fields"][field["id"]]["value"]
            actual = after["fields"][field["id"]]
            if not S._same(actual["value"], expected) and field["restore"]:
                need(actual["settable"] and type(actual["value"]) is type(expected), "public state type/settable identity changed")
                act("set_field", field, expected)
                after = observe()
            need(S._same(after["fields"][field["id"]]["value"], expected), "declared observable app state was not restored")
        # Public controls can be coupled: resizing can move a window and one
        # field setter can change another. Verify the whole declared state in
        # one final observation, after every write has finished.
        after = observe(phase="final_saved_state")
        need(after["edited"] is False and S._same(after["window"], value["baseline"]["window"]) and
             all(S._same(after["fields"][field["id"]]["value"], value["baseline"]["fields"][field["id"]]["value"])
                 for field in context["fields"]), "final complete declared state readback differs from checkpoint")
        need(pin(file) == restored_pin, "restored document changed during app readback")
        _workspace(value["workspace"])
        need(backend.producers == value["producers"], "recovery adapter changed during restoration")
        for expected in backend.producers.values(): _verified(expected)
        need(R.fingerprint(context["identity"]["bundle"]) == context["target"], "application target changed during document recovery")
        _budget(context)
        admit()
        result.update(status="restored", app_state_restored=True, observation_after=after)
    except BaseException as error:
        failure = error
        result.update(status="partial" if result["files_restored"] else "unknown" if result["actions"] else "failed",
                      error={"type": type(error).__name__, "message": str(error)[:2048]})
        if not isinstance(error, Exception):
            result["interrupted"] = True
    finally:
        if metadata_fd is not None:
            try: os.close(metadata_fd)
            except BaseException as close_error:
                result["metadata_fd_close_error"] = {"type": type(close_error).__name__, "message": str(close_error)[:2048]}
                if failure is None:
                    failure = close_error
                    result.update(status="partial" if result["files_restored"] else "unknown",
                                  error=copy.deepcopy(result["metadata_fd_close_error"]))
                    if not isinstance(close_error, Exception): result["interrupted"] = True
        result["finished_monotonic"] = time.monotonic()
        try:
            result["receipt"] = _save(folder / "result.json", result)
        except BaseException as retention_error:
            if failure is not None: raise failure from retention_error
            raise
    if failure is not None and not isinstance(failure, Exception):
        raise failure
    return result


class PublicAXDocument:
    """Normal public close/open and measured AXValue/geometry, in an owned VM."""
    @property
    def producers(self):
        source = pin(Path(__file__).resolve())
        return {key: source for key in ("observe", "close", "reopen", "set_field")}

    def _current(self, context):
        from . import rea_public_campaign as U
        import ApplicationServices as AX
        R.assert_guest()
        identity, app = U.gate(context["identity"], "document-recovery.public", context["deadline"])
        root = AX.AXUIElementCreateApplication(identity["pid"])
        def attr(element, name, *, optional=False):
            _budget(context)
            need(AX.AXUIElementSetMessagingTimeout(element, min(1., context["deadline"] - time.monotonic())) == 0,
                 "cannot bound public document AX call")
            _budget(context)
            code, pid = AX.AXUIElementGetPid(element, None)
            _budget(context)
            need(code == 0 and pid == identity["pid"], "public document element owner changed")
            code, answer = AX.AXUIElementCopyAttributeValue(element, name, None)
            _budget(context)
            if optional and code in {-25205, -25212}:  # Unsupported / no public value.
                return None
            need(code == 0 and answer is not None, "required document AX attribute unavailable: " + name)
            return answer
        windows = attr(root, "AXWindows")
        need(windows is not None and len(windows) <= 128, "bounded actual document windows required")
        matches = []
        self._last_windows = list(windows)
        for window in windows:
            _budget(context)
            need(AX.AXUIElementSetMessagingTimeout(window, min(1., context["deadline"] - time.monotonic())) == 0, "cannot bound document identification")
            code, pid = AX.AXUIElementGetPid(window, None)
            need(code == 0 and pid == identity["pid"], "window belongs to another process")
            role, subrole = attr(window, "AXRole"), attr(window, "AXSubrole")
            need(role == "AXWindow" and isinstance(subrole, str) and subrole not in {"AXDialog", "AXSystemDialog", "AXUnknown", ""},
                 "app dialog, sheet or unknown window prevents document recovery")
            self._not_modal(window, attr, required=True)
            document = attr(window, "AXDocument", optional=True)
            if document is not None and str(document) == Path(context["document"]).as_uri():
                matches.append(window)
        need(len(matches) <= 1, "ambiguous owned task document window")
        return AX, attr, matches[0] if matches else None

    def _edited(self, window, attr):
        """Observe the public dirty flag at either documented element placement."""
        import CoreFoundation as CF
        from Foundation import NSNumber
        def boolean(raw):
            value = S._scalar(raw, lambda v: S._native_scalar(v, CF, NSNumber))
            need(type(value) is bool, "known actual AXEdited boolean required")
            return value
        raw = attr(window, "AXEdited", optional=True)
        edited = None if raw is None else boolean(raw)
        button = attr(window, "AXCloseButton", optional=True)
        if button is not None:
            button_raw = attr(button, "AXEdited", optional=True)
            if button_raw is not None:
                need(str(attr(button, "AXRole")) == "AXButton" and
                     str(attr(button, "AXSubrole")) == "AXCloseButton" and
                     CF.CFEqual(attr(button, "AXWindow"), window),
                     "edited close button is not associated with the exact document window")
                button_edited = boolean(button_raw)
                need(edited is None or edited is button_edited, "public document dirty indicators disagree")
                need(CF.CFEqual(attr(window, "AXCloseButton"), button), "document close button changed during observation")
                edited = button_edited
        need(type(edited) is bool, "supported public document AXEdited boolean unavailable")
        return edited

    def _not_modal(self, element, attr, *, required=False):
        modal = attr(element, "AXModal", optional=not required)
        if modal is not None:
            import CoreFoundation as CF
            from Foundation import NSNumber
            value = S._scalar(modal, lambda v: S._native_scalar(v, CF, NSNumber))
            need(type(value) is bool and value is False, "modal or unknown app state prevents document recovery")

    def _no_sheets(self, window, attr):
        """Inspect the complete published child hierarchy, never an invented AXSheets."""
        import CoreFoundation as CF
        pending, visited, text_areas = [window], [], []
        while pending:
            element = pending.pop()
            need(len(visited) < 512 and not any(CF.CFEqual(element, prior) for prior in visited),
                 "document hierarchy is cyclic, duplicated or exceeds its bound")
            visited.append(element)
            role = attr(element, "AXRole")
            need(isinstance(role, str) and role.startswith("AX") and
                 role not in {"AXSheet", "AXDialog", "AXSystemDialog", "AXUnknown"},
                 "document sheet, dialog or unknown role prevents saved-state recovery")
            if role == "AXTextArea":
                text_areas.append(element)
            # Window subroles distinguish dialogs; ordinary groups/buttons do
            # not need that classification. Do not request irrelevant optional
            # metadata that some apps advertise but cannot actually return.
            # Any requested safety attribute still fails closed on read errors.
            if role == "AXWindow":
                subrole = attr(element, "AXSubrole")
                need(isinstance(subrole, str) and subrole not in {"AXDialog", "AXSystemDialog", "AXUnknown", ""},
                     "document dialog or unknown window subrole prevents recovery")
                self._not_modal(element, attr, required=True)
            else:
                self._not_modal(element, attr)
            children = attr(element, "AXChildren", optional=element is not window)
            if children is not None:
                need(len(children) <= 128 and len(visited) + len(pending) + len(children) <= 512,
                     "document hierarchy exceeds its complete observation bound")
                pending.extend(children)
        return text_areas

    def _element(self, window, field, attr, *, text_buffer=False):
        element = window
        for index in field["path"]:
            children = attr(element, "AXChildren")
            need(len(children) <= 128 and index < len(children), "declared public field path changed")
            element = children[index]
        need(str(attr(element, "AXRole")) == field["role"], "declared public field role changed")
        if field["identifier"] is not None:
            need(str(attr(element, "AXIdentifier")) == field["identifier"], "declared public field identifier changed")
        code, subrole = __import__("ApplicationServices").AXUIElementCopyAttributeValue(element, "AXSubrole", None)
        need((code == 0 and subrole is not None and str(subrole) != "AXSecureTextField") or
             (text_buffer and field["role"] == "AXTextArea" and code == -25205 and subrole is None),
             "known nonsecure public field subrole required")
        return element

    def observe(self, context):
        self._last_text_buffer_guard = {}
        AX, attr, window = self._current(context)
        result = {"identity": copy.deepcopy(context["identity"]), "document": context["document"],
                  "open": window is not None, "edited": None, "window": None, "fields": {}}
        if window is None: return result
        import CoreFoundation as CF
        from Foundation import NSNumber
        edited = self._edited(window, attr)
        text_areas = self._no_sheets(window, attr)
        validate_fields(context["fields"])
        contract = validate_text_buffer(context.get("text_buffer"), context["fields"])
        need(not text_areas or contract is not None,
             "observed AXTextArea requires a declared exact UTF-8 text-buffer contract")
        need(contract is None or len(text_areas) == 1, "one unambiguous declared plain-text area required")
        def vector(name):
            raw = attr(window, name)
            kind = AX.kAXValueTypeCGPoint if name == "AXPosition" else AX.kAXValueTypeCGSize
            ok, value = AX.AXValueGetValue(raw, kind, None)
            need(ok, "public document geometry unavailable")
            return [float(value[0]), float(value[1])]
        result.update(edited=edited, window={"position": vector("AXPosition"), "size": vector("AXSize")})
        for field in context["fields"]:
            is_buffer = contract is not None and field["id"] == contract["field"]
            element = self._element(window, field, attr, text_buffer=is_buffer)
            if is_buffer:
                need(CF.CFEqual(element, text_areas[0]) and CF.CFEqual(attr(element, "AXWindow"), window),
                     "declared text buffer is not the exact owned document area")
            raw = attr(element, "AXValue")
            if is_buffer:
                need(isinstance(raw, str), "known declared plain-text buffer string required")
                raw = str(raw)
                need(len(raw.encode("utf-8", errors="strict")) <= MAX_TEXT_BYTES,
                     "declared text buffer exceeds its byte bound")
            else:
                raw = S._scalar(raw, lambda v: S._native_scalar(v, CF, NSNumber))
            code, settable = AX.AXUIElementIsAttributeSettable(element, "AXValue", None)
            need(code == 0, "public field settable state unavailable")
            result["fields"][field["id"]] = {"value": raw, "settable": bool(settable)}
        if contract is not None:
            _text_buffer_guard(result, context, expected=context.get("_text_buffer_expected"),
                               evidence=self._last_text_buffer_guard)
        _, _, current = self._current(context)
        need(current is not None and CF.CFEqual(window, current) and
             self._edited(window, attr) is edited, "document window or dirty state changed during observation")
        return result

    def close(self, context):
        context = copy.deepcopy(context)
        if context.get("text_buffer") is not None and context.get("_text_buffer_expected") is None:
            _, current, leaf = _read(context["document"], maximum=MAX_TEXT_BYTES)
            context["_text_buffer_expected"] = {**current, "identity": list(leaf)}
        AX, attr, window = self._current(context)
        need(window is not None and self.observe(context)["edited"] is False, "only an exact saved task document may close")
        import CoreFoundation as CF
        _, _, current_window = self._current(context)
        need(current_window is not None and CF.CFEqual(window, current_window), "exact document window changed before close")
        need(str(attr(window, "AXDocument")) == Path(context["document"]).as_uri(), "document identity changed before close")
        button = attr(window, "AXCloseButton")
        _budget(context)
        need(AX.AXUIElementSetMessagingTimeout(button, min(1., context["deadline"] - time.monotonic())) == 0,
             "cannot bound exact document close button")
        code, pid = AX.AXUIElementGetPid(button, None)
        need(code == 0 and pid == context["identity"]["pid"], "document close button owner changed")
        _budget(context)
        if context.get("text_buffer") is not None:
            latest = self.observe(context)
            need(latest["edited"] is False, "only an exact saved task document may close")
            _, _, latest_window = self._current(context)
            need(latest_window is not None and CF.CFEqual(window, latest_window),
                 "exact document window changed before guarded close")
            _text_buffer_guard(latest, context, expected=context["_text_buffer_expected"],
                               evidence=self._last_text_buffer_guard)
            _budget(context)
        need(int(AX.AXUIElementPerformAction(button, "AXPress")) == 0, "ordinary document close refused")
        _budget(context)
        import CoreFoundation as CF
        while True:
            actual = self.observe(context)
            if not actual["open"] and not any(CF.CFEqual(window, candidate) for candidate in self._last_windows):
                break
            need(time.monotonic() < context["deadline"], "exact ordinary document close not observed")
            time.sleep(.02)

    def reopen(self, context):
        from . import rea_public_campaign as U
        result = U._open(context["identity"], [context["document"]], context["deadline"], new_instance=False)
        need(result["application_pid"] == context["identity"]["pid"], "document reopen selected another process")
        while not self.observe(context)["open"]:
            need(time.monotonic() < context["deadline"], "reopened exact document not observed")
            time.sleep(.02)

    def set_field(self, context, field, value):
        AX, attr, window = self._current(context)
        need(window is not None, "owned document disappeared before public state restoration")
        if field["id"] in {"window.position", "window.size"}:
            name = "AXPosition" if field["id"] == "window.position" else "AXSize"
            import Quartz
            raw = AX.AXValueCreate(AX.kAXValueTypeCGPoint, Quartz.CGPointMake(*value)) if name == "AXPosition" else \
                  AX.AXValueCreate(AX.kAXValueTypeCGSize, Quartz.CGSizeMake(*value))
            element = window
        else:
            need(field in context["fields"], "undeclared public state mutation refused")
            element = self._element(window, field, attr); name = "AXValue"; raw = value
            import CoreFoundation as CF
            from Foundation import NSNumber
            current = S._scalar(attr(element, name), lambda v: S._native_scalar(v, CF, NSNumber))
            need(type(current) is type(value), "public state scalar type changed before mutation")
        code, settable = AX.AXUIElementIsAttributeSettable(element, name, None)
        need(code == 0 and settable, "declared public restoration attribute is not settable")
        need(int(AX.AXUIElementSetAttributeValue(element, name, raw)) == 0, "public state restoration refused")
