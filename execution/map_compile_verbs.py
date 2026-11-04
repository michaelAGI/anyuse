"""The COVERAGE verb list of the map planner (``execution/map_compile.coverage_gaps``), kept apart from the
lexicon on purpose: the lexicon says what the planner CAN read; this list says what a task text ASKS, so a
clause the lexicon could not read is still seen as an instruction - and becomes a gap, never silence.

A clause (a sentence split at commas, semicolons, "and" / "und", "then" / "dann", "but" / "aber") is an
INSTRUCTION when one of its tokens begins with an action stem below and it holds no constraint word (a
constraint says what must stay: "leave that alone", "lass das so", "not the one for the café"). German stems
cover the imperative and infinitive forms the authors use ("sortier", "sortieren", "öffne", "öffnen",
"anlegen", "nachschlagen"); English stems cover the base form and -s / -ing.
"""
from __future__ import annotations

import re
from typing import Dict, Optional, List, Sequence, Tuple

#: action stems by the plan EFFECT that consumes them, matched as a token PREFIX on the normalized clause
#: (lower case, whitespace collapsed); a stem may serve several effects ("mach" makes a folder or a change)
VERB_EFFECTS = {
    # a stem with a trailing space is a WHOLE word (without it "find" read "Finder", "sort" read "Sorte:", "check" read
    # "Checkliste", "ordne" read "Ordner", "edit" read "Editor", "take" read "takes place", "add" read "address")
    # ("zeig mir die Notiz", "find my note about ...", "die Notiz raussuchen", "schau mal in den Notizen nach",
    # "im Lexikon aufrufen", "die Mehlmenge verdoppeln", "von 39 auf 42 Euro hochsetzen", "stell die Abholzeit auf
    # 9:00 Uhr um", "übernimm die Zeile in den Ablauf") - general verbs only, each with the effect it serves
    "open_item": ("open", "öffne", "öffn", "aufmach",
                  "zeig", "show ", "showing", "find ", "finde ", "raussuch", "heraussuch", "schau", "sieh", "guck", "aufruf"),
    "navigate": ("go to", "go into", "navigate", "geh ", "wechsel", "switch to"),
    "toggle_select": ("sort ", "sorting", "sortier", "order the", "order by", "ordne ", "group", "gruppier", "show ", "showing", "zeig", "display",
                      "switch", "stell", "set the view", "umstell"),
    "flag_toggle": ("turn ", "schalt", "enable", "disable", "aktivier", "deaktivier", "activate", "stell", "mach"),
    "doc_save": ("write", "schreib", "type", "tipp", "add ", "adding", "füg", "fueg", "append", "häng", "insert", "trag", "put ",
                 "enter ", "entering", "change ", "changing", "änder", "aender", "set ", "setz", "correct", "korrigier", "fix ", "fixing",
                 "update ", "updating", "aktualisier", "replace", "ersetz", "edit ", "editing", "bearbeit", "mach", "make", "rewrite",
                 "overwrite", "überschreib", "save ", "saving", "sicher", "speicher", "store",
                 "stell", "verdoppel", "halbier", "double", "halve", "hochsetz", "runtersetz", "erhöh", "senk",
                 "raise", "lower", "increase", "decrease", "übernimm", "übernehm", "eintrag", "nachtrag"),
    "object_create": ("create", "erstell", "erzeug", "leg ", "lege ", "anleg", "new folder", "neuen ordner", "make", "mach"),
    "scoped_find": ("look ", "looking", "schlag ", "nachschlag", "search ", "searching", "such ", "suche", "durchsuch",
                    "check ", "prüf", "aufruf", "nachseh", "nachschau", "nachguck", "schau", "sieh"),              # ("find" = locate)
    "read_content": ("read", "lies", "copy", "kopier", "grab", "take ", "taking", "nimm", "übernimm", "use ", "nutz", "verwend",
                     "schau", "sieh", "guck", "nachseh", "nachschau", "übernehm"),
    "select_item": ("pick", "wähl", "select", "find ", "finde ", "raussuch", "heraussuch"),
    # verbs no transition of the desktop maps serves: always an honest gap
    "": ("rename", "benenn", "move", "verschieb", "delete", "lösch", "remove", "entfern", "trash", "papierkorb"),
}
ACTION_STEMS: Tuple[str, ...] = tuple(sorted({st for stems in VERB_EFFECTS.values() for st in stems}))
#: a German separable verb - its stem and its prefix standing apart ("mach ... auf", "stell ... um",
#: "trag ... ein", "such ... raus") - serves the effect of the WHOLE verb, not the stem's
SEPARABLE_VERBS: Dict[Tuple[str, str], Tuple[str, ...]] = {
    ("mach", "auf"): ("open_item",), ("stell", "um"): ("toggle_select", "doc_save"),
    ("trag", "ein"): ("doc_save",), ("trag", "nach"): ("doc_save",), ("such", "raus"): ("open_item", "select_item"),
    ("leg", "an"): ("object_create",), ("schalt", "aus"): ("flag_toggle",), ("schalt", "ein"): ("flag_toggle",),
    ("schalt", "an"): ("flag_toggle",), ("stell", "aus"): ("flag_toggle",), ("stell", "ein"): ("flag_toggle",),
    ("stell", "an"): ("flag_toggle",), ("häng", "an"): ("doc_save",), ("häng", "dran"): ("doc_save",),
    ("setz", "hoch"): ("doc_save",), ("setz", "runter"): ("doc_save",), ("ruf", "auf"): ("scoped_find",),
    ("schau", "nach"): ("scoped_find", "read_content"), ("seh", "nach"): ("scoped_find", "read_content"),
}
#: a verb right after a first-person subject describes what the person did, not what the plan should do
#: ("the one I changed most recently", "das ich mir notiert hab"); a verb stem right after an article is an adjective
#: or participle on a noun ("die korrigierte Notiz", "the open visitor sheet", "der geöffnete Ordner"), no imperative
_PERSON_BEFORE = frozenset({"i", "ich", "we", "wir", "nobody", "niemand", "i've", "we've", "i'd", "we'd",
                            "the", "a", "an", "my", "die", "der", "das", "den", "dem", "des", "eine", "einen", "einer",
                            "einem", "meine", "meinen", "meiner", "meinem", "this", "that", "diese", "diesen", "dieser"})
