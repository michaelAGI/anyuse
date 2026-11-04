"""VM-only Dictionary event adapter with a separate asset reference and AX reader.

This is one measured interface, not a runner for arbitrary recovered functions.
The actor uses the recovered stxt/path parameters on AppKit's reopen event. The
reference is a uniquely indexed entry in the selected asset's shipped XML; the
observation is the visible GUI. The all-dictionaries API cannot fix the scope.
Imports neither load native frameworks nor inspect the desktop.
"""
from __future__ import annotations

import copy
import ctypes as C
import math
import os
from pathlib import Path
import plistlib
import subprocess
import time
import unicodedata
import xml.etree.ElementTree as ET

from . import rea_mapping as R

BACKEND = "guest_apple_event_dictionary_ax_v1"
EVENT = {"class": "aevt", "id": "rapp", "query_keyword": "stxt", "dictionary_keyword": "path"}


def validate(candidate: dict) -> dict:
    c = copy.deepcopy(candidate)
    if c.get("format") != R.CANDIDATE or not R.NAME.fullmatch(str(c.get("id", ""))):
        raise ValueError("invalid REA candidate format or id")
    if c.get("backend") != BACKEND or not c.get("label"):
        raise ValueError("unknown Apple Event backend or missing label")
    target = c.get("target", {})
    if (not isinstance(target, dict) or target.get("bundle_id") != "com.apple.Dictionary" or
            not R.SHA.fullmatch(str(target.get("sha256", "")))):
        raise ValueError("this observer requires the analysed Dictionary app fingerprint")
    if c.get("effect") != "scoped_find" or c.get("item_kind") != "" or c.get("mode", "") != "":
        raise ValueError("this interface supports dictionary lookup only")
    params = c.get("params", [])
    if (not isinstance(params, list) or len(params) != 1 or not isinstance(params[0], dict) or
            not R.NAME.fullmatch(str(params[0].get("name", ""))) or params[0].get("type") != "string" or
            params[0].get("source") != "lookup"):
        raise ValueError("one explicit string lookup parameter is required")
    if c.get("apple_event") != EVENT or "argv" in c:
        raise ValueError("only the recovered typed reopen event is supported")
    observer = c.get("observer", {})
    if not isinstance(observer, dict) or observer.get("kind") != "dictionary_ax":
        raise ValueError("independent dictionary definition observer required")
    path = observer.get("dictionary_path", "")
    if (not isinstance(path, str) or "\0" in path or not Path(path).is_absolute() or
            Path(path).suffix != ".dictionary" or ".." in Path(path).parts):
        raise ValueError("observer requires an explicit dictionary asset path")
    if (not observer.get("dictionary_label") or
            not R.SHA.fullmatch(str(observer.get("dictionary_sha256", "")))):
        raise ValueError("observer requires the dictionary label and resource fingerprint")
    xml = observer.get("reference_xml", "")
    if (not isinstance(xml, str) or "\0" in xml or Path(xml).is_absolute() or ".." in Path(xml).parts or
            not xml.startswith("Contents/Resources/") or not xml.endswith(".xml")):
        raise ValueError("reference XML must be a relative path inside the selected dictionary")
    evidence = c.get("evidence", [])
    if not isinstance(evidence, list) or not evidence or any(
            not isinstance(e, dict) or not e.get("file") or not e.get("pointer") or
            not R.SHA.fullmatch(str(e.get("sha256", ""))) for e in evidence):
        raise ValueError("candidate needs REA evidence hashes and specific source pointers")
    fields = ("format", "backend", "id", "label", "target", "effect", "item_kind", "mode",
              "params", "apple_event", "observer", "evidence")
    return {k: c[k] for k in fields if k in c}


def bind(c: dict, inputs: dict) -> dict:
    name = c["params"][0]["name"]
    if not isinstance(inputs, dict) or set(inputs) != {name}:
        raise ValueError("inputs must match the candidate's lookup parameter")
    query = inputs[name]
    if (not isinstance(query, str) or not query.strip() or "\0" in query or
            len(query.encode("utf-8")) > 4096):
        raise ValueError("lookup must be nonempty UTF-8 text without NUL, at most 4096 bytes")
    return {"class": "aevt", "id": "rapp", "parameters": {
        "stxt": {"type": "utf8", "value": query},
        "path": {"type": "utf8", "value": c["observer"]["dictionary_path"]}}}


