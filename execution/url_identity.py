"""ONE rule for what identifies a link's TARGET: per-session and tracking query parameters are
noise for identity and for forming a result region; navigation always keeps the full URL.

The case: a shop's result links each carry ``?_pos=1&_sid=5e59a2878&_ss=r`` (the result's position and a
session id); the link clusters (``discover.link_clusters``) need ONE varying URL part, the path and ``_pos`` both
varied, so no result region formed and the compile refused a correct demonstration.

The page's own links decide first, and the lists are the last rung:

* **per-item noise on a page** (:func:`page_noise`): a parameter whose value varies from link to link while the links'
  PATHS already tell the targets apart (each carrying path appears with one value only) does not change the target -
  it is noise there. A parameter that is what tells two links of the same path apart (``/item?id=3`` vs ``?id=4``) is
  identity there and is kept, whatever a list says (``PageNoise.identity``). Used where a page's links are clustered
  into a result region.
* **known tracking parameters** (:func:`is_tracking`), where the page says nothing: the click ids of the ad and
  analytics platforms (``utm_*``, ``fbclid``, ``gclid``, ``msclkid``, ``yclid``, ``dclid``, ``srsltid``,
  ``igshid``; :data:`CLICK_IDS`), then the mail, analytics and shop-system tags (``mc_cid``, ``_ga``, ``_hsenc``,
  the search-position and session tags ``_pos``, ``_sid``, ``_ss``, ...; :data:`VENDOR_TAGS`). Dropped from an
  identity key with no page evidence (``web_arch.norm_url``).
"""
from __future__ import annotations

import urllib.parse
from typing import Dict, Iterable, List, Optional, Sequence, Set

#: the click ids the ad and analytics platforms add to a link (with every ``utm_*``)
CLICK_IDS = frozenset({"fbclid", "gclid", "gclsrc", "dclid", "msclkid", "yclid", "igshid", "srsltid"})
#: mail, analytics and shop-system tags: dropped only when the page's own links say nothing about them
VENDOR_TAGS = frozenset({"mc_cid", "mc_eid", "_ga", "_gl", "_hsenc", "_hsmi", "_pos", "_sid", "_ss", "_psq",
                         "_fid", "spm", "scm", "trk", "trkid", "zanpid", "wt_mc", "ito"})
TRACKING = CLICK_IDS | VENDOR_TAGS


class PageNoise(frozenset):
    """The per-item noise parameters of one page's links (a set of names), and ``identity``: the
    parameters that tell two links of one path apart on that page."""

    def __new__(cls, noise: Iterable[str] = (), identity: Iterable[str] = ()):
        obj = super().__new__(cls, noise)
        obj.identity = frozenset(identity)
        return obj


def is_tracking(key: str) -> bool:
    """A known tracking parameter (:data:`CLICK_IDS`, ``utm_*``, :data:`VENDOR_TAGS`): the lists, read
    where no page evidence is at hand."""
    k = str(key or "").lower()
    return k.startswith("utm_") or k in TRACKING


def strip(url: str, noise: Iterable[str] = ()) -> str:
    """The URL without the page's per-item ``noise`` parameters and without its tracking parameters (order of the
    rest kept); anything unparsable unchanged. The page's evidence first: a ``noise`` parameter is dropped, and one
    the page tells targets apart by (``noise.identity``, :func:`page_noise`) is kept even when a list names it;
    every other parameter a list names (:func:`is_tracking`) is dropped, the last rung."""
    s = str(url or "")
    try:
        p = urllib.parse.urlsplit(s)
    except ValueError:
        return s
    if not p.query:
        return s
    drop = {str(n).lower() for n in noise}
    keep = {str(n).lower() for n in getattr(noise, "identity", ())}
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    kept = [(k, v) for k, v in q
            if k.lower() not in drop and (k.lower() in keep or not is_tracking(k))]
    if len(kept) == len(q):
        return s
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(kept), p.fragment))


def page_noise(urls: Sequence[str], min_links: int = 2) -> PageNoise:
    """The query parameters that are per-item noise among ``urls`` (one page's links): carried by >= ``min_links``
    links of one host, with varying values, while every carrying path appears with ONE value only (the path already
    tells the targets apart). ``identity``: the parameters that tell links of one path apart: all their links share
    one path, or two or more of their paths, and at least half of them, hold two or more values each."""
    by: Dict[tuple, Dict[str, Set[str]]] = {}        # (host, key) -> path -> values
    for u in urls:
        try:
            p = urllib.parse.urlsplit(str(u or ""))
        except ValueError:
            continue
        if not p.query or p.scheme not in ("http", "https"):
            continue
        for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True):
            by.setdefault(((p.hostname or "").lower(), k), {}).setdefault(p.path, set()).add(v)
    noise: Set[str] = set()
    identity: Set[str] = set()
    for (_host, k), paths in by.items():
        # the paths alone cannot tell these links apart: one path holds them all, or two or more of the
        # carrying paths, and at least half, hold two or more values (one product listed twice, at two
        # positions, is not that, however short the list)
        multi = sum(1 for vs in paths.values() if len(vs) > 1)
        if multi and (len(paths) == 1 or (multi >= 2 and multi * 2 >= len(paths))):
            identity.add(k)
        if len(paths) < min_links:
            continue
        values = set().union(*paths.values())
        if len(values) > 1 and all(len(vs) == 1 for vs in paths.values()):
            noise.add(k)
    return PageNoise(noise, identity - noise)