#: a clause with one of these is a CONSTRAINT (what must stay), not an instruction
CONSTRAINT_WORDS: Tuple[str, ...] = (
    "leave ", "keep ", "ignore", "don't", "do not", "never", "not the", "nothing else", "alone", "untouched",
    "as is", "as it is", "lass ", "lasse ", "lassen", "bleib", "ignorier", "nicht ", "kein", "unverändert", "so lassen",
    "in ruhe", "unangetastet", "nur das", "only the",
)
#: words that only restate a save (a write step saves): a clause of these alone is consumed by any doc_save
SAVE_WORDS: Tuple[str, ...] = ("save", "saving", "sicher", "speicher", "store")

_SPLIT = ("; ", ", ", " and ", " und ", " then ", " dann ", " but ", " aber ", " sowie ", " anschließend ")


def normalize(s: str) -> str:
    return " ".join(str(s or "").lower().split())


_SENT_END = re.compile(r"(?<!\d)[.!?](?=\s+[A-ZÄÖÜ„\"'(]|\s*$)")


def sentences(text: str) -> List[str]:
    """Sentences: a period / ! / ? followed by whitespace and an uppercase letter (or the end), never after a
    digit ("3. Oktober") nor inside a token ("Rechnung_Wasser.txt")."""
    out: List[str] = []
    start = 0
    t = str(text or "")
    for m in _SENT_END.finditer(t):
        out.append(t[start:m.end()].strip())
        start = m.end()
    if t[start:].strip():
        out.append(t[start:].strip())
    return [x for x in out if x]


