"""Shareable projection of an app map — what may leave this machine, and why.

An anyuse ``AppGuide`` is a private recording of one operator's screen. This
module builds the small, structural subset of it that is meaningful to a
DIFFERENT tenant running the same application, and it is built as an ALLOW-LIST:
every field in the output is named here on purpose. Nothing is copied wholesale,
so a field added to ``AppGuide`` tomorrow does not silently start travelling.

THE RULE
--------
    A locator whose text is STRUCTURAL travels. A locator whose text is CONTENT
    does not.

An OCR anchor on "Mueller, Anna" is worthless to another tenant — that string is
never on their screen — and it is a leak. Static app chrome ("Speichern",
"Buchen", field labels) is exactly what transfers, so excluding content-derived
locators costs no capability.

Structural vs content is MEASURED, never guessed from string shape:
``guides.locator_census.LocatorCensus`` records which texts occupied which
structural slot across sessions. A locator that recurred unchanged is chrome;
one whose slot varied is content; one that was never measured is UNSURE.

    UNSURE DOES NOT TRAVEL. Under-sharing is recoverable by observing another
    session. A leaked customer name in a shared map is not.

THE FINGERPRINT LEAK — LOUD, AND NOT "SANITISED"
------------------------------------------------
``UIFingerprint`` is only half structural. ``structural_hash`` and
``role_frequency`` are role-only and clean. But ``required_elements`` sitting
next to them is ``[{"role", "title"}]`` with RAW AX TITLES copied straight off
the screen (``memory/recorder.py`` ``_build_fingerprint_from_snapshot``,
``guides/generator.py`` ``_build_state_from_elements``). On a data grid those
titles ARE the row contents. Anything that ships "the fingerprint" wholesale
ships screen text believing it shipped a hash.

This projection never emits ``required_elements``. And when any title inside it
fails to measure as chrome, the WHOLE STATE IS DROPPED — the leak is in the
identity of the node, so the node does not travel. It is not stripped and
shipped: a fingerprint minus its content is a different fingerprint, and one we
never actually matched anything against. Every drop is reported under
``report.leaks`` with the slot, the verdict and the counts — never the text.

WHAT NEVER TRAVELS (see :data:`NEVER_TRAVELS`)
---------------------------------------------
Screenshot and pixel bytes; ActivityRecord prose; ``full_guide`` markdown;
``short_description``; ``use_cases``; ``ui_landmarks``; ``navigation_tips``;
``common_tasks``; ``successful_patterns``; ``known_issues``;
``url_patterns``/``title_patterns`` (literal URLs and window titles despite the
name); ``domain`` (a website guide's host — the same URL under another name, see
APP IDENTITY below); ``custom_render_notes``; ``UIElement.ax_path`` (a Selector whose name
falls back to the field's AXVALUE — typed contents); ``UIElement.js_selector``
and ``description``; ``TransitionImpl.command`` (embeds ax paths, JS source and
free-text descriptions); timestamps; ``linked_workflows``; ``common_shortcuts``
(its keys are LLM-written prose, and shortcuts are re-derivable from vendor
docs — a deliberate omission, not a leak finding).

APP IDENTITY IS PROVENANCE, NOT A STRING
----------------------------------------
The map has to say WHICH app it describes, and that identity is the one thing the
census cannot measure: it is not a locator sitting in a structural slot, it is the
name of the scope every slot is measured under. It was therefore exempted, on the
belief that "the app's name is what the app IS, never observed content".

That belief was FALSE for one of the two ways anyuse derives a name.
``guides/generator._get_key(url=...)`` reduces a visited URL to its host and hands
that host to ``AppGuide.create_website_guide(name=key, domain=key)``. On the
everyday SaaS form — ``mueller-anna-gmbh.myshopify.com``,
``kunde.billing.vendor.example`` — the host IS the customer. So a website guide's
name and domain are a URL under another name, and the module already says a URL
never travels: ``url_patterns``/``title_patterns`` are in :data:`NEVER_TRAVELS`.

    THE CONTRADICTION IS RESOLVED TOWARDS ``NEVER_TRAVELS``: nothing derived from
    a visited URL travels, the app identity included.

Identity is therefore decided by PROVENANCE — where the name came from — and never
by looking at the string. No hostname regex, no "does this look like a company"
heuristic: the whole point is that content-vs-chrome is measured or
refused, and guessing at a string would be the same mistake in a new costume.

    ``os_app_name``   the OS-reported application name of a desktop app
                      (``_get_key(app=...)``, ``create_app_guide``). Set at build
                      time by the vendor, identical on every tenant's machine.
                      TRAVELS.

    ``url_host``      a website guide's name/domain. Derived from a visited URL,
                      may be tenant-scoped, and nothing short of inspecting it
                      would say which. DOES NOT TRAVEL.

PROVENANCE IS A RECORDED FACT, NOT A LATER READING OF THE STRING
----------------------------------------------------------------
The first version of this rule asked the guide what KIND it was and inferred the
provenance from that — website (or any guide carrying a ``domain``) meant
``url_host``, a plain application guide meant ``os_app_name``. The inference was
the hole, because the thing that decides the kind was itself a string test:

    ``generator._get_key(url="http://angebote-mueller-gmbh/erp/kunde/4711")``
        -> ``"angebote-mueller-gmbh"``            the netloc of a VISITED URL
    ``generator._create_guide``  ``"." in key``   -> False, an INTRANET host has
        no dot, so the URL's host was created as an APPLICATION guide with no
        ``domain`` -> provenance read back as ``os_app_name`` -> the tenant
        travelled. Intranet hosts are the normal case in the enterprises that
        run an on-prem ERP behind a flat name.

So the provenance is now RECORDED WHERE IT IS KNOWN — at the argument the key
came in on (``generator._get_key(url=...)`` is ``url_host``, permanently,
whatever the host looks like) — persisted on the guide as
``AppGuide.identity_provenance``, and only ever READ here. No dot test, no
smarter dot test: every string test has another counterexample, and the intranet
host is the counterexample to the one that shipped.

    A guide with NO recorded provenance (every guide file written before the
    field existed) is ``unrecorded`` and DOES NOT TRAVEL. It is not inferred back
    from the name — inferring it back is the bug.

When the identity cannot be established as non-tenant-scoped, the map does not
travel under it AT ALL: ``project_app_guide`` returns ``{"map": None}`` with the
refusal in ``report["identity"]``. It is not anonymised — a hash of the tenant
host is still a stable tenant correlator, and a map whose app nobody can name is
worth nothing to a receiver anyway. Website maps come back when anyuse can record
a VENDOR identity that a visited URL did not produce; until then this under-shares
on purpose, and under-sharing is recoverable.

The same rule applies inside the map: ``UIState.app`` used to be copied onto every
projected state, which put the identity string on the wire once per screen. States
now carry no app name, and a state whose recorded app is not the guide's own app is
dropped — a foreign scope is an identity we never established.

THE TOKEN ATTESTS ONE STRING, SO THE MAP CARRIES ONE
-----------------------------------------------------
``identity_provenance`` records where THE NAME came from. It says nothing about
any other string in the identity block, and the map used to carry a second one:
``bundle_id``, free text, vendor-set in the honest case and unestablished in every
other. The transport type-checked it as a name — which proves it is a bounded
string and nothing about its origin — and promoted it into the wire's app name
whenever ``name`` was empty. A content string crossed with an honest stamp beside
it.

    A SECOND IDENTIFIER NOBODY ATTESTED BUYS NOTHING AND COSTS A LEAK.

The receiver identifies the map by the attested name and dereferences it by the
handle derived from that name; the bundle id was never read for either. It is gone
from the projection, and ``"bundle_id"`` is in :data:`NEVER_TRAVELS` so the same
walk that pins ``domain`` and ``ax_path`` pins it too.

``aliases`` once survived on the argument that an alias is the same
OS-reported name under another spelling ("Chrome" / "Google Chrome"), so the
token that covers the name covers it — one derivation, one verdict. That argument
fails on a synthesised name: it assumes the name the token was recorded over was the OS's.
``guide.app_aliases`` is therefore no longer read at all, and the map's ``aliases``
come out of the curated file with the name (see IDENTITY IS RECOGNISED below).

A TRUTHFUL STAMP OVER A SYNTHESISED NAME — WHY THE VERSION MOVED AGAIN
----------------------------------------------------------------------
Every defence above asks WHERE THE NAME CAME FROM and trusts the answer. The
answer was honest and the name was still the customer, because the thing that
took the name synthesised it:

    The removed caption watcher's context-change detector (before map version 3)
        ``if current_app.lower() in ("electron", "helper", "node"):``
        ``    display_app = f"{current_app} ({window_title.split(' - ')[0][:30]})"``

which travels ``apps_used`` -> ``_get_key(app=...)`` -> ``os_app_name``. The OS
really did report that process, the record really was made at the ``app=``
argument, and "Electron (Angebot Mueller GmbH)" crossed with nothing lying
anywhere. VS Code, Slack, Teams and Notion are all Electron, so this was the
ordinary case.

    THE PROVENANCE OF A NAME IS ONLY AS GOOD AS THE NAME THE OS WAS ASKED FOR.

Fixed where it happened: the identity keeps the vendor-set process name, the
window title stays CONTENT in its own field, and ``ContextChange`` records how
its app name was derived so the only read that can produce a guide key
(``ContextChange.os_app_name``) refuses a name that was not the OS's. Nothing
here re-derives that from the string — a synthesised name is unrecognisable by
inspection, and re-deriving from shape is the same mistake in a new costume.

Which leaves the maps ALREADY BUILT. A version-2 ``os_app_name`` stamp is
byte-identical to a version-3 one and was recorded just as honestly over a name
that may be a window title. There is nothing in such a map to detect, so
:data:`PROJECTION_VERSION` moves to 3 and the transport's floor moves with it —
the same and only instrument that separated a recorded version-2 stamp from an
inferred version-1 one.

IDENTITY IS RECOGNISED, NOT DERIVED
-----------------------------------
Everything above asks ONE question — where did this name come from — and every
answer it got was true. Twice the answer was true and the name was the customer
anyway, through two different producers:

    -   the WATCHER synthesised an OS app name out of the focused window's title
        for generic process names: ``"Electron (Angebot Mueller GmbH)"``.
    -   the MODEL reported a screen as the application:
        ``{"type": "CONTEXT_CHANGE", "app": "Angebot Mueller GmbH - Kalkulation"}``.

Both were fixed at their producer. Neither fix says anything about the third
producer, and there is no reason to believe there isn't one.

    ANYTHING DERIVED FROM A RUNNING MACHINE — a process name, a window title, a
    model's observation — CAN BE CUSTOMER CONTENT, and asking where it came from
    cannot separate the two: the strings are identical and the provenance is
    honest in both cases.

    SO THE IDENTITY IS NO LONGER DERIVED. IT IS MATCHED.

A map travels only when the observed application name MATCHES an entry in a
curated list of known vendor software (``vendor_applications.json``, beside this
file). The match is EXACT against a recorded identity token, after ONE
normalisation — ``strip()`` then ``casefold()`` — applied identically to both
sides and to nothing else. No substring, no prefix, no "starts with a listed
name", no token overlap, no fuzz:

    ``"Electron (Angebot Mueller GmbH)"`` does not match ``"Electron"`` — and
    would not match it if ``Electron`` were on the list. A NEAR-MATCH IS A
    NON-MATCH.

    NO MATCH -> THE MAP DOES NOT TRAVEL. Not anonymised, not hashed, not partly
    shared: a hash of a tenant identity is still a stable tenant correlator,
    and a map whose application nobody can name is worth nothing to a receiver.

THE NAME THAT TRAVELS IS THE FILE'S, NOT THE MACHINE'S
-------------------------------------------------------
The match SELECTS an entry; that entry's own ``application`` string is what the
map carries and what ``ref`` is slugified from. The observed name is never copied
onto the map — it stays here as ``local_scope``, the key the census was measured
under and the states were recorded against. So the one string this projection
puts on a wire verbatim now comes out of an operator's file, and there is NO
INPUT that makes it come out of a screen.

It also merges what the machines disagree about: "SAP Logon" and "SAP GUI" are
the same screens under two OS names, so both project as ``SAP GUI`` under one
handle — which is the entire point of sharing structure between tenants.

``aliases`` follows the name for the same reason. They are the entry's OTHER
identity tokens, read out of the file; ``guide.app_aliases`` is machine-derived
text that nothing attested (see THE TOKEN ATTESTS ONE STRING) and it is in
:data:`NEVER_TRAVELS` with the rest. The identity block now holds no observed
string at all — two file constants and two literals.

THE LIST IS A LAYER, NOT A REPLACEMENT
----------------------------------------
It gates the IDENTITY and only that. WHICH LABELS travel is still decided by the
measured census, the whole-state drop on a content-bearing fingerprint and the
typed count guard; the transport still checks the provenance token, the version
floor and its own copy of this list. A map passes all of them or it does not go.

WHERE THE LIST LIVES
--------------------
``anyuse/guides/vendor_applications.json`` — a data file on disk, beside this
module, overridable by the ``ANYUSE_VENDOR_APPLICATIONS`` environment variable so
a deployment can point at a longer one. Read-only here, and re-read when its
mtime or size changes.

It is deliberately NOT the guide, NOT the map, NOT app_settings and NOT anything
a watched machine writes: an observed application cannot add itself to the list
of applications we recognise. The two things that can change it are an operator
with a text editor and an operator with an environment.

    A LIST THAT CANNOT BE READ RECOGNISES NOTHING, and nothing travels. A missing
    file under-shares, which is recoverable; a missing file that defaulted to
    "allow everything" would be the original leak with an apology.

OUTPUT
------
``project_app_guide`` returns ``{"map": ..., "report": ...}``.

    THE TRANSPORT SENDS ``result["map"]`` AND NOTHING ELSE.

``map`` is None when the app identity was refused — there is nothing to send, and
``report["identity"]`` says why.

``report`` is local-only diagnostics: what was withheld and why. It carries local
state ids, which ``map`` never does.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from anyuse.guides.locator_census import (
    DEFAULT_MIN_SESSIONS,
    KIND_AX_IDENTIFIER,
    KIND_AX_TITLE,
    KIND_INTENT,
    KIND_OCR,
    LocatorCensus,
    Verdict,
    _attr,
    element_slots,
    ocr_slot,
    state_ref_of,
)

#: 2 — the provenance token became a RECORDED fact. A version-1 map carries the
#: same ``identity_provenance`` key filled in by INFERENCE from the guide's kind,
#: so a version-1 ``os_app_name`` is not evidence of anything: an intranet host passed
#: that inference. The transport uses this as a floor, which is the only way a
#: receiver can tell a recorded stamp from a derived one — the strings are
#: identical and the host is unrecognisable by inspection.
#:
#: 3 — the name the record was made ABOUT became the OS's own. Before version 3 the
#: watcher synthesised an app name out of a window title for generic process
#: names (Electron and everything built on it), and that name then collected a
#: perfectly honest ``os_app_name`` record. A version-2 stamp is therefore evidence that
#: a record was made, and no evidence about what it was made over. Same floor,
#: same reason as version 1: there is nothing in the string to look at.
#:
#: 4 — the identity stopped being DERIVED at all. A version-3 map's ``app.name`` is
#: whatever the machine reported and its ``aliases`` came off the guide: two
#: machine-derived strings sitting in a body a receiver DEREFERENCES. On a version-4 map
#: both come out of the curated vendor file, selected by an exact match. The
#: stamps are byte-identical for the fourth time and there is still nothing in the
#: string to look at, so the version is again the only discriminator.
PROJECTION_VERSION = 4
PROJECTION_KIND = "anyuse_app_map"

#: How ``map["app"]["name"]`` was derived. Stamped on the guide by whoever
#: derived the name (``guides/generator``), read here, and carried on the map so
#: the transport can refuse an identity whose provenance was never established
#: instead of trusting that whoever built the map got it right.
IDENTITY_OS_APP_NAME = "os_app_name"     # desktop app name, vendor-set
IDENTITY_URL_HOST = "url_host"           # website guide name/domain, URL-derived
IDENTITY_NONE = "none"                   # no name at all
IDENTITY_UNRECORDED = "unrecorded"       # nobody recorded where the name came from

#: The only two tokens that mean anything was actually recorded. Anything else —
#: absent, empty, a value some other writer invented — is ``unrecorded``.
RECORDED_IDENTITY_PROVENANCE = frozenset({IDENTITY_OS_APP_NAME, IDENTITY_URL_HOST})

#: The curated list of applications we RECOGNISE — data, not code, so extending it
#: is an edit to a file rather than a release. See IDENTITY IS RECOGNISED above.
#: The environment variable is the only override, because the environment is the
#: operator's and a watched machine's screens are not.
VENDOR_LIST_ENV = "ANYUSE_VENDOR_APPLICATIONS"
DEFAULT_VENDOR_LIST = Path(__file__).resolve().with_name("vendor_applications.json")

#: Documentation + the assertion target of ``test_guide_projection``. These names
#: must never appear as keys anywhere in ``result["map"]``.
NEVER_TRAVELS: Tuple[str, ...] = (
    # observed content on the guide
    "full_guide", "short_description", "use_cases", "ui_landmarks",
    "navigation_tips", "common_tasks", "successful_patterns", "known_issues",
    "user_activities", "agent_activities", "linked_workflows",
    "common_shortcuts",
    # observed content on a state
    "required_elements", "url_patterns", "title_patterns",
    "custom_render_notes",
    # identity taken from a visited URL: the host may be the customer
    # (mueller-anna-gmbh.vendor.example). Same rule as url_patterns above.
    "domain",
    # a SECOND identity string that the provenance token never covered. Nothing
    # downstream reads it, and free text on a map with no attestation behind it
    # is a leak. See THE TOKEN ATTESTS ONE STRING above.
    "bundle_id",
    # the guide's own alternate spellings: machine-derived text, and a
    # machine-derived string is a candidate leak whatever stamp sits beside it.
    # The map's "aliases" are the matched vendor entry's other identity tokens,
    # read out of the curated file — see THE NAME THAT TRAVELS IS THE FILE'S.
    "app_aliases",
    # locators that embed values by construction
    "ax_path", "js_selector", "description",
    # execution payloads: ax paths, JS source, free-text computer-use prompts
    "command", "implementations",
    # pixel/byte stores
    "screenshot", "visual_anchor", "anchors", "filename", "image", "png",
    # user-activity telemetry
    "created_at", "updated_at", "last_seen", "last_executed",
    "last_failure_reason", "timestamp",
)

_JSON_SCALARS = (str, int, float, bool)


class ProjectionLeak(RuntimeError):
    """Raised when a value reaching the map is not what the map declares it is.

    Two shapes of that. A value that is not a JSON scalar — screenshot bytes, a
    PIL image, a ``Selector`` — none of which the allow-list below can produce,
    so it firing means the builder has a bug. And a value of the wrong DECLARED
    type: a counter that is a string, a name that is a dict. That second one is
    not cosmetic. ``map["completeness"]`` is read downstream as numbers and one
    of those numbers reached a summary sent off the machine, so a "count" that is
    prose is a leak with a number's name on it.

    Deliberately fatal rather than filtered — a leak guard that silently drops
    things teaches nobody, and one that ``str()``s them teaches the wrong thing.
    """


def _require_count(name: str, value: Any) -> int:
    """A count is a plain non-negative int. Nothing else, and no coercion.

    ``bool`` is an ``int`` in Python and is not a count. A string of digits is not
    a count either: the moment one of these is interpolated into prose, whatever
    it actually held is prose too.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProjectionLeak(
            f"{name} must be a non-negative int, got {type(value).__name__}")
    return value