def dictionary_identity(path: str | Path) -> dict:
    """Pure file identity; production callers first check the VM."""
    root = Path(path).resolve(strict=True)
    if root.suffix != ".dictionary" or not root.is_dir():
        raise ValueError("expected a dictionary asset directory")
    info = plistlib.loads(R._within(root, "Contents/Info.plist").read_bytes())
    rows, total = [], 0
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            p = Path(base) / name
            relative = p.relative_to(root).as_posix()
            if p.is_symlink():
                R._within(root, relative)
                rows.append([relative, "symlink", os.readlink(p)])
            elif p.is_file():
                total += p.stat().st_size
                if total > R.MAX_BUNDLE_BYTES:
                    raise ValueError("dictionary exceeds the resource budget")
                rows.append([relative, p.stat().st_mode & 0o777, R._file_hash(p)])
            elif not p.is_dir():
                raise ValueError("special file in dictionary asset")
    return {"sha256": R.digest(sorted(rows)), "label": info.get("CFBundleName", ""),
            "bundle_id": info.get("CFBundleIdentifier", ""), "bytes": total}


def _environment(c: dict) -> dict:
    actual = dictionary_identity(c["observer"]["dictionary_path"])
    if (actual["sha256"] != c["observer"]["dictionary_sha256"] or
            actual["label"] != c["observer"]["dictionary_label"]):
        raise ValueError("dictionary resource changed; reanalyse and reverify")
    return actual


def normalize(text: str) -> str:
    # AX splits styled runs and links, including spaces around punctuation.
    return "".join(unicodedata.normalize("NFKC", text).split())


def reference(c: dict, query: str) -> str:
    """Read an exact, unique dictionary index entry before actuation.

    Reference text comes from the selected immutable asset, never from the actor
    or a previous GUI result. DCSCopyTextDefinition(NULL, ...) is unsuitable here:
    it can return another active dictionary's definition for the same word.
    """
    R.assert_guest()
    root = Path(c["observer"]["dictionary_path"]).resolve(strict=True)
    xml = R._within(root, c["observer"]["reference_xml"])
    if xml.stat().st_size > R.MAX_JSON_BYTES:
        raise ValueError("reference XML exceeds the input budget")
    data = xml.read_bytes()
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("reference XML must not declare entities or a DTD")
    ns = "{http://www.apple.com/DTDs/DictionaryService-1.0.rng}"
    html = "{http://www.w3.org/1999/xhtml}"
    document = ET.fromstring(data)
    if document.tag != ns + "dictionary":
        raise ValueError("expected a dictionary XML document")
    def key(text):
        return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()
    matches = []
    for entry in document.findall(ns + "entry"):
        values = [index.get(ns + "value", index.findtext(ns + "index_value", ""))
                  for index in entry.findall(ns + "index")]
        if any(key(value) == key(query) for value in values):
            matches.append(entry)
    # A secondary entry can index the same term as an alias (Finder versus
    # Einfacher Finder). Prefer an exact entry title; ties remain a refusal.
    exact = []
    for entry in matches:
        titles = [index.get(ns + "title", index.findtext(ns + "index_title", ""))
                  for index in entry.findall(ns + "index")]
        titles += ["".join(h.itertext()) for h in entry.findall(html + "h1")]
        if any(key(title) == key(query) for title in titles):
            exact.append(entry)
    if exact:
        matches = exact
    if len(matches) != 1:
        raise ValueError("no unique independent dictionary reference for this query")
    entry = matches[0]
    if not any("".join(p.itertext()).strip() for p in entry.iter(html + "p")):
        raise ValueError("dictionary reference has no definition body")
    # Index values/titles are metadata, not visible definition text.
    text = "\n".join("".join(child.itertext()) for child in entry if not child.tag.startswith(ns))
    if not text.strip() or len(text.encode("utf-8")) > 256 * 1024:
        raise ValueError("reference exceeds the text budget or is empty")
    return text