def clauses(text: str) -> List[Tuple[int, str]]:
    """The clauses of a task text as (sentence index, normalized clause), in order."""
    out: List[Tuple[int, str]] = []
    for k, s in enumerate(sentences(text)):
        n = normalize(s)
        parts = [n]
        for sep in _SPLIT:
            nxt: List[str] = []
            for p in parts:
                nxt += [x for x in p.split(sep) if x.strip()]
            parts = nxt
        out += [(k, p.strip(" .!?;,")) for p in parts if p.strip(" .!?;,")]
    return out


def clause_effects(clause: str) -> List[str]:
    """The effects whose verbs the clause holds (the verb list, not the lexicon), in the verb list's order."""
    effs = {e for _v, es in clause_verbs(clause) for e in es}
    return [eff for eff in VERB_EFFECTS if eff in effs]


def _past_form(c: str, i: int, st: str) -> bool:
    """The stem at ``i`` is a past form or follows a first-person subject - what the person did, not an
    instruction ("the one i changed most recently", "that the dentist moved", "das ich mir notiert hab")."""
    j = i + 1
    k = c.find(" ", j)
    word = c[j:k if k >= 0 else len(c)]
    before = c[:i].split()
    prev = before[-1] if before else ""
    if prev in _PERSON_BEFORE:
        return True
    if word.endswith("ed") and not st.strip().endswith("ed") and word != st.strip():
        return True
    return word.startswith("ge") and not st.startswith("ge") and word != st.strip()


def _separable(c: str, i: int, st: str) -> Optional[Tuple[str, ...]]:
    """The prefix of a separable verb standing apart after its stem ("mach ... auf"): the whole verb's
    effects, else None."""
    rest = c[i + 1 + len(st):]
    toks = rest.split()
    for (stem, prefix), effs in SEPARABLE_VERBS.items():
        if st.strip() == stem and prefix in toks:
            return effs
    return None


def clause_verbs(clause: str) -> List[Tuple[str, List[str]]]:
    """The distinct verb stems of a clause with the effects each serves, in order of appearance (rule 5: a step per
    verb). Two stems at the same position (a stem and its longer form) count once; a past form or a verb after a
    first-person subject is not an instruction; a separable verb serves the whole verb's effect."""
    c = " " + normalize(clause) + " "
    found: List[Tuple[int, str]] = []
    for eff, stems in VERB_EFFECTS.items():
        for st in stems:
            i = c.find(" " + st)
            while i >= 0:
                if not _past_form(c, i, st):
                    found.append((i, st))
                i = c.find(" " + st, i + 1)
    out: List[Tuple[str, List[str], int]] = []
    for i, st in sorted(set(found)):
        sep = _separable(c, i, st)
        effs = list(sep) if sep is not None else [eff for eff, stems in VERB_EFFECTS.items() if st in stems]
        if out and abs(i - out[-1][2]) <= 1:            # the same word matched by two stems ("leg " / "lege ")
            out[-1] = (out[-1][0], sorted(set(out[-1][1]) | set(effs)), out[-1][2])
            continue
        out.append((st.strip(), effs, i))
    return [(v, e) for v, e, _i in out]


_SOURCE = re.compile(r"\b(?:aus|laut|in|from|per)\s+(?:meiner|meinen|meinem|der|den|dem|my|the)\s+(?:[\w\-äöüß]+\s+){0,2}?"
                     r"[\w\-äöüß]*(?:notiz|notizen|note|notes|datei|dateien|file|files|liste|list|protokoll|log|sheet|blatt)\b"
                     r"|\b(?:die|eine|der|the|a|my|meine)\s+(?:[\w\-äöüß]+\s+){0,2}?(?:notiz|note|datei|file)\s+"
                     r"(?:zu|zum|zur|über|mit|about|with|where|wo|that|which|in der|in dem|in which)\b"
                     r"|\b(?:dort|darin|da)\s+(?:genannt|notiert|steht|aufgeschrieben)|\b(?:mentioned|noted|written)\s+(?:there|in it)\b"
                     r"|\b(?:aus|from)\s+[„“\"'»][^„“\"”'«»]{2,60}[“”\"'«»]")   # "aus „Sternwarte Absprachen“"


