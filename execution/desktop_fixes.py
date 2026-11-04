"""General fixes for the desktop engine, as ONE layer over the ``desktop_e2e*`` arms. PURE like them (no pyobjc,
no subprocess, no network).

The ``desktop_e2e*`` modules stay byte-identical: their arms are the frozen incumbents of the measured tests
(``desktop_e2e8.Arch8`` is the reference arm), and their tests pin the bytes. The layer is the mixin ``DesktopFixes`` (put
it FIRST in the bases). It is part of every current consumer -- the checked replay of a routine
(``desktop_routine.ReplayArch``; through it the escalation's executor ``EscExec`` and the MCP routine tools) --
and of ``Arch8F`` (``Arch8`` with the layer) for a run that plans; ``with_desktop_fixes(cls)`` puts it over any arm from
``desktop_e2e5`` on.

1. **The extracted value ends at its clause** (e.g. the note read "Agreed collection point: West
   courtyard."; the plan's extract named the words with the note's period and the checklist was saved as "Collection
   point: West courtyard."). Sentence punctuation (. ! ? ; : ,) that closes the sentence the words stand in is left
   out of the bound value unless it is part of the requested value: the field names a sentence, line or wording;
   the span is a whole sentence or line; an abbreviation or initial ("Co.", "z.B.", "J."); an ellipsis; punctuation
   inside a token ("3.5"); an ordinal followed by its month ("am 3. Oktober"). The punctuation left out is
   remembered: a write that forms a line of its own (append / insert / replace ending with the variable) takes it
   back (tasks of ``desktop_e2e5`` and ``desktop_e2e11`` were authored so); an edit writes INTO an existing line,
   whose own punctuation stays.
3. **Names compare in NFC** (Finder created the folder 'Rückfragen' and listed it
   DECOMPOSED; the executor's check reported "no object named 'Rückfragen' afterwards (new: ['Rückfragen'])", the
   step was not compiled, the routine lacked it). The layer's guest boundary hands every NAME of a guest answer
   (row labels, the folder or note shown, paths and titles, a listing's names) to the arm in NFC, and a new
   object's planned name too; the routine's checks (``desktop_routine``), the object contract
   (``desktop_contracts``) and the guest's ``listing`` compare composed; ``run_one_nfc`` gives the frozen judges
   (up to judge) their inputs -- the task's reads and expected state, the reader's observations -- in NFC,
   so every judge comparison is composed without a byte of the frozen judges changing.
4. **A descriptive scope resolves to exactly one recorded container, or the caller is asked** (``desktop_e2e4``'s
   location check refused descriptions: e.g. the plan's scope 'Angebote für draußen' while the run had entered
   'Außenflächen' FOR that very description, or a plan that named the document 'aktuellen'). A scope that
   names a container (``desktop_e2e4``'s label rule) is checked as before; a scope that names one of the places, files
   or notes this run saw but not the one shown is still refused. A scope that names none is a DESCRIPTION: it resolves
   to the container (or document) an earlier checked step of this run resolved the same words to -- a navigate's
   target, an open's or select's target or scope (the map, the picker or the compiled replay) -- and then must be
   the one shown; a description no step resolved (or resolved to two) is never guessed: the step is not taken and
   the run hands off asking the caller for the parameter ("hand-off (declined): asked the caller for the
   parameter: ..."), never a refusal.
"""
from __future__ import annotations

import os
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import desktop_e2e as X
from . import desktop_e2e4 as W
from . import desktop_e2e5 as V

LAYER = "desktop_fixes"

# ---------------------------------------------------------------------------
# 1. The extracted value ends at its clause
# ---------------------------------------------------------------------------

#: punctuation that closes a sentence or a clause: never part of an extracted value unless the value asks for it
CLAUSE_PUNCT = ".!?;:,"
#: field words that ask for a whole sentence, line or wording: its closing punctuation is part of the value
SENTENCE_WORDS = {"sentence", "sentences", "satz", "sätze", "line", "lines", "zeile", "zeilen", "wording", "wortlaut",
                  "quote", "zitat", "phrase", "formulierung", "message", "nachricht", "slogan", "motto", "spruch",
                  "text", "hinweis", "notice"}