def path_companions(urls: Sequence[str], min_links: int = 2) -> Set[str]:
    """Query parameters that vary per item TOGETHER WITH the path (an article number: each product's path differs,
    and a product's variants share a path with different article numbers): carried
    by >= ``min_links`` links of one host with >= 2 different paths and >= 2 different values. They stay part of each
    target's identity (unlike :func:`page_noise`, a path may carry several values), but a result region may vary in the
    path PLUS such a value: ``discover.link_clusters`` forms a path cluster for those links (``param`` "item+query")."""
    by: Dict[tuple, Dict[str, Set[str]]] = {}
    for u in urls:
        try:
            p = urllib.parse.urlsplit(str(u or ""))
        except ValueError:
            continue
        if not p.query or p.scheme not in ("http", "https"):
            continue
        for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True):
            by.setdefault(((p.hostname or "").lower(), k), {}).setdefault(p.path, set()).add(v)
    out: Set[str] = set()
    for (_host, k), paths in by.items():
        if len(paths) >= min_links and len(set().union(*paths.values())) >= 2:
            out.add(k)
    return out


def path_only(url: str) -> str:
    """The URL without its query and fragment (a path cluster's member)."""
    try:
        p = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return str(url or "")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def identity(url: str, noise: Iterable[str] = ()) -> str:
    """The identity key of a link's target: :func:`strip`, then the form of ``web_arch.norm_url`` (scheme, lower-case
    host, path without a trailing slash, the kept query; no fragment). The page's ``noise`` evidence is kept: the key
    is not stripped a second time without it."""
    s = strip(str(url or "").split("#", 1)[0], noise)
    try:
        p = urllib.parse.urlsplit(s)
    except ValueError:
        return s
    return urllib.parse.urlunsplit((p.scheme, (p.hostname or "").lower(), p.path.rstrip("/") or "/", p.query, ""))


#: The result URL shape (a news search: each result card also links its category, /news/category/<id>, beside the
#: article, /news/<id>/<slug>; a blog search: the site's home page "/" listed among the /<slug>/ articles): the results of one result region share a URL SHAPE - the form of the segment that
#: names each result, the path's LAST segment: a WORD (it holds a letter: a slug, a name, ``x.html``) or a NUMBER (no
#: letter: an id, a date) - or no segment at all (the site's ``root``). A link of another shape than the region's
#: majority (more than half of its distinct targets) is not a result, whatever its repeat count; no majority: nothing
#: is decided by shape. No word list. Measured on every saved list answer:
#: the segment count, the constant segments, the file extension or the query's keys as part of the shape mark
#: results of another section of the same site (``/news/x`` beside ``/news/rumors/y``) as no results; this form marked
#: none. The verifier reads the same rule with its own code (``attribute_verify.SHAPE_CHECK``).
SHAPE_RULE = True


def url_shape(url: str) -> str:
    """``root`` | ``number`` | ``word``: the form of the URL's last path segment (see ``SHAPE_RULE``)."""
    try:
        path = urllib.parse.urlsplit(str(url or "")).path or ""
    except ValueError:
        return "word"
    segs = [urllib.parse.unquote(x) for x in path.split("/") if x]
    if not segs:
        return "root"
    return "word" if any(c.isalpha() for c in segs[-1]) else "number"


def majority_shape(urls: Sequence[str]) -> str:
    """The shape of more than half of ``urls``' distinct targets (``identity``); on a tie, of more than half of the
    links themselves (``urls`` as given, one entry per link: a result card links its result from its image and its
    title, its category once - five results with five different categories); "" when neither holds a majority or the
    rule is off."""
    if not SHAPE_RULE:
        return ""
    links = [identity(u) for u in urls if u]
    from collections import Counter
    for pop in (list(dict.fromkeys(links)), links):
        if not pop:
            return ""
        sh, n = Counter(url_shape(t) for t in pop).most_common(1)[0]
        if n * 2 > len(pop):
            return sh
    return ""


def link_counts(hrefs: Iterable[str]) -> Dict[str, int]:
    """identity -> how many of ``hrefs`` (one page's links) lead to it."""
    out: Dict[str, int] = {}
    for h in hrefs:
        if h:
            k = identity(h)
            out[k] = out.get(k, 0) + 1
    return out


def off_shape(urls: Sequence[str], keep: int = 2, counts: Optional[Dict[str, int]] = None) -> List[int]:
    """The positions in ``urls`` (a list's results) whose shape is not the majority's (``majority_shape``; ``counts``:
    the page's links per target, ``link_counts`` - each result weighs that many links on a tie) - [] when there is no
    majority, or when fewer than ``keep`` results would remain (a list is never cut below that by shape). An empty URL
    is never off."""
    pop: List[str] = []
    for u in dict.fromkeys(u for u in urls if u):
        pop.extend([u] * max(1, int((counts or {}).get(identity(u), 1))))
    sh = majority_shape(pop)
    if not sh:
        return []
    off = [k for k, u in enumerate(urls) if u and url_shape(u) != sh]
    return off if len(urls) - len(off) >= keep else []


__all__ = ["TRACKING", "CLICK_IDS", "VENDOR_TAGS", "PageNoise", "is_tracking", "strip", "page_noise", "path_companions", "path_only", "identity", "SHAPE_RULE",
           "url_shape", "majority_shape", "link_counts", "off_shape"]