def source_phrase(clause: str) -> str:
    """A source the clause names ("aus meiner Notiz über ...", "from my kayak club notes", "in the file", a German
    compound "aus meinen Paddelnotizen") - rule 6 / 7: it must be opened or read by a step. Not the app's
    name alone ("in Notes", "in TextEdit")."""
    m = _SOURCE.search(normalize(clause))
    return m.group(0).strip() if m else ""


def is_constraint(clause: str) -> bool:
    c = " " + normalize(clause) + " "
    return any((" " + w) in c or c.startswith(w) for w in CONSTRAINT_WORDS)


def is_instruction(clause: str) -> bool:
    """An instruction: a token begins with an action stem that is not a past form (``clause_verbs``), and no
    constraint word is in the clause."""
    if is_constraint(clause):
        return False
    return bool(clause_verbs(clause))


_CONTAINER_NOUN = re.compile(r"\b(?:in|im|inside|into|within|unter|nur in|only in)\b[^,;]{0,40}?\b(?:sub)?(?:folder|ordner|unterordner)\b")
_CONTAINER_QUOTED = re.compile(r"\b(?:in|im|inside|into|within|nur in|only in|unter)\s+(?:the\s+|my\s+|den\s+|dem\s+|der\s+)?[„“\"'»]([^„“\"”'«»]{2,60})[“”\"'«»]")
_LITERAL = re.compile(r"[„“\"'»][^„“\"”'«»]{1,200}[“”\"'«»]|\b[\w\-]+\.(?:txt|md|csv|rtf|pdf|json|log)\b|\d")


def container_phrase(clause: str) -> str:
    """The container a clause names ("in the X folder", "nur in „Aktuell“"), or "" - rule 1: an unnavigated
    container phrase is a gap."""
    c = normalize(clause)
    m = _CONTAINER_QUOTED.search(c)
    if m:
        return m.group(1).strip()
    m = _CONTAINER_NOUN.search(c)
    if m:
        return m.group(0).strip()
    return ""


def has_literal(clause: str) -> bool:
    """Rule 2: a clause with a literal of its own (a quoted span, a file name, a number) must be consumed by its
    own step, never by a neighbour's."""
    return bool(_LITERAL.search(str(clause or "")))


#: rule 3: a lookup never binds one of these
STOP_WORDS = frozenset({"das", "mal", "es", "it", "the", "den", "die", "der", "this", "that", "dies", "diesen", "was",
                        "what", "up", "mir", "dir", "bitte", "please", "them", "sie", "ihn", "him", "her", "one", "eins",
                        "da", "dort", "there", "here", "hier", "nochmal", "again", "auch", "also", "then", "dann"})
#: rule 4: a German separable verb prefix at the end of a captured value belongs to the verb, not the value
SEPARABLE_PREFIXES = ("um", "ein", "aus", "an", "ab", "auf", "zu", "nach", "vor", "weg", "dran", "rein", "hinzu", "fest",
                      "hin", "her", "mit", "nieder", "los", "zurück")


def is_stop_phrase(value: str) -> bool:
    toks = [t for t in normalize(value).replace("'", " ").split() if t]
    return not toks or all(t in STOP_WORDS for t in toks)


def strip_separable_prefix(value: str) -> str:
    toks = str(value or "").split()
    if len(toks) > 1 and toks[-1].lower() in SEPARABLE_PREFIXES:
        return " ".join(toks[:-1])
    return str(value or "")


def is_save_only(clause: str) -> bool:
    toks = [t for t in normalize(clause).replace(",", " ").split() if t not in ("it", "es", "das", "the", "and", "und", "then", "dann", "bitte", "please", "them", "sie")]
    return bool(toks) and all(any(t.startswith(w) for w in SAVE_WORDS) for t in toks)


def instruction_clauses(text: str) -> List[Tuple[int, int, str]]:
    return [(i, k, c) for i, (k, c) in enumerate(clauses(text)) if is_instruction(c)]