#: abbreviations whose period belongs to the word (German and English, as the pools write them); a token with an
#: inner period ("z.B.", "U.S.") or a single letter (an initial) is one too
ABBREVIATIONS = {"dr", "prof", "nr", "no", "str", "st", "bzw", "usw", "etc", "ca", "inkl", "zzgl", "ggf", "evtl", "vgl",
                 "mr", "mrs", "ms", "jr", "sr", "co", "inc", "ltd", "corp", "fr", "hr", "tel", "abs", "bspw", "ff"}
_QUOTES = "\"'„“”‚‘’«»"
_CLOSERS = "\"'”’»)]"
WRITES = ("doc_save", "field_rw")


def asks_sentence(target: str) -> bool:
    return bool(set(X.tokens(target)) & SENTENCE_WORDS)


def _abbreviation(token: str) -> bool:
    t = token.strip(_QUOTES + "([").rstrip(".")
    return bool(t) and ("." in t or (len(t) == 1 and t.isalpha()) or t.casefold() in ABBREVIATIONS)


def _sentence_start(text: str, k: int) -> bool:
    """Does a sentence (or a line) start at ``k``: the text's or a line's start, or after . ! ? and a space?"""
    j = k
    while j > 0 and text[j - 1] in " \t":
        j -= 1
    return j == 0 or text[j - 1] == "\n" or (j < k and text[j - 1] in ".!?")


def clause_end(text: str, start: int, end: int, target: str = "") -> Tuple[int, str]:
    """(where the value ends, the punctuation left out) for the span ``text[start:end]``: the extracted span ends
    at the clause, not at the sentence punctuation that closes it, unless the value asks for it (module docstring,
    item 1)."""
    span = text[start:end]
    k = end
    while k > start and text[k - 1] in CLAUSE_PUNCT:
        k -= 1
    if k == end or k == start or asks_sentence(target) or span.endswith(("...", "…")):
        return end, ""
    nxt = text[end:end + 1]
    if nxt and not (nxt.isspace() or nxt in _CLOSERS):
        return end, ""                                   # inside a token: "3.5", "a.b"
    if _sentence_start(text, start):
        return end, ""                                   # a whole sentence or line is the value
    if text[k] == ".":
        words = text[start:k].split()
        tok = words[-1] if words else ""
        if _abbreviation(tok + "."):
            return end, ""
        rest = text[end:].split("\n", 1)[0]
        if tok.isdigit() and rest.strip() and rest[:1] == " ":
            return end, ""                               # an ordinal: "am 3. Oktober"
    return k, text[k:end]


def extract_clause(texts: Sequence[str], words: str, target: str = "") -> Tuple[Optional[str], str, str]:
    """(the detail, how, the closing punctuation left out) or (None, why, ""): ``desktop_e2e5.extract_detail`` (the
    words occur in a text read, the latest read first; never the item's title or the whole text) with the detail
    ending at its clause. The span is sliced from the NFC text ``locate`` searched (slicing the raw text with
    the NFC offsets cuts a decomposed note at the wrong place)."""
    span, how = V.extract_detail(texts, words, target)
    if span is None:
        return None, how, ""
    for text in reversed(list(texts)):
        hit = V.locate(text, words)
        if hit is None:
            continue
        t = unicodedata.normalize("NFC", text or "")
        k, left = clause_end(t, hit[0], hit[1], target)
        if left:
            return t[hit[0]:k], f"{hit[2]}; the sentence punctuation {left!r} is not part of the value", left
        return t[hit[0]:hit[1]], hit[2], ""
    return span, how, ""


def line_value(step: Dict[str, Any], concrete: Dict[str, Any], closing: Dict[str, str]) -> Optional[str]:
    """The value of a write that forms a line of its own (append / insert / replace) and ENDS with an extracted
    variable whose sentence punctuation the extract left out: that punctuation back (the value is the source's
    sentence). None otherwise: an edit writes INTO an existing line, whose own punctuation stays as it was (e.g.
    "Collection point: TBD" -> "Collection point: West courtyard")."""
    if step.get("effect") not in WRITES or (step.get("mode") or "replace") not in ("append", "insert", "replace"):
        return None
    planned = str(step.get("value") or "").rstrip()
    refs = W.VAR_RE.findall(planned)
    if not refs or not planned.endswith("}}") or not closing.get(refs[-1]):
        return None
    value = str(concrete.get("value") or "").rstrip()
    return None if value.endswith(closing[refs[-1]]) else value + closing[refs[-1]]


# ---------------------------------------------------------------------------
# 3. Names compare in NFC
# ---------------------------------------------------------------------------

