"""Where in a URL the user's typed value actually sits — including the awkward cases.

Binding a parameter only to a **whole** query value or a **whole** path
segment, and refusing everything else, is safe (guessing which
part of a path is the parameter produces templates that silently answer a
different question), but it was also the single most concentrated cause of
failure measured: three of eleven detail workflows failed for exactly
this reason, because the item is identified by a slug that spans several path
segments —

    https://packagist.org/packages/guzzlehttp/guzzle        pkg = guzzlehttp/guzzle
    https://gitea.com/gitea/tea                             repo = gitea/tea
    https://standardebooks.org/ebooks/charles-dickens/a-christmas-carol

This module replaces the refusal with **evidence**. It never decides from one
URL. A binding site is proposed from one recording and then has to be
corroborated by *every other recording of a different value*: the same kind of
site, at the same place, leaving the same constant remainder. Anything not
corroborated is dropped with a reason. Anything corroborated is still only a
candidate — the fresh-input proof remains the only thing that can persist a
template, and a wrong split shows up there as a 404 or a mismatch.

Three kinds of site, in the order they are preferred:

``query``      the value is a whole query value            ``?q=serde``
``path_run``   the value is a run of whole path segments   ``/packages/guzzlehttp/guzzle``
``path_part``  the value is a delimited part of ONE segment ``/book/1234-a-christmas-carol``

and four deterministic transforms between what the user typed and what the URL
carries. A transform is only used when **every** recording agrees on the same
one; recordings that disagree produce a refusal naming both.

What this module deliberately does not do
-----------------------------------------
* It does not invent a transform. The four below are the whole list, they are
  all reversible enough to check, and an unlisted one is a refusal, not a guess.
* It does not bind a fragment shorter than :data:`MIN_PART` characters (an
  ideograph counts as two) or one that is not delimited on both sides,
  because "a" inside "brand" is not a parameter.
* It does not rank by plausibility. Corroboration is a filter, not a score:
  a site supported by two of three recordings is dropped, not down-weighted.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

#: Shortest fragment that may be bound inside a segment, in
#: ``api_compile.match_length`` units (two ideographs count). Below this a
#: match is much more likely to be a coincidence than a parameter.
MIN_PART = 3

#: Characters that may sit next to a fragment inside a path segment. A value
#: bound to part of a segment must be delimited on both sides by one of these
#: (or by the segment boundary), so ``guzzle`` never binds inside ``guzzlehttp``.
DELIMITERS = "-_.,@:~+"


def _slug(value: str) -> str:
    return re.sub(r"\s+", "-", value.strip().lower())


def _hyphen(value: str) -> str:
    return re.sub(r"\s+", "-", value.strip())


#: Deterministic transforms from "what the user typed" to "what the URL carries".
#: Ordered: the earliest that fits is preferred, so a URL that carries the value
#: verbatim is never described as carrying a transformed one.
TRANSFORMS = (
    ("identity", lambda v: v),
    ("lower", lambda v: v.lower()),
    ("hyphen", _hyphen),
    ("slug", _slug),
)
_TRANSFORM_MAP = dict(TRANSFORMS)


def apply_transform(name: str, value: str) -> str:
    """Apply a named transform. An unknown name raises — never silently identity."""
    try:
        return _TRANSFORM_MAP[name](str(value))
    except KeyError:
        raise KeyError(f"unknown parameter transform {name!r}; the whole list is "
                       f"{[n for n, _ in TRANSFORMS]}") from None


class PathBindError(ValueError):
    """A value cannot be put into the path shape the recording proved."""


def bind_path_value(value: str, span: int) -> str:
    """Percent-encode ``value`` as exactly ``span`` path segments, or refuse.

    This is the whole safety story of a multi-segment parameter. The invariant
    is that a bound value contributes **exactly** the number of ``/`` characters
    the recordings proved — no more, no fewer.

    For ``span == 1`` that is what percent-encoding with no safe characters has
    always done and still does: a value containing ``/`` is encoded to ``%2F``
    and stays one segment. Multi-segment support changes nothing here,
    deliberately, so every one-segment template renders byte for byte as it did.

    For ``span > 1`` the value must split into exactly that many non-empty
    segments; each is then encoded with no safe characters and they are joined
    by the ``span - 1`` separators the recording proved. A value of a different
    shape is refused rather than reshaped, and no value can add a query string,
    a fragment or a ``..``.
    """
    raw = str(value)
    if span <= 1:
        if raw in (".", ".."):
            raise PathBindError(f"{raw!r} is a relative path segment")
        return urllib.parse.quote(raw, safe="")
    parts = raw.split("/")
    if len(parts) != span:
        raise PathBindError(
            f"{raw!r} has {len(parts)} path segment(s) but the recording proved "
            f"this parameter spans exactly {span}; refusing rather than reshaping "
            f"the URL")
    for p in parts:
        if not p or p in (".", ".."):
            raise PathBindError(
                f"{raw!r} has an empty or relative path segment ({p!r})")
    return "/".join(urllib.parse.quote(p, safe="") for p in parts)


# ---------------------------------------------------------------------------
# binding sites
# ---------------------------------------------------------------------------

#: Preference order between kinds. A whole query value is the least ambiguous
#: thing a parameter can be; a fragment inside a segment is the most.
_KIND_RANK = {"query": 0, "path_run": 1, "path_part": 2}
_TRANSFORM_RANK = {name: i for i, (name, _) in enumerate(TRANSFORMS)}


@dataclass(frozen=True)
class BindingSite:
    """One place a URL could carry the parameter, and how."""
    where: str                 # "query" | "path_run" | "path_part"
    key: str                   # query key | "i" | "i:j"
    prefix: str = ""           # path_part: literal before the value in that segment
    suffix: str = ""           # path_part: literal after it
    transform: str = "identity"
    span: int = 1              # path_run: how many segments the value occupies

    @property
    def identity(self) -> Tuple:
        return (self.where, self.key, self.prefix, self.suffix, self.transform,
                self.span)

    @property
    def rank(self) -> Tuple:
        return (_KIND_RANK.get(self.where, 9), _TRANSFORM_RANK.get(self.transform, 9),
                -self.span, len(self.prefix) + len(self.suffix), self.key)

    def describe(self) -> str:
        t = "" if self.transform == "identity" else f", after the {self.transform} transform"
        if self.where == "query":
            return f"the whole value of query parameter {self.key!r}{t}"
        if self.where == "path_run":
            if self.span == 1:
                return f"path segment {self.key}{t}"
            a, b = self.key.split(":")
            return f"path segments {a}–{b} ({self.span} of them, joined by '/'){t}"
        return (f"part of path segment {self.key}, between {self.prefix!r} and "
                f"{self.suffix!r}{t}")

    def to_dict(self) -> dict:
        return {"where": self.where, "key": self.key, "prefix": self.prefix,
                "suffix": self.suffix, "transform": self.transform,
                "span": self.span, "describe": self.describe()}


def _segments(url: str) -> List[str]:
    path = urllib.parse.urlsplit(url).path
    return [urllib.parse.unquote(s) for s in path.split("/")]


def _delimited(seg: str, start: int, end: int) -> bool:
    before = seg[start - 1] if start > 0 else ""
    after = seg[end] if end < len(seg) else ""
    return (before == "" or before in DELIMITERS) and (after == "" or after in DELIMITERS)


def sites_for(url: str, value: str, transforms: Sequence[str] = ()) -> List[BindingSite]:
    """Every place ``value`` could be bound in ``url``, best kind first.

    ``transforms`` restricts which transforms may be considered (the caller
    passes the one all recordings agreed on); empty means "try them all".

    Comparison is **exact**, never case-insensitive. A loose compare would make
    ``identity`` and ``lower`` — and ``hyphen`` and ``slug`` — indistinguishable,
    and the transform would then be decided by which one happened to be listed
    first rather than by what the site actually carries. A site whose path is
    case-sensitive would get a URL that 404s, and the whole point of naming the
    transform is that the URL can be rebuilt byte for byte.
    """
    from anyuse.execution.api_compile import match_length
    value = str(value or "")
    if not value.strip():
        return []
    names = [n for n, _ in TRANSFORMS if not transforms or n in transforms]
    out: List[BindingSite] = []
    sp = urllib.parse.urlsplit(url)
    segs = _segments(url)

    for name in names:
        want = apply_transform(name, value)
        if not want:
            continue
        for key, val in urllib.parse.parse_qsl(sp.query, keep_blank_values=True):
            if val == want:
                out.append(BindingSite("query", key, transform=name))
        # runs of whole path segments, longest first; equality means the runs
        # that match are never nested, so all of them are reported
        for i in range(len(segs)):
            for j in range(i, len(segs)):
                run = "/".join(segs[i:j + 1])
                if not run:
                    continue
                if run == want:
                    out.append(BindingSite("path_run", f"{i}:{j}" if j > i else str(i),
                                           transform=name, span=j - i + 1))
                if len(run) > len(want):
                    break
        # a delimited fragment inside ONE segment
        if match_length(want) >= MIN_PART:
            for i, seg in enumerate(segs):
                if not seg or seg == want:
                    continue
                start = seg.find(want)
                while start >= 0:
                    end = start + len(want)
                    if _delimited(seg, start, end):
                        out.append(BindingSite("path_part", str(i),
                                               prefix=seg[:start], suffix=seg[end:],
                                               transform=name))
                        break
                    start = seg.find(want, start + 1)
    seen: Set[Tuple] = set()
    unique = []
    for s in sorted(out, key=lambda s: s.rank):
        if s.identity in seen:
            continue
        seen.add(s.identity)
        unique.append(s)
    return unique


def templated_url(url: str, site: BindingSite, placeholder: str) -> Optional[str]:
    """Rebuild ``url`` with ``placeholder`` standing where ``site`` says the value is.

    Returns ``None`` when the site does not describe this URL (a different
    number of segments, a missing query key), which is how corroboration fails.
    """
    sp = urllib.parse.urlsplit(url)
    segs = _segments(url)
    marker = "{%s}" % placeholder
    if site.where == "query":
        pairs = urllib.parse.parse_qsl(sp.query, keep_blank_values=True)
        if not any(k == site.key for k, _ in pairs):
            return None
        pairs = [(k, marker if k == site.key else v) for k, v in pairs]
        query = "&".join(f"{urllib.parse.quote(k)}="
                         f"{v if v == marker else urllib.parse.quote(v)}"
                         for k, v in pairs)
        path = "/".join(urllib.parse.quote(s, safe="") for s in segs)
        return urllib.parse.urlunsplit((sp.scheme, sp.netloc, path, "", "")) + \
            (f"?{query}" if query else "")

    if site.where == "path_run":
        a, _, b = site.key.partition(":")
        i, j = int(a), int(b or a)
        if j >= len(segs):
            return None
        segs = segs[:i] + [marker] + segs[j + 1:]
    elif site.where == "path_part":
        i = int(site.key)
        if i >= len(segs):
            return None
        seg = segs[i]
        if not (seg.startswith(site.prefix) and seg.endswith(site.suffix)):
            return None
        segs = list(segs)
        segs[i] = site.prefix + marker + site.suffix
    else:
        return None
    path = "/".join(s if marker in s else urllib.parse.quote(s, safe="") for s in segs)
    query = "&".join(f"{urllib.parse.quote(k)}={urllib.parse.quote(v)}"
                     for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True))
    return urllib.parse.urlunsplit((sp.scheme, sp.netloc, path, "", "")) + \
        (f"?{query}" if query else "")


# ---------------------------------------------------------------------------
# evidence across recordings
# ---------------------------------------------------------------------------

@dataclass
class BindingEvidence:
    """What several recordings of DIFFERENT values agree about a parameter.

    ``supported`` holds, for every recording, the set of
    ``(site identity, templated URL)`` pairs that recording supports. A site is
    only usable when it is in the intersection — i.e. when it describes every
    recording, in the same place, leaving the same constant remainder.
    """
    per_recording: List[Set[Tuple]] = field(default_factory=list)
    transform: str = ""
    notes: List[str] = field(default_factory=list)
    n_recordings: int = 0

    @property
    def supported(self) -> Set[Tuple]:
        if len(self.per_recording) < 3:
            return set()
        out = set(self.per_recording[0])
        for other in self.per_recording[1:]:
            out &= other
        return out

    def corroborates(self, url: str, site: BindingSite, placeholder: str = "p") -> bool:
        tmpl = templated_url(url, site, placeholder)
        if tmpl is None:
            return False
        return (site.identity, tmpl) in self.supported

    def to_dict(self) -> dict:
        return {"n_recordings": self.n_recordings, "transform": self.transform,
                "supported": len(self.supported), "notes": list(self.notes)}


def _urls_of(recording: dict) -> List[str]:
    urls = [str(recording.get("final_url") or "")]
    for req in recording.get("network") or []:
        u = str(req.get("url") or "")
        if u.startswith("http"):
            urls.append(u)
    return [u for u in urls if u]


def agreed_transform(recordings: Sequence[dict]) -> Tuple[str, str]:
    """The one transform every recording's URLs agree on, or ``("", reason)``.

    A transform is only allowed when it is the *earliest* one that fits for
    every recording. If two recordings need different transforms the parameter
    is refused with both named: a template that lower-cases one input and not
    another is not deterministic, it is lucky.
    """
    per: List[Set[str]] = []
    for rec in recordings:
        value = str(((rec.get("param") or {}).get("value")) or "")
        if not value.strip():
            continue
        found: Set[str] = set()
        for url in _urls_of(rec):
            for site in sites_for(url, value):
                found.add(site.transform)
        per.append(found)
    per = [p for p in per if p]
    if not per:
        return "", "the typed value appears in no URL of any recording"
    common = set(per[0])
    for p in per[1:]:
        common &= p
    if not common:
        seen = sorted({t for p in per for t in p})
        return "", (f"the recordings disagree about how the typed value reaches the "
                    f"URL ({', '.join(seen) or 'no transform fits'}); a parameter "
                    f"that is transformed differently per input is refused")
    for name, _ in TRANSFORMS:
        if name in common:
            return name, ""
    return "", "no known transform fits every recording"


def binding_evidence(recordings: Sequence[dict],
                     placeholder: str = "p") -> BindingEvidence:
    """Build :class:`BindingEvidence` from recordings of DIFFERENT parameter values."""
    usable = [r for r in recordings
              if str(((r.get("param") or {}).get("value")) or "").strip()]
    distinct = []
    seen_values = set()
    for rec in usable:
        value = str((rec.get("param") or {}).get("value"))
        key = " ".join(value.split()).casefold()
        if key not in seen_values:
            distinct.append(rec)
            seen_values.add(key)
    usable = distinct
    ev = BindingEvidence(n_recordings=len(usable))
    if len(usable) < 3:
        ev.notes.append("fewer than three distinct recordings with a typed value: a binding "
                        "site cannot be corroborated and none is offered")
        return ev
    transform, why = agreed_transform(usable)
    ev.transform = transform
    if not transform:
        ev.notes.append(why)
        return ev
    for rec in usable:
        value = str((rec.get("param") or {}).get("value"))
        here: Set[Tuple] = set()
        for url in _urls_of(rec):
            for site in sites_for(url, value, transforms=(transform,)):
                tmpl = templated_url(url, site, placeholder)
                if tmpl is not None:
                    here.add((site.identity, tmpl))
        ev.per_recording.append(here)
    ev.notes.append(f"{len(ev.supported)} binding site(s) describe all "
                    f"{len(usable)} recordings identically, transform={transform}")
    return ev


def corroborated_sites(url: str, value: str, evidence: Optional[BindingEvidence],
                       placeholder: str = "p") -> Tuple[List[BindingSite], str]:
    """The sites in ``url`` that ``evidence`` corroborates, best first.

    Returns ``([], reason)`` when nothing is corroborated, and the reason names
    what was seen so a reader can tell "no site at all" from "a site only one
    recording supports".
    """
    if evidence is None or evidence.n_recordings < 3:
        return [], ("no three-recording evidence: a path binding requires three "
                    "distinct corroborating recordings")
    if not evidence.transform:
        return [], (evidence.notes[0] if evidence.notes else "no agreed transform")
    proposed = sites_for(url, value, transforms=(evidence.transform,))
    if not proposed:
        return [], "the typed value appears nowhere in this URL"
    ok = [s for s in proposed if evidence.corroborates(url, s, placeholder)]
    if not ok:
        return [], (f"{len(proposed)} candidate site(s) in this URL "
                    f"({'; '.join(s.describe() for s in proposed[:3])}) but none is "
                    f"supported by all {evidence.n_recordings} recordings")
    return ok, ""


def site_params(site: BindingSite, name: str) -> Tuple[Dict[str, int], Dict[str, str]]:
    """``(path_params, param_transforms)`` entries a template needs for ``site``."""
    path_params: Dict[str, int] = {}
    transforms: Dict[str, str] = {}
    if site.where == "path_run" and site.span > 1:
        path_params[name] = site.span
    if site.transform != "identity":
        transforms[name] = site.transform
    return path_params, transforms