def observation(rows: list[dict]) -> dict:
    """Flatten definition children in DOM order; headings in menus/rows don't count."""
    children = {}
    for i, row in enumerate(rows):
        children.setdefault(row.get("parent"), []).append(i)
    def body(i):
        row = rows[i]
        text = [str(row.get("AXValue", ""))] if row.get("role") in {"AXStaticText", "AXTextArea"} else []
        for child in children.get(i, []):
            text.extend(body(child))
        return text
    searches = [r.get("AXValue", "") for r in rows
                if r.get("role") == "AXTextField" and r.get("AXSubrole") == "AXSearchField"]
    selected = [r.get("AXTitle", "") for r in rows
                if r.get("role") == "AXRadioButton" and str(r.get("AXValue")) == "1"]
    areas = [i for i, r in enumerate(rows) if r.get("role") == "AXWebArea"]
    return {"query": searches[0] if len(searches) == 1 else None,
            "dictionary": selected[0] if len(selected) == 1 else None,
            "definition": normalize(" ".join(body(areas[0]))) if len(areas) == 1 else None}


class _AEDesc(C.Structure):
    _pack_ = 2  # SDK AEDataModel.h: #pragma pack(push, 2)
    _fields_ = [("descriptorType", C.c_uint32), ("dataHandle", C.c_void_p)]


def _fcc(text: str) -> int:
    return int.from_bytes(text.encode("ascii"), "big")