def nfc(s: Any) -> Any:
    """A name (or any text) in Unicode NFC; anything that is not a string as it is."""
    return unicodedata.normalize("NFC", s) if isinstance(s, str) else s


def same_name(a: Any, b: Any) -> bool:
    """Two names are the same name when their NFC forms are equal (a composed vs a decomposed umlaut)."""
    return nfc(a if a is not None else "") == nfc(b if b is not None else "")


def nfc_deep(x: Any) -> Any:
    """``x`` with every string (dict keys too) in NFC; lists, tuples and dicts copied."""
    if isinstance(x, str):
        return nfc(x)
    if isinstance(x, dict):
        return {nfc_deep(k): nfc_deep(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return type(x)(nfc_deep(v) for v in x)
    return x


#: the fields of a guest answer that carry NAMES (items, folders, documents, notes). File contents (``text``) and
#: hashes are never touched.
NAME_FIELDS = ("where", "shown_note", "shown_before", "path", "title", "document", "note", "folder", "file", "label")
_ROW_FIELDS = ("label", "text", "folder", "line")


def nfc_names(op: str, r: Any) -> Any:
    """A guest answer with its names in NFC (``NAME_FIELDS``, the rows' labels and texts, a listing's names, the keys
    of the per-item maps of ``item_dates`` and ``previews``); everything else as it was."""
    if not isinstance(r, dict):
        return r
    out = dict(r)
    for k in NAME_FIELDS:
        if isinstance(out.get(k), str):
            out[k] = nfc(out[k])
    if isinstance(out.get("names"), list):
        out["names"] = [nfc(n) for n in out["names"]]
    if isinstance(out.get("rows"), list):
        out["rows"] = [dict(row, **{k: nfc(row[k]) for k in _ROW_FIELDS if isinstance(row.get(k), str)})
                       if isinstance(row, dict) else row for row in out["rows"]]
    if op in ("item_dates", "previews"):
        for k in ("items", "previews"):
            if isinstance(out.get(k), dict):
                out[k] = {nfc(n): v for n, v in out[k].items()}
    return out


def nfc_judge(task: X.Task, start: Any, end: Any) -> Tuple[X.Task, Any, Any]:
    """A judge's inputs with every name in NFC: the task's reads and expected state, the reader's observations."""
    import dataclasses
    return (dataclasses.replace(task, read=nfc_deep(task.read), expect=nfc_deep(task.expect)),
            nfc_deep(start), nfc_deep(end))


class NFCChannel:
    """A guest channel whose answers carry their names in NFC; the reader's observations (``clean_start`` and
    ``read_state``: what the judge compares) entirely in NFC."""

    def __init__(self, guest):
        self.g = guest

    def __call__(self, op, **kw):
        r = self.g(op, **kw)
        if op in ("clean_start", "read_state") and isinstance(r, dict) and isinstance(r.get("read"), list):
            r = dict(r, read=nfc_deep(r["read"]))
        return nfc_names(op, r)

    def __getattr__(self, name):
        return getattr(self.g, name)


def run_one_nfc(task: X.Task, arm: str, run: int, guest, **kw):
    """``desktop_e2e9.run_one9`` (judge, judge and judge kept; the later desktop_e2e modules run it too)
    with the judges' inputs in NFC: the task's reads and expected state, and the guest's answers (``NFCChannel``)."""
    from . import desktop_e2e9 as N
    import dataclasses
    t = dataclasses.replace(task, read=nfc_deep(task.read), expect=nfc_deep(task.expect))
    return N.run_one9(t, arm, run, NFCChannel(guest), **kw)


# ---------------------------------------------------------------------------
# 4. A descriptive scope resolves to exactly one recorded container, or the caller is asked
# ---------------------------------------------------------------------------

ASK_STOP = "hand-off (declined): asked the caller for the parameter"
#: the guest lists whose labels are places, files or notes this run SAW
_SEEN_KINDS = ("finder_locations", "notes_folders", "finder_files", "notes_notes")


def words_key(words: Any) -> str:
    return X.norm(words)


def stem(name: Any) -> str:
    """A file name without its extension ("Probenplan.txt" -> "Probenplan"); a folder or note name as it is."""
    n = os.path.basename(str(name or "").rstrip("/"))
    return os.path.splitext(n)[0] if W.FILE_RE.search(n) else n


class Ask(str):
    """A location check's answer that is not a refusal: the caller must name the container (the step is not taken)."""


# ---------------------------------------------------------------------------
# The layer
# ---------------------------------------------------------------------------

class DesktopFixes:
    """The layer over a ``desktop_e2e*`` arm (``desktop_e2e5`` on: the arm reads, extracts and writes with modes). Put
    it FIRST in the bases: ``class A(DesktopFixes, Arch12)``."""

    layer = LAYER

    def run(self, task, rec, compiled=None):
        self._reset2()
        rec.__dict__.setdefault("layer", LAYER)
        out = super().run(task, rec, compiled)
        last = next((s for s in reversed(rec.steps) if s.get("outcome") == "handoff"), None)
        if last is not None and last.get("ask2") and "hand-off" in str(rec.stopped):
            rec.stopped = f"{ASK_STOP}: {last['ask2']}"[:400]
        return out

    def _reset2(self):
        self._closing2: Dict[str, str] = {}
        self._resolved2: Dict[str, Dict[str, Set[str]]] = {"container": {}, "document": {}}
        self._seen2: Set[str] = set()
        self._ask2: Optional[str] = None

    def _state2(self):
        if not hasattr(self, "_resolved2"):
            self._reset2()

    # -- 3. names in NFC at the guest boundary ----------------------------------------------------------
    def call(self, op, **kw):
        r = nfc_names(op, super().call(op, **kw))
        self._state2()
        if op == "rows" and kw.get("kind") in _SEEN_KINDS:
            self._seen2.update(str(x.get("label") or "") for x in r.get("rows") or [] if isinstance(x, dict))
        elif op == "listing":
            self._seen2.update(str(n) for n in r.get("names") or [])
        elif op == "front" and r.get("document"):
            self._seen2.add(os.path.basename(str(r["document"])))
        return r

    # -- 4. descriptive scopes -------------------------------------------------------------------------
    def _resolve2(self, kind: str, words: Any, value: Any) -> None:
        if words and value:
            self._resolved2[kind].setdefault(words_key(words), set()).add(nfc(str(value)))

    def _record2(self, step: Dict[str, Any], log: Dict[str, Any]) -> None:
        """What this checked step resolved: a navigate's target to its container; an open's or select's target and
        scope to its item (a file's or note's name)."""
        res = log.get("resolved") or {}
        lab, eff = res.get("label"), step.get("effect")
        if eff == "navigate" and res.get("op") == "navigate" and lab:
            self._resolve2("container", step.get("target"), lab)
            self._resolve2("container", lab, lab)
        elif eff in ("open_item", "select_item") and lab:
            for words in (step.get("target"), step.get("scope"), lab):
                self._resolve2("document", words, lab)
            if res.get("enter"):
                self._resolve2("container", step.get("scope"), res["enter"][-1])

    def _names_seen2(self, named: str) -> bool:
        """Does the scope NAME one of the places, files or notes this run saw (``desktop_e2e4``'s label rule)?"""
        return any(W.names_location(named, stem(n)) for n in self._seen2 if n)

    def _scope2(self, kind: str, named: str, shown: str, log: Dict[str, Any]) -> Tuple[Optional[str], str]:
        """(None = the description resolves to the one shown, else the refusal or an ``Ask``; what was decided)."""
        if self._names_seen2(named):
            return "", "names another"                    # a NAME of another place this run saw: desktop_e2e4 refuses
        res = sorted(self._resolved2[kind].get(words_key(named)) or ())
        if len(res) == 1:
            if same_name(stem(res[0]), stem(shown)) or W.names_location(stem(res[0]), stem(shown)):
                log["location2"] = (f"the plan's {named!r} was resolved in this run to {res[0]!r}: the {kind} shown "
                                    f"{shown!r} is that one")
                return None, "resolved"
            return (f"the {kind} shown is {shown!r}; the plan's {named!r} was resolved in this run to {res[0]!r}",
                    "resolved to another")
        why = (f"the plan's scope {named!r} describes a {kind}" +
               (f" this run resolved to {len(res)} different ones ({', '.join(map(repr, res))})" if res else
                " (it names none of the places, files or notes this run saw) and no step of this run resolved it") +
               ": the caller is asked which one is meant (nothing done)")
        return Ask(why), "ask"

    def _location_check(self, rec, step, log, bid) -> Optional[str]:
        """``desktop_e2e4``'s check (the container shown is the one the plan names) with descriptive scopes resolved
        (module docstring, item 4). The same reads as ``desktop_e2e4``'s: the scope (or the last navigate's words),
        then the container shown."""
        self._state2()
        app = X.APP_OF[bid]
        named = self._named_container(app, step)
        if not named:
            log["location"] = "the plan names no container (nothing to verify)"
            return None
        if bid == "com.apple.finder":
            path = self.call("shown_folder", bundle=bid).get("path") or ""
            shown = os.path.basename(path)
        elif bid == "com.apple.Notes":
            shown = self.call("rows", bundle=bid, kind="notes_folders").get("where") or ""
        else:
            log["location"] = f"no container reader for {app}"
            return None
        if W.names_location(named, shown):
            log["location"] = f"container {shown!r} is the one the plan names ({named!r})"
            return None
        why, how = self._scope2("container", named, shown, log)
        if why is None:
            log["location"] = log["location2"]
            return None
        if how == "names another":
            return f"the container shown is {shown!r}, the plan names {named!r}"
        if isinstance(why, Ask):
            self._ask2 = str(why)
        return why

    def _document_check(self, rec, step, log, doc):
        """``desktop_e2e4``'s document check (and a later arm's own, e.g. ``desktop_e2e9``'s identity recheck) with a
        descriptive document scope resolved first: resolved to the front document, the rest of the check runs as if
        the plan had named it; resolved elsewhere, a refusal; unresolved, the caller is asked."""
        self._state2()
        named = step.get("document") or ""
        name = os.path.basename(doc or "")
        if named and not W.names_location(named, stem(name)):
            why, how = self._scope2("document", named, name, log)
            if why is None:
                step = dict(step, document=stem(name))
            elif how != "names another":
                if isinstance(why, Ask):
                    self._ask2 = str(why)
                return why
        return super()._document_check(rec, step, log, doc)

    # -- 1. the extracted value ends at its clause ---------------------------------------------------
    def _extract(self, task, rec, step, log, fixed):
        out = super()._extract(task, rec, step, log, fixed)
        if out != "ok":
            return out
        name = W.var_name(step["value"])
        span, how, closing = extract_clause(list(self._reads.values()), step.get("find") or "",
                                            step.get("target") or "")
        if span is None:
            return out
        if not hasattr(self, "_closing2"):
            self._closing2 = {}
        self._closing2[name] = closing
        if span != self._vars.get(name):
            self._vars[name] = span
            log["resolved"] = dict(log.get("resolved") or {}, label=span)
            log["extract"] = dict(log.get("extract") or {}, bound=span, how=how,
                                  **({"closing": closing} if closing else {}))
            log["check"] = f"the detail {span!r} occurs in the text read ({how})"
            if rec.extracts and rec.extracts[-1].get("var") == name:
                rec.extracts[-1].update(bound=span, how=how)
            prov = log.get("provenance12")
            if isinstance(prov, dict):
                prov["value"] = span
        return out

    def do_step(self, task, rec, step, log, fixed=None):
        self._state2()
        self._ask2 = None
        if step.get("effect") == "object_create" and step.get("value") != nfc(step.get("value")):
            step = dict(step, value=nfc(step.get("value")))           # a new object's planned name, composed
            log["step"] = step
        planned = log.get("planned")
        if isinstance(planned, dict):
            lv = line_value(planned, step, getattr(self, "_closing2", {}))
            if lv is not None:
                step = dict(step, value=lv)
                log["step"] = step
                log["line_value"] = ("the extract's sentence punctuation restored: the value forms a line of its own "
                                     f"({planned.get('mode') or 'replace'})")
        out = super().do_step(task, rec, step, log, fixed)
        if out == "location" and self._ask2:
            # never a refusal for a description no step resolved, never a guess: the caller names the container
            log["why"], log["ask2"] = self._ask2, self._ask2
            rec.handoffs += 1
            out = "handoff"
        elif out == "ok":
            self._record2(step, log)
        self._ask2 = None
        return out


def with_desktop_fixes(cls):
    """``cls`` (a ``desktop_e2e*`` arm, ``desktop_e2e5`` on) with the layer first in its bases."""
    return type(f"{cls.__name__}F", (DesktopFixes, cls), {"__doc__": f"{cls.__name__} with the desktop_fixes layer."})


def _arch8f():
    from . import desktop_e2e8 as R8
    return with_desktop_fixes(R8.Arch8)


#: ``Arch8`` with the layer: for a run that plans with the fixes (the engine stage before an escalation)
Arch8F = _arch8f()