def _require_text_or_none(name: str, value: Any) -> Optional[str]:
    """A declared string, or nothing. ``str(value)`` is how a dict becomes a name."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ProjectionLeak(
            f"{name} must be a string, got {type(value).__name__}")
    return value


def _assert_shareable(node: Any, path: str = "$") -> None:
    """Every leaf must be a JSON scalar or None. No bytes. No objects. Ever."""
    if node is None or isinstance(node, _JSON_SCALARS):
        return
    if isinstance(node, (bytes, bytearray, memoryview)):
        raise ProjectionLeak(f"binary value at {path} ({len(bytes(node))} bytes)")
    if isinstance(node, Mapping):
        for k, v in node.items():
            if not isinstance(k, str):
                raise ProjectionLeak(f"non-string key at {path}: {type(k).__name__}")
            _assert_shareable(v, f"{path}.{k}")
        return
    if isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            _assert_shareable(v, f"{path}[{i}]")
        return
    raise ProjectionLeak(f"unshareable {type(node).__name__} at {path}")


def _success_rate(used: int, ok: int) -> float:
    return round(ok / used, 4) if used else 0.0


def _app_slug(name: str) -> str:
    """Stable, url-safe app identity for the map's ``ref``."""
    slug = "".join(c if c.isalnum() else "_" for c in str(name or "").lower())
    return "_".join(p for p in slug.split("_") if p) or "unknown"