class Runtime:
    """Existing guest GUI grants required. Never requests or answers consent."""
    def __init__(self, bundle: Path, *, bundle_id: str | None = None, expected_identity=None):
        R.assert_guest()
        expected = None
        if expected_identity is not None:
            from . import rea_runtime_bridge as B
            expected = B._expected_identity(expected_identity)
            if (expected is None or expected["bundle"] != str(bundle) or
                    expected["bundle_id"] != bundle_id or B.process_identity(bundle, bundle_id) != dict(expected)):
                raise ValueError("existing GUI context differs from supplied process epoch")
        import AppKit
        import ApplicationServices as A
        import Quartz
        self.A, self.Quartz = A, Quartz
        self._ready()
        bundle_id = bundle_id or plistlib.loads(R._within(bundle, "Contents/Info.plist").read_bytes())["CFBundleIdentifier"]
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
        if not apps:
            if expected is not None:
                raise ValueError("pinned replay requires its existing app; no launch")
            subprocess.run(["/usr/bin/open", "-g", str(bundle)], check=True, timeout=15)
            for _ in range(100):
                apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
                if apps:
                    break
                time.sleep(.1)
        if len(apps) != 1 or Path(str(apps[0].bundleURL().path())).resolve() != bundle:
            raise ValueError("expected one exact target app process")
        self.app, self.pid = apps[0], int(apps[0].processIdentifier())
        if expected is not None:
            if self.pid != expected["pid"] or B.process_identity(bundle, bundle_id) != dict(expected):
                raise ValueError("selected GUI context differs from supplied process epoch")
        else:
            apps[0].activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
        self.lib = C.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
        signatures = {
            "AECreateDesc": ([C.c_uint32, C.c_void_p, C.c_long, C.POINTER(_AEDesc)], C.c_int16),
            "AEDisposeDesc": ([C.POINTER(_AEDesc)], C.c_int16),
            "AEDeterminePermissionToAutomateTarget": ([C.POINTER(_AEDesc), C.c_uint32, C.c_uint32, C.c_ubyte], C.c_int32),
            "AECreateAppleEvent": ([C.c_uint32, C.c_uint32, C.POINTER(_AEDesc), C.c_int16, C.c_int32, C.POINTER(_AEDesc)], C.c_int16),
            "AEPutParamPtr": ([C.POINTER(_AEDesc), C.c_uint32, C.c_uint32, C.c_void_p, C.c_long], C.c_int16),
            "AESendMessage": ([C.POINTER(_AEDesc), C.POINTER(_AEDesc), C.c_uint32, C.c_int32], C.c_int32),
            "AEGetParamPtr": ([C.POINTER(_AEDesc), C.c_uint32, C.c_uint32, C.POINTER(C.c_uint32), C.c_void_p, C.c_long, C.POINTER(C.c_long)], C.c_int16)}
        for name, (args, result) in signatures.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = args, result

    def _ready(self):
        if (self.Quartz.CGSessionCopyCurrentDictionary() or {}).get("CGSSessionScreenIsLocked"):
            raise PermissionError("guest screen is locked")
        if not self.A.AXIsProcessTrusted():
            raise PermissionError("guest GUI runner needs its existing Accessibility grant")

    def read(self, *, all_windows: bool = False) -> list[dict]:
        self._ready()
        if self.app.isTerminated():
            raise ValueError("target app process exited")
        def attr(element, name):
            code, value = self.A.AXUIElementCopyAttributeValue(element, name, None)
            return value if code == 0 else None
        app = self.A.AXUIElementCreateApplication(self.pid)
        windows = attr(app, "AXWindows") or []
        if not all_windows and len(windows) != 1:
            raise ValueError("Dictionary observer requires one unambiguous window")
        rows, todo = [], [(window, None, 0) for window in windows]
        while todo:
            if len(rows) >= 2500:
                raise ValueError("dictionary AX tree exceeds the observation budget")
            element, parent, depth = todo.pop(0)
            role = str(attr(element, "AXRole") or "")
            row = {"parent": parent, "role": role}
            for key in ("AXSubrole", "AXTitle", "AXValue", "AXDocument"):
                value = attr(element, key)
                if value is not None and role != "AXSecureTextField":
                    if len(str(value)) > 256 * 1024:
                        raise ValueError("dictionary AX value exceeds the text budget")
                    row[key] = str(value)
            rows.append(row)
            children = attr(element, "AXChildren") or []
            if children and depth >= 20:
                raise ValueError("dictionary AX tree exceeds the depth budget")
            todo.extend((child, len(rows) - 1, depth + 1) for child in children)
        return rows

    def send(self, c: dict, inputs: dict, timeout: float) -> dict:
        return self.send_invocation(bind(c, inputs), timeout)

    def send_invocation(self, invocation: dict, timeout: float) -> dict:
        """Shared typed event transport; no app-specific event or parameter table."""
        if not 1 <= timeout <= 300:
            raise ValueError("timeout must be 1–300 seconds")
        for code in (invocation.get("class"), invocation.get("id"), *invocation.get("parameters", {})):
            if not isinstance(code, str) or len(code) != 4 or not code.isascii() or not code.isprintable():
                raise ValueError("Apple Event codes must be four printable ASCII characters")
        for value in invocation["parameters"].values():
            if (not isinstance(value, dict) or set(value) != {"type", "value"} or value["type"] != "utf8" or
                    not isinstance(value["value"], str) or "\0" in value["value"] or
                    len(value["value"].encode("utf-8")) > 4096):
                raise ValueError("only bounded UTF-8 Apple Event parameters are supported")
        self._ready()
        if self.app.isTerminated():
            raise ValueError("target app process exited before the event")
        event_class, event_id = _fcc(invocation["class"]), _fcc(invocation["id"])
        address, event, reply = _AEDesc(), _AEDesc(), _AEDesc()
        raw_pid = C.c_int32(self.pid)
        def checked(status):
            if status != 0:
                raise ValueError(f"Apple Event construction failed: {status}")
        try:
            checked(self.lib.AECreateDesc(_fcc("kpid"), C.byref(raw_pid), C.sizeof(raw_pid), C.byref(address)))
            permission = int(self.lib.AEDeterminePermissionToAutomateTarget(C.byref(address), event_class, event_id, 0))
            if permission:
                raise PermissionError(f"guest Automation is not granted for the exact event: {permission}; no event sent")
            checked(self.lib.AECreateAppleEvent(event_class, event_id, C.byref(address), -1, 0, C.byref(event)))
            for keyword, value in invocation["parameters"].items():
                raw = value["value"].encode("utf-8")
                buffer = C.create_string_buffer(raw)
                checked(self.lib.AEPutParamPtr(C.byref(event), _fcc(keyword), _fcc("utf8"), buffer, len(raw)))
            started = time.monotonic()
            status = int(self.lib.AESendMessage(C.byref(event), C.byref(reply), 3 | 0x10 | 0x20000, math.ceil(timeout * 60)))
            reply_error = C.c_int32()
            actual_type, actual_size = C.c_uint32(), C.c_long()
            read_error = self.lib.AEGetParamPtr(C.byref(reply), _fcc("errn"), _fcc("long"), C.byref(actual_type),
                                               C.byref(reply_error), 4, C.byref(actual_size)) if status == 0 else -1701
            if read_error not in (0, -1701):
                status = int(read_error)
            return {"returncode": status or int(reply_error.value), "timed_out": status == -1712,
                    "elapsed_s": round(time.monotonic() - started, 4), "permission": permission,
                    "pid": self.pid, "event_class": invocation["class"], "event_id": invocation["id"]}
        finally:
            for desc in (reply, event, address):
                self.lib.AEDisposeDesc(C.byref(desc))


