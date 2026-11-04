"""MCP tool names in the format Anthropic's and OpenAI's tool APIs accept: ``^[a-zA-Z0-9_-]{1,64}$``.

A generated name is ``<namespace>__<name>``: the namespace is a host with its dots as underscores
(``crates_io__search_crates``) or a fixed prefix (``desktop__``, ``routine__``, ``skill__``). A name
over 64 characters is cut and ends in a hash of the whole name. The older dotted names (``agent.run``,
``<host>.<workflow>``, ``desktop.<workflow>``, ``routine.<name>``) are kept as aliases: a call by one
still reaches its tool, but only the new names are listed.

Tools of one module that would get the same name (:func:`unique`) all get a suffix from a hash of their
own key, so a name never moves to another tool when one is added; the shared name then calls none of
them. :class:`Registry` is one listing: every tool's listed name, its kind and source name, the aliases
that resolve to it and the shared names. Two tools of different kinds that would get the same name:
the second gets a suffix from a hash of its kind and source name.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MAX_LEN = 64
#: between a namespace (host, desktop, routine, skill) and a name
SEP = "__"
_BAD = re.compile(r"[^A-Za-z0-9_-]+")
_HASH_LEN = 8


def piece(value: Any, fallback: str = "workflow") -> str:
    """One name part: a run of characters outside ``[A-Za-z0-9_-]`` (dots included) becomes one ``_``. Not cut:
    :func:`fit` cuts the joined name and hashes all of it."""
    return _BAD.sub("_", str(value or "")).strip("_-") or fallback


def _digest(text: str, n: int = _HASH_LEN) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def fit(name: Any) -> str:
    """``name`` in the allowed format: other characters become ``_``; over 64 characters it is cut to
    55 and ends in ``_`` plus 8 hex characters of the whole name's sha256."""
    raw = str(name or "")
    clean = _BAD.sub("_", raw) or "tool"
    if len(clean) <= MAX_LEN:
        return clean
    return clean[:MAX_LEN - _HASH_LEN - 1] + "_" + _digest(raw)


def joined(namespace: Any, name: Any) -> str:
    """``<namespace>__<name>``, fitted."""
    return fit(f"{piece(namespace, 'site')}{SEP}{piece(name)}")


def suffixed(name: str, key: str) -> str:
    """``name`` with a stable suffix from ``key`` (one of two tools that would share a name)."""
    tag = "_" + _digest(key, 6)
    return fit(name)[:MAX_LEN - len(tag)] + tag


def unique(items: Sequence[Tuple[str, str]], taken: Iterable[str] = ()) -> List[str]:
    """The listed names of one module's tools, from each tool's ``(name, key)``; ``key`` names the tool itself
    (its host and workflow, or its directory). A name no other tool has is kept. Tools that share one, or whose
    name is in ``taken`` (kept by tools listed before them), each get ``suffixed(name, key)``, whichever came
    first: an added tool never takes a name over, the shared name calls none of them."""
    fixed = set(taken)
    bases = [fit(base) for base, _ in items]
    count = Counter(bases)
    names = [suffixed(base, key) if count[base] > 1 or base in fixed else base
             for base, (_, key) in zip(bases, items)]
    # a suffix equal to another tool's name (a hash clash) or one key twice (a copied record): position decides
    out: List[str] = []
    seen = set(fixed)
    for name, (_, key) in zip(names, items):
        n = 0
        while name in seen:
            n += 1
            name = suffixed(name, f"{key}\0{n}")
        seen.add(name)
        out.append(name)
    return out


class Entry(NamedTuple):
    name: str                       #: the listed name
    kind: str                       #: which module runs it
    source: str                     #: the name that module knows it by
    spec: Optional[Dict[str, Any]]


class Registry:
    """The tools of one listing: listed name -> entry, older name -> listed name, shared name -> the listed names
    of the tools that share it."""

    def __init__(self) -> None:
        self.entries: List[Entry] = []
        self._by_name: Dict[str, Entry] = {}
        self._alias: Dict[str, str] = {}
        self._shared: Dict[str, List[str]] = {}

    def add(self, kind: str, source: str, aliases: Iterable[str] = (),
            spec: Optional[Dict[str, Any]] = None, shares: str = "") -> Entry:
        """``shares``: the name this tool would have had alone, when another tool has it too."""
        source = str(source)
        base = fit(source)
        name, n = base, 0
        while name in self._by_name:
            name = suffixed(base, f"{kind}\0{source}" + (f"\0{n}" if n else ""))
            n += 1
        entry = Entry(name, kind, source, spec)
        self.entries.append(entry)
        self._by_name[name] = entry
        if source != name and source not in self._by_name:
            self._alias.setdefault(source, name)        # the module's own name: the first kind keeps it, as above
        for old in dict.fromkeys(str(item) for item in aliases if item):
            if old == name or old in self._by_name:
                continue
            if old in self._shared:
                self._shared[old].append(name)
            elif old in self._alias and self._alias[old] != name:
                # an older name two tools had calls neither (else it would move with each tool added)
                self._shared[old] = [self._alias.pop(old), name]
            else:
                self._alias[old] = name
        if shares:
            self._shared.setdefault(str(shares), []).append(name)
        return entry

    def names(self) -> List[str]:
        return [e.name for e in self.entries]

    def of_kind(self, kind: str) -> List[Entry]:
        return [e for e in self.entries if e.kind == kind]

    def resolve(self, name: str) -> Optional[Entry]:
        """The entry a listed name or an alias calls; None when neither is known."""
        hit = self._by_name.get(name)
        if hit is None and name in self._alias:
            hit = self._by_name.get(self._alias[name])
        return hit

    def sharing(self, name: str) -> List[str]:
        """The listed names of the tools that share ``name`` (which then calls none of them)."""
        return list(self._shared.get(name, ()))


def lookup(items: Iterable[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    """The item listed as ``name``, else the one item that has ``name`` among its ``aliases``; an older name two
    items share finds neither."""
    items = list(items)
    hit = next((item for item in items if item["name"] == name), None)
    if hit is None:
        hits = [item for item in items if name in item.get("aliases", ())]
        hit = hits[0] if len(hits) == 1 else None
    return hit


__all__ = ["PATTERN", "MAX_LEN", "SEP", "piece", "fit", "joined", "suffixed", "unique", "lookup", "Entry", "Registry"]