# ── the curated vendor list ─────────────────────────────────────────────────
class VendorListError(RuntimeError):
    """The list of applications we recognise could not be read, or is not a list.

    Never a reason to fall back to "recognise everything": raised where it can be
    turned into a REFUSAL with a reason attached, so a deployment with a broken
    file shares nothing and says why, instead of sharing everything quietly.
    """


#: (resolved path, mtime_ns, size) -> {folded token: entry}. Keyed on the stat so
#: an operator's edit is picked up without a restart, and so a test pointing the
#: environment variable at another file is not served the first one's answers.
_VENDOR_CACHE: Dict[Tuple[str, int, int], Dict[str, Dict[str, Any]]] = {}


def _fold(raw: Any) -> str:
    """THE ONE NORMALISATION, applied to the file's tokens and to the observed
    name identically: surrounding whitespace off, case folded. Nothing else.

    Not a place to be clever. Every extra transformation here — stripping
    punctuation, dropping a parenthesised tail, collapsing runs — is a way for a
    decorated tenant string to land on a vendor's entry, which is precisely the
    synthesised-name shape ("Electron (Angebot Mueller GmbH)"). Under-matching costs a map
    nobody shared; over-matching costs the customer.
    """
    return str(raw or "").strip().casefold()


def vendor_list_path() -> Path:
    """Where the list is read from: the environment's file, or the shipped one."""
    override = str(os.environ.get(VENDOR_LIST_ENV, "") or "").strip()
    return Path(override) if override else DEFAULT_VENDOR_LIST