def proof_status(c: dict, proof: dict | None, current: dict) -> tuple[bool, str]:
    if c["target"] != current:
        return False, "app fingerprint changed; reanalyse and reverify"
    if (not isinstance(proof, dict) or proof.get("format") != R.PROOF or
            proof.get("candidate_sha256") != R.digest(c) or proof.get("target") != current or
            proof.get("backend") != BACKEND):
        return False, "no matching Apple Event verification receipt"
    if proof.get("dictionary", {}).get("sha256") != c["observer"]["dictionary_sha256"]:
        return False, "receipt dictionary resource does not match"
    rows = proof.get("cases", [])
    if not isinstance(rows, list) or len(rows) < 3:
        return False, "at least three independent checks must pass"
    for row in rows:
        try:
            query = bind(c, row["inputs"])["parameters"]["stxt"]["value"]
            expected, run = row["expected"], row["run"]
            valid = (row.get("verdict") == "verified" and row.get("observer") == "ax_dictionary" and
                     expected == row.get("seen") and row.get("before") != expected and
                     expected.get("query") == query and expected.get("dictionary") == c["observer"]["dictionary_label"] and
                     bool(expected.get("definition")) and run.get("returncode") == 0 and
                     not run.get("timed_out") and run.get("permission") == 0 and
                     row.get("inputs_sha256") == R.digest(row["inputs"]) and
                     row.get("before_sha256") == R.digest(row["before"]))
        except (KeyError, ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            return False, "receipt lacks a successful independent definition check"
    if len({r["inputs_sha256"] for r in rows}) < 3 or len({r["before_sha256"] for r in rows}) < 2:
        return False, "need three distinct inputs and two observed initial states"
    if proof.get("status") != "verified":
        return False, "verification did not finish successfully"
    return True, "measured lookup on this app and dictionary; local evidence, not a signature"


def _expected(c: dict, inputs: dict) -> tuple[dict, str]:
    query = bind(c, inputs)["parameters"]["stxt"]["value"]
    text = reference(c, query)
    return {"query": query, "dictionary": c["observer"]["dictionary_label"], "definition": normalize(text)}, text


def _trial(c: dict, inputs: dict, runtime: Runtime, folder: Path, expected: dict, timeout: float, *, require_change=False) -> dict:
    before_rows = runtime.read()
    before = observation(before_rows)
    R.write_json(folder / "before-ax.json", before_rows)
    if require_change and before == expected:
        raise ValueError("a no-op case cannot establish that the adapter works")
    started = time.monotonic()
    run = runtime.send(c, inputs, timeout)
    deadline = started + timeout
    after_rows = runtime.read()
    seen = observation(after_rows)
    while run["returncode"] == 0 and not run["timed_out"] and seen != expected and time.monotonic() < deadline:
        time.sleep(.1)  # read-only polling; the actor is never retried
        after_rows = runtime.read()
        seen = observation(after_rows)
    R.write_json(folder / "after-ax.json", after_rows)
    return {"inputs": inputs, "inputs_sha256": R.digest(inputs), "before": before,
            "before_sha256": R.digest(before), "expected": expected, "seen": seen,
            "observer": "ax_dictionary", "run": run, "elapsed_s": round(time.monotonic() - started, 4),
            "verdict": "verified" if run["returncode"] == 0 and not run["timed_out"] and seen == expected else "refuted"}


def verify(c: dict, cases: list[dict], bundle: str, out: str, timeout: float) -> dict:
    R.assert_guest()
    root = Path(bundle).resolve(strict=True)
    if R.fingerprint(root) != c["target"]:
        raise ValueError("candidate target changed")
    if not 1 <= timeout <= 300 or len(cases) < 3:
        raise ValueError("need at least three cases and a 1–300 second timeout")
    if any(not isinstance(case, dict) or set(case) != {"inputs"} for case in cases):
        raise ValueError("GUI cases contain inputs only; initial state is observed, not supplied")
    for case in cases:
        bind(c, case["inputs"])
    if len({R.digest(case["inputs"]) for case in cases}) < 3:
        raise ValueError("need three distinct input sets")
    dictionary = _environment(c)
    references = [_expected(c, case["inputs"]) for case in cases]  # before any event
    folder = Path(out).resolve()
    if folder.is_relative_to(root) or folder.is_relative_to(Path(c["observer"]["dictionary_path"])):
        raise ValueError("receipts must be outside app and dictionary")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    proof = {"format": R.PROOF, "backend": BACKEND, "candidate_sha256": R.digest(c),
             "target": c["target"], "dictionary": dictionary, "status": "incomplete", "cases": [], "model_calls": 0}
    try:
        runtime = Runtime(root)
        for i, (case, (expected, text)) in enumerate(zip(cases, references)):
            work = folder / f"case-{i}"
            work.mkdir(mode=0o700)
            R.write_json(work / "reference.json", {"source": c["observer"]["reference_xml"], "query": expected["query"], "text": text})
            row = _trial(c, case["inputs"], runtime, work, expected, timeout, require_change=True)
            proof["cases"].append(row)
            R.write_json(folder / "proof.json", proof)
            if row["verdict"] != "verified":
                raise ValueError("definition verification failed; stopped without actor retry or fallback")
        if R.fingerprint(root) != c["target"] or _environment(c) != dictionary:
            raise ValueError("app or dictionary changed during verification")
        proof["status"] = "verified"
        ready, why = proof_status(c, proof, c["target"])
        if not ready:
            proof["status"] = "incomplete"
            raise ValueError(why)
    except Exception as exc:
        proof["error"] = str(exc)
        raise
    finally:
        R.write_json(folder / "proof.json", proof)
    return proof


def replay(c: dict, proof: dict, inputs: dict, bundle: str, out: str, timeout: float) -> dict:
    R.assert_guest()
    root = Path(bundle).resolve(strict=True)
    ready, why = proof_status(c, proof, R.fingerprint(root))
    if not ready:
        raise ValueError(why)
    if not 1 <= timeout <= 300:
        raise ValueError("timeout must be 1–300 seconds")
    dictionary = _environment(c)
    expected, text = _expected(c, inputs)
    folder = Path(out).resolve()
    if folder.is_relative_to(root) or folder.is_relative_to(Path(c["observer"]["dictionary_path"])):
        raise ValueError("receipts must be outside app and dictionary")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    result = {"verdict": "uncertain", "retry": False, "observer": "ax_dictionary", "candidate_sha256": R.digest(c), "model_calls": 0}
    try:
        R.write_json(folder / "reference.json", {"source": c["observer"]["reference_xml"], "query": expected["query"], "text": text})
        result.update(_trial(c, inputs, Runtime(root), folder, expected, timeout))
        if R.fingerprint(root) != c["target"] or _environment(c) != dictionary:
            result["verdict"] = "refuted"
            result["error"] = "app or dictionary changed during replay"
    except Exception as exc:
        result["error"] = str(exc)
        raise
    finally:
        R.write_json(folder / "replay.json", result)
    return result