def load_vendor_applications(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Read the curated list into ``{folded identity token: entry}``.

    Every entry is checked, because a curated list is a CLAIM and a malformed
    claim is worth no more than a derived name:

      * ``vendor`` and ``application`` are named — an entry whose vendor nobody
        wrote down is not curated, it is a wish;
      * ``application`` must itself appear in ``identity``, so the file cannot
        canonicalise a match onto a string no operating system ever reports;
      * a folded token may appear ONCE in the whole file. Two entries claiming
        one token is an ambiguous list, and resolving the ambiguity by taking the
        first is how a list quietly starts meaning something else.

    Raises :class:`VendorListError` for all of it. The caller turns that into a
    refusal; nothing here decides to recognise something instead.
    """
    target = Path(path) if path is not None else vendor_list_path()
    try:
        stat = target.stat()
        key = (str(target), stat.st_mtime_ns, stat.st_size)
        cached = _VENDOR_CACHE.get(key)
        if cached is not None:
            return cached
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise VendorListError(f"{target}: {type(exc).__name__}") from exc

    if not isinstance(raw, Mapping):
        raise VendorListError(f"{target}: not a JSON object")
    entries = raw.get("applications")
    if not isinstance(entries, list) or not entries:
        raise VendorListError(f"{target}: no applications listed")

    def _text(where: str, value: Any) -> str:
        # Explicitly, rather than through _require_text_or_none: a bad FILE is a
        # refusal with a reason, not a ProjectionLeak out of the top of the
        # projection. Different fault, different exit.
        if not isinstance(value, str) or not value.strip():
            raise VendorListError(f"{where}: expected a non-empty string")
        return value.strip()

    table: Dict[str, Dict[str, Any]] = {}
    for i, entry in enumerate(entries):
        where = f"{target}: applications[{i}]"
        if not isinstance(entry, Mapping):
            raise VendorListError(f"{where}: not an object")
        application = _text(f"{where}.application", entry.get("application"))
        _text(f"{where}.vendor", entry.get("vendor"))
        tokens_raw = entry.get("identity")
        if not isinstance(tokens_raw, list) or not tokens_raw:
            raise VendorListError(f"{where}: no identity tokens")
        tokens = [_text(f"{where}.identity[{j}]", token)
                  for j, token in enumerate(tokens_raw)]
        if _fold(application) not in {_fold(t) for t in tokens}:
            raise VendorListError(
                f"{where}: the application name is not one of its identity tokens")
        record = {"application": application,
                  "aliases": tuple(t for t in tokens if _fold(t) != _fold(application))}
        for text in tokens:
            folded = _fold(text)
            if folded in table:
                raise VendorListError(
                    f"{where}: identity token claimed by two applications")
            table[folded] = record

    _VENDOR_CACHE[key] = table
    return table


def recognised_vendor(name: Any) -> Optional[Dict[str, Any]]:
    """The match. ``{"application", "aliases"}`` for a listed name, else None.

    EXACT, after :func:`_fold` and nothing else. ``in``, ``startswith`` and every
    similarity measure are absent on purpose: a name that merely CONTAINS a vendor's
    is a name with something else in it, and the something else is the leak.

    A COPY comes back, so a caller cannot edit the cached list through the record
    it was handed. "Nothing observed can write the list" is meant structurally.
    """
    entry = load_vendor_applications().get(_fold(name))
    return dict(entry) if entry is not None else None


# ── app identity ────────────────────────────────────────────────────────────
def _guide_type(guide: Any) -> str:
    """The guide's kind as a plain token. ``GuideType.WEBSITE`` -> ``"website"``."""
    raw = _attr(guide, "guide_type", None)
    return str(getattr(raw, "value", raw) or "").strip().lower()


def _app_identity(
    guide: Any,
) -> Tuple[Optional[Dict[str, Any]], str, Dict[str, Any]]:
    """RECOGNISE the app against the curated list, or refuse it.

    Returns ``(identity, local_scope, verdict)``.

    ``identity`` is what the map carries and is built ENTIRELY out of the matched
    vendor entry plus two literals — no observed string reaches it. It is None
    when the map does not travel at all.

    ``local_scope`` is the OBSERVED name and never leaves this process: the census
    was measured under it and the guide's states are recorded against it, so it is
    what the rest of the projection compares. Two different machines reporting
    "SAP Logon" and "SAP GUI" therefore keep their own local scopes and still
    project one shared identity.

    Two gates, in order, and both must hold.

    PROVENANCE — the string is never examined and the guide's KIND is
    never used to work out where the name came from: the kind is itself the output
    of a string test (``generator._create_guide``), and an intranet host passes
    that test as an application. Only ``guide.identity_provenance``, recorded at
    the moment the name was derived, decides. Absent means absent.

    RECOGNITION — and then the name has to BE a vendor application we
    listed, because an honest provenance over a synthesised name is exactly what
    two producers already shipped. Exact match, no fallback, no near-miss.
    """
    # Declared type, not str() — the name is the ONE string this map puts on a
    # wire verbatim, and stringifying a dict into it is the same defect as
    # stringifying a counter into prose.
    name = (_require_text_or_none("guide.name", _attr(guide, "name", None))
            or "").strip()
    domain = _attr(guide, "domain", None)
    gtype = _guide_type(guide)
    recorded = _require_text_or_none(
        "guide.identity_provenance", _attr(guide, "identity_provenance", None))
    recorded = (recorded or "").strip()

    if not name:
        return None, name, {"established": False, "recognised": False,
                            "provenance": IDENTITY_NONE,
                            "reason": "the guide has no app name"}
    if recorded not in RECORDED_IDENTITY_PROVENANCE:
        # A guide written before the provenance was recorded, or by a writer that
        # does not record it. Where its name came from is simply not known, and
        # reading it back off the name is exactly the refuted heuristic.
        return None, name, {
            "established": False, "recognised": False,
            "provenance": IDENTITY_UNRECORDED,
            "reason": ("nobody recorded where this app name came from, so it "
                       "cannot be shown to be free of the tenant"),
        }
    if recorded != IDENTITY_OS_APP_NAME or domain or gtype != "application":
        # Either the name IS a visited URL's host, or the record says one thing
        # and the guide's own shape says another (a domain, or a website kind,
        # can only have come from a URL). A contradiction is not an established
        # provenance; refuse the pair rather than keep the half that looks safer.
        return None, name, {
            "established": False, "recognised": False,
            "provenance": IDENTITY_URL_HOST,
            "reason": ("the identity is a host taken from a visited URL and may "
                       "be tenant-scoped; a URL never travels, this one included"),
        }

    # And now the recognition gate. Everything above is satisfied by a name the watcher
    # built out of a window title, or one the model read off a screen: the record
    # is honest, the process really was reported, and the string is the customer.
    # So the name has to be one we RECOGNISE.
    try:
        entry = recognised_vendor(name)
    except VendorListError as exc:
        # A broken or missing list recognises NOTHING. The reason is on the local
        # report so an operator can fix the file, rather than a silent all-clear.
        return None, name, {
            "established": False, "recognised": False,
            "provenance": IDENTITY_OS_APP_NAME,
            "reason": (f"the vendor application list could not be read ({exc}), "
                       "so no application is recognised and none travels"),
        }
    if entry is None:
        # Not on the list. That covers a genuinely unlisted vendor app (fix: add
        # it) and every synthesised, decorated or model-invented name there will
        # ever be (fix: none — that is the point). The refusal does not quote the
        # name: this report is local, but a reason that carries the string is a
        # string looking for somewhere to be logged.
        return None, name, {
            "established": False, "recognised": False,
            "provenance": IDENTITY_OS_APP_NAME,
            "reason": ("this application is not on the curated vendor list, so "
                       "its name cannot be shown to be vendor-set rather than "
                       "read off a screen"),
        }

    # Every value below is a constant: two out of the matched file entry, two
    # written here. ``guide.app_aliases`` is not read at all — it is machine-derived
    # text, and machine-derived text does not become an identity
    # because a stamp sits beside it.
    return {
        "name": entry["application"],
        "guide_type": "application",
        "identity_provenance": IDENTITY_OS_APP_NAME,
        # No bundle id. The token above attests where the NAME came from and
        # nothing else, and a second identity string with no attestation behind
        # it is a leak — see THE TOKEN ATTESTS ONE STRING.
        "aliases": list(entry["aliases"]),
    }, name, {"established": True, "recognised": True,
              "provenance": IDENTITY_OS_APP_NAME,
              "reason": ("the OS-reported name matched a curated vendor "
                         "application, which is the name that travels")}


# ── states ──────────────────────────────────────────────────────────────────
def _project_element(
    census: LocatorCensus, app: str, state_ref: str, slot: str, el: Any,
    min_sessions: int,
) -> Dict[str, Any]:
    """One element: its structure always, its text only when measured chrome.

    An element whose label is content still travels — as an anonymous structural
    slot. The role, the type and the fact that it accepts input are what another
    tenant needs to find their OWN value's field; the value itself is ours.
    """
    role = str(_attr(el, "role", "") or "")
    out: Dict[str, Any] = {
        "slot": slot,
        "role": role,
        "element_type": str(_attr(el, "element_type", "") or ""),
        "actions": [str(a) for a in (_attr(el, "actions", []) or [])],
        "accepts_input": bool(_attr(el, "accepts_input", False)),
        "label": None,
        "identifier": None,
    }
    withheld: Dict[str, str] = {}

    for field_name, kind in (("label", KIND_AX_TITLE),
                             ("identifier", KIND_AX_IDENTIFIER)):
        source = "title" if kind == KIND_AX_TITLE else "identifier"
        text = str(_attr(el, source, "") or "").strip()
        if not text:
            continue
        v = census.classify(app=app, state_ref=state_ref, kind=kind, slot=slot,
                            text=text, min_sessions=min_sessions)
        if v.travels:
            out[field_name] = text
            out.setdefault("evidence", {})[field_name] = {
                "sessions": v.sessions, "variants": v.variants}
        else:
            # The reason is evidence about the SLOT, never about the text: it
            # tells the receiving tenant "this position holds data, fill it with
            # your own" without saying what ours held.
            withheld[field_name] = f"{v.verdict}:{v.reason}"

    if withheld:
        out["withheld"] = withheld
    return out


def _project_ocr_anchor(
    census: LocatorCensus, app: str, state_ref: str, anchor: Any, min_sessions: int,
) -> Tuple[Optional[Dict[str, Any]], Verdict]:
    """An OCR anchor travels only when its text is measured app chrome.

    A region-less anchor has no structural address, so it can never be measured
    and never travels — ``ocr_slot`` returns None and the verdict is UNSURE.
    """
    text = str(_attr(anchor, "text", "") or "").strip()
    region = _attr(anchor, "region", None)
    slot = ocr_slot(region)
    v = census.classify(app=app, state_ref=state_ref, kind=KIND_OCR, slot=slot,
                        text=text, min_sessions=min_sessions)
    if not v.travels:
        return None, v
    offset = _attr(anchor, "click_offset", (0, 0)) or (0, 0)
    return {
        "slot": slot,
        "text": text,
        "region": [int(x) for x in list(region)[:4]],
        "click_offset": [int(x) for x in list(offset)[:2]],
        "evidence": {"sessions": v.sessions, "variants": v.variants},
    }, v


def _fingerprint_leaks(
    census: LocatorCensus, app: str, state_ref: str, fingerprint: Any,
    min_sessions: int,
) -> List[Dict[str, Any]]:
    """Titles inside ``required_elements`` that are not measured chrome.

    A non-empty list means the node's own IDENTITY carries screen text. The
    caller drops the state.
    """
    required = list(_attr(fingerprint, "required_elements", []) or [])
    leaks: List[Dict[str, Any]] = []
    for el, slot in zip(required, element_slots(required)):
        text = str(_attr(el, "title", "") or "").strip()
        if not text:
            continue
        v = census.classify(app=app, state_ref=state_ref, kind=KIND_AX_TITLE,
                            slot=slot, text=text, min_sessions=min_sessions)
        if not v.travels:
            leaks.append({"slot": slot, "verdict": v.verdict, "reason": v.reason,
                          "sessions": v.sessions, "variants": v.variants})
    return leaks


# ── step order ──────────────────────────────────────────────────────────────
def _derive_step_order(
    state_refs: Sequence[str], transitions: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Deterministic walks through the projected graph — the order things happen.

    From every entry state (no inbound edge) follow the best-performing outgoing
    transition that reaches somewhere new. Self-loops never advance a path. A
    walk of a single state is not an order and is not emitted.
    """
    out_edges: Dict[str, List[int]] = {}
    indegree: Dict[str, int] = {}
    for i, t in enumerate(transitions):
        if t["from"] == t["to"]:
            continue
        out_edges.setdefault(t["from"], []).append(i)
        indegree[t["to"]] = indegree.get(t["to"], 0) + 1

    roots = sorted(r for r in state_refs if not indegree.get(r))
    if not roots and state_refs:
        roots = [sorted(state_refs)[0]]

    paths: List[Dict[str, Any]] = []
    for root in roots:
        visited = {root}
        states = [root]
        used: List[int] = []
        current = root
        while True:
            candidates = [i for i in out_edges.get(current, [])
                          if transitions[i]["to"] not in visited]
            if not candidates:
                break
            best = max(candidates, key=lambda i: (
                _success_rate(transitions[i]["times_executed"],
                              transitions[i]["times_succeeded"]),
                transitions[i]["times_executed"],
                -i,
            ))
            used.append(best)
            current = transitions[best]["to"]
            visited.add(current)
            states.append(current)
        if len(states) > 1:
            paths.append({"path_id": f"p{len(paths)}", "states": states,
                          "transitions": used})
    return paths


# ── the projection ──────────────────────────────────────────────────────────
def project_app_guide(
    guide: Any,
    census: LocatorCensus,
    *,
    ocr_anchors: Optional[Mapping[str, Sequence[Any]]] = None,
    min_sessions: int = DEFAULT_MIN_SESSIONS,
) -> Dict[str, Any]:
    """Project one stored ``AppGuide`` + its state graph into a shareable map.

    Args:
        guide: an ``AppGuide`` (or anything with the same attributes).
        census: the measured ledger. An EMPTY census projects an EMPTY map —
            that is correct, not a bug: nothing has been measured, so nothing is
            known to be chrome.
        ocr_anchors: LOCAL ``state_id`` -> OCR anchors observed on that state.
            Nothing in anyuse persists OCR anchors today (``OCRAnchor`` has no
            to_dict and no producer outside its test), so the caller supplies
            them; they must have been fed to the census under the same regions.
        min_sessions: distinct sessions a locator must recur in to be chrome.

    Returns:
        ``{"map": <shareable>, "report": <local-only diagnostics>}``.
        Send ``["map"]``. Never send ``["report"]`` — it names local state ids.

        ``map`` is None when the app identity was not established
        (``report["identity"]["established"] is False``) — either its provenance
        was never recorded / came from a URL, or the name matched no curated
        vendor application (``["recognised"] is False``). There is nothing to
        send: the map would have had to name the app, and the only name available
        is one this machine produced. See the module docstring.
    """
    # ``min_sessions`` is annotated ``int`` and nothing enforced that. It is a
    # caller's argument and it is copied verbatim into ``completeness``, which the
    # transport reads as a number and puts in prose that leaves the machine — so
    # an un-checked annotation here is a typed hole two modules away. Checked at
    # the door, before it can be stored anywhere.
    min_sessions = _require_count("min_sessions", min_sessions)

    # Identity first. If the map cannot say which app it describes without naming
    # a customer, nothing downstream should exist to be leaked by accident.
    identity, local_scope, identity_verdict = _app_identity(guide)
    if identity is None:
        refusal = {"map": None,
                   "report": {"excluded": [], "leaks": [],
                              "never_travels": list(NEVER_TRAVELS),
                              "identity": identity_verdict}}
        _assert_shareable(refusal)
        json.dumps(refusal)
        return refusal

    graph = guide.get_state_graph()
    states: Dict[str, Any] = dict(_attr(graph, "states", {}) or {})
    anchors_by_state = dict(ocr_anchors or {})
    # THE SCOPE IS LOCAL, THE IDENTITY IS THE FILE'S. ``guide_app`` addresses the
    # census and matches the states, both of which were recorded under whatever
    # the machine reported; it is never written into the map. The map is named by
    # ``identity["name"]``, which came out of the vendor file — so a tenant that
    # reports "SAP Logon" and one that reports "SAP GUI" measure under their own
    # keys and publish under one.
    guide_app = local_scope

    excluded: List[Dict[str, Any]] = []
    leaks: List[Dict[str, Any]] = []
    kept: List[Tuple[str, str, Any]] = []      # (state_ref, local_id, state)
    tainted_refs: set = set()

    # Pass 1 — decide per local state. A fingerprint that carries screen text
    # taints its structural ref for every state that shares it.
    for local_id, state in sorted(states.items()):
        state_ref = state_ref_of(state)
        app = str(_attr(state, "app", "") or guide_app)
        if not state_ref:
            excluded.append({"kind": "state", "state_id": local_id,
                             "reason": "no_structural_identity"})
            continue
        if app.casefold() != guide_app.casefold():
            # This screen was recorded under a different app than the one whose
            # identity we established. Its scope is an identity of unknown
            # provenance, so the node does not travel — same rule as a
            # content-bearing fingerprint. (Case only: the generator lowercases
            # the guide's key, the recorder does not.)
            excluded.append({"kind": "state", "state_id": local_id,
                             "state_ref": state_ref,
                             "reason": "foreign_app_identity"})
            continue
        fp_leaks = _fingerprint_leaks(census, app, state_ref,
                                      _attr(state, "fingerprint", None), min_sessions)
        if fp_leaks:
            tainted_refs.add(state_ref)
            excluded.append({"kind": "state", "state_id": local_id,
                             "state_ref": state_ref,
                             "reason": "fingerprint_embeds_content",
                             "slots": [fl["slot"] for fl in fp_leaks]})
            leaks.append({
                "where": "UIFingerprint.required_elements",
                "state_id": local_id, "state_ref": state_ref, "app": app,
                "detail": ("the node's own identity carries raw AX titles; the "
                           "state was DROPPED, not sanitised"),
                "slots": fp_leaks,
            })
            continue
        kept.append((state_ref, local_id, state))

    # Pass 2 — build, merging local states that share one structural ref.
    projected: Dict[str, Dict[str, Any]] = {}
    labels_kept = labels_withheld = 0
    anchors_out = 0
    # Counted over EVERY state, including the ones pass 1 dropped: an anchor on a
    # withheld node is a withheld anchor, and the receiver should see that.
    anchors_in = sum(len(anchors_by_state.get(sid, ()) or ()) for sid in states)

    for state_ref, local_id, state in kept:
        if state_ref in tainted_refs:
            excluded.append({"kind": "state", "state_id": local_id,
                             "state_ref": state_ref,
                             "reason": "sibling_state_fingerprint_embeds_content"})
            continue
        app = str(_attr(state, "app", "") or guide_app)
        fp = _attr(state, "fingerprint", None)
        ecr = list(_attr(fp, "element_count_range", (0, 0)) or (0, 0))[:2]

        node = projected.get(state_ref)
        if node is None:
            node = {
                # No "app": the identity is named ONCE, at map["app"], where its
                # provenance was established. Repeating it per screen put the
                # string on the wire once per node and measured nothing.
                "state_ref": state_ref,
                "structural_hash": str(_attr(fp, "structural_hash", "") or ""),
                "role_frequency": {str(k): int(v) for k, v in
                                   sorted((_attr(fp, "role_frequency", {}) or {}).items())},
                "element_count_range": [int(ecr[0]), int(ecr[1])] if len(ecr) == 2 else [0, 0],
                "elements": [],
                "ocr_anchors": [],
            }
            projected[state_ref] = node

        by_slot = {e["slot"]: e for e in node["elements"]}
        elements = list(_attr(state, "elements", []) or [])
        for el, slot in zip(elements, element_slots(elements)):
            new = _project_element(census, app, state_ref, slot, el, min_sessions)
            labels_kept += sum(1 for f in ("label", "identifier") if new.get(f))
            labels_withheld += len(new.get("withheld", {}))
            old = by_slot.get(slot)
            if old is None:
                by_slot[slot] = new
                node["elements"].append(new)
            else:
                # Two local states share this structural slot. Any disagreement
                # about the text means the slot is not stable — withhold it.
                for f in ("label", "identifier"):
                    if old.get(f) != new.get(f):
                        old[f] = None
                        old.setdefault("withheld", {})[f] = "unsure:slot_disagrees_across_states"
                        old.get("evidence", {}).pop(f, None)
                        if not old.get("evidence", True):
                            old.pop("evidence")

        seen_anchor_slots = {a["slot"] for a in node["ocr_anchors"]}
        for anchor in anchors_by_state.get(local_id, ()) or ():
            row, verdict = _project_ocr_anchor(census, app, state_ref, anchor,
                                               min_sessions)
            if row is None:
                excluded.append({"kind": "ocr_anchor", "state_id": local_id,
                                 "state_ref": state_ref,
                                 "reason": f"{verdict.verdict}:{verdict.reason}"})
                continue
            if row["slot"] in seen_anchor_slots:
                continue
            seen_anchor_slots.add(row["slot"])
            node["ocr_anchors"].append(row)
            anchors_out += 1

    for node in projected.values():
        node["elements"].sort(key=lambda e: e["slot"])
        node["ocr_anchors"].sort(key=lambda a: a["slot"])

    # Pass 3 — transitions, over structural refs only.
    ref_by_id = {local_id: ref for ref, local_id, _ in kept if ref in projected}
    merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for t in list(_attr(graph, "transitions", []) or []):
        from_id = str(_attr(t, "from_state", "") or "")
        to_id = str(_attr(t, "to_state", "") or "")
        from_ref = ref_by_id.get(from_id, "")
        to_ref = ref_by_id.get(to_id, "")
        if not from_ref or not to_ref:
            excluded.append({"kind": "transition", "from_state": from_id,
                             "to_state": to_id, "reason": "endpoint_not_projected"})
            continue

        app = str(_attr(states.get(from_id), "app", "") or guide_app)
        intent = str(_attr(t, "intent", "") or "").strip()
        v = census.classify(app=app, state_ref=from_ref, kind=KIND_INTENT,
                            slot=f"edge->{to_ref}", text=intent,
                            min_sessions=min_sessions)
        # An intent is LLM/user-written prose about a real screen; it can name a
        # record just as easily as an action. Same measured rule, same default.
        intent_out = intent if v.travels else None
        key = (from_ref, to_ref, intent_out or "")
        row = merged.get(key)
        if row is None:
            row = {
                "from": from_ref, "to": to_ref, "intent": intent_out,
                "intent_withheld": None if v.travels else f"{v.verdict}:{v.reason}",
                "modalities": [], "times_executed": 0, "times_succeeded": 0,
            }
            merged[key] = row
        row["times_executed"] += int(_attr(t, "times_executed", 0) or 0)
        row["times_succeeded"] += int(_attr(t, "times_succeeded", 0) or 0)

        by_modality = {m["modality"]: m for m in row["modalities"]}
        for impl in list(_attr(t, "implementations", []) or []):
            # modality + counts only. The command dict NEVER travels: it holds ax
            # paths, JS source and free-text computer-use prompts.
            name = str(_attr(impl, "modality", "") or "")
            if not name:
                continue
            m = by_modality.get(name)
            if m is None:
                m = {"modality": name, "times_used": 0, "times_succeeded": 0}
                by_modality[name] = m
                row["modalities"].append(m)
            m["times_used"] += int(_attr(impl, "times_used", 0) or 0)
            m["times_succeeded"] += int(_attr(impl, "times_succeeded", 0) or 0)

    transitions: List[Dict[str, Any]] = []
    for key in sorted(merged):
        row = merged[key]
        row["modalities"].sort(key=lambda m: m["modality"])
        for m in row["modalities"]:
            m["success_rate"] = _success_rate(m["times_used"], m["times_succeeded"])
        row["success_rate"] = _success_rate(row["times_executed"],
                                            row["times_succeeded"])
        transitions.append(row)

    state_rows = [projected[r] for r in sorted(projected)]
    step_order = _derive_step_order([r["state_ref"] for r in state_rows], transitions)

    shareable: Dict[str, Any] = {
        "projection_version": PROJECTION_VERSION,
        "kind": PROJECTION_KIND,
        # Slugified from the RECOGNISED name, never the observed one — the handle
        # is an identity too, and two tenants' maps of one application have to
        # land on the same one to be worth sharing.
        "ref": f"anyuse:map/{_app_slug(identity['name'])}",
        # Recognised above, and carrying the provenance token that got it as far
        # as the list. There is no "domain" key: a domain is a URL, and a URL
        # never leaves.
        "app": dict(identity),
        "states": state_rows,
        "transitions": transitions,
        "step_order": step_order,
        # Counts only — so the receiver knows the map is partial without learning
        # anything about what was held back.
        "completeness": {
            "states_projected": len(state_rows),
            "states_withheld": sum(1 for e in excluded if e["kind"] == "state"),
            "transitions_projected": len(transitions),
            "transitions_withheld": sum(1 for e in excluded
                                        if e["kind"] == "transition"),
            "labels_projected": labels_kept,
            "labels_withheld": labels_withheld,
            "ocr_anchors_projected": anchors_out,
            "ocr_anchors_withheld": anchors_in - anchors_out,
            "min_sessions": min_sessions,
        },
    }

    result = {
        "map": shareable,
        "report": {
            "excluded": excluded,
            "leaks": leaks,
            "never_travels": list(NEVER_TRAVELS),
            "identity": identity_verdict,
        },
    }

    # The leak guard. Bytes, images and Selectors cannot be produced by the
    # allow-list above, so a failure here is a builder bug and is fatal.
    _assert_shareable(result)
    # And the typed guard beside it: every completeness value is a COUNT. The
    # walk above proves a leaf is JSON — a string is JSON — so it cannot tell a
    # counter from a sentence. This can, and these are the values the transport
    # interpolates into prose it sends. Both sides check independently; that is
    # the point, same as the provenance token.
    for key, value in shareable["completeness"].items():
        _require_count(f"completeness.{key}", value)
    json.dumps(result)
    return result
