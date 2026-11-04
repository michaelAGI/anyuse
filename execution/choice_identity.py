"""Choice parameters bound by the option's IDENTITY, not its title.

A parameter that picks one option from a list -- a menu item of a settings group, a popup's item, a list row -- is
bound by what identifies the option whatever the UI language and wherever the option sits in the list:

1. ``id:<identifier>``: its AXIdentifier, when no other option of the recorded group carries it. Assigned by the
   app's developer: the same in every UI language and at every position.
2. ``id:<identifier>|cmd:<char>/<mods>|title:<title>``: an identifier SHARED by the group's items (one action
   selector for all of them: Lexikon's ``chooseDictionary:``, Calculator's ``menuAction:``), told apart by the item's
   shortcut and its recorded title. At replay the recorded title decides when it names exactly one live item of
   that identifier (a reordered list moves shortcuts that follow the order, as Lexikon's dictionaries do); when it
   names none (another UI language renamed the items) the shortcut decides; anything else is refused.
3. ``value:<value>``: an item's own value when it has no identifier (a list row, a popup item).
4. Nothing position-free tells it apart (only its title or its index path): no key. Such an option cannot be a
   choice value bound by identity.

A value is named by such a key, by the option's RECORDED key (the effect map's ``key``) or by its RECORDED label
(resolved against the recorded group, never against the live titles: the live titles may be another language).
Pure: no pyobjc, no subprocess, no network.
"""
from __future__ import annotations

import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

ID, CMD, TITLE, VALUE = "id:", "|cmd:", "|title:", "value:"


def _nfc(s: Any) -> str:
    return unicodedata.normalize("NFC", str(s or "")).strip()


def _norm(s: Any) -> str:
    return " ".join(_nfc(s).casefold().split())


def identity_of(option: Dict[str, Any]) -> Dict[str, str]:
    """The identity fields of one option: an effect-map option (``{key, label, path, trigger}``), a guest menu
    item (``desktop_gen.item_identity``: identifier, cmd, mods, title) or a fingerprint element (role, title,
    identifier, value)."""
    trig = option.get("trigger") if isinstance(option.get("trigger"), dict) else None
    if trig is not None:
        return {"identifier": _nfc(trig.get("identifier")), "cmd": _nfc(trig.get("cmd_char")).lower(),
                "mods": _nfc(trig.get("cmd_mods") or "0"), "title": _nfc(option.get("label")), "value": ""}
    return {"identifier": _nfc(option.get("identifier")),
            "cmd": _nfc(option.get("cmd") if "cmd" in option else option.get("cmd_char")).lower(),
            "mods": _nfc(option.get("mods") if "mods" in option else option.get("cmd_mods")) or "0",
            "title": _nfc(option.get("title") if "title" in option else option.get("label")),
            "value": _nfc(option.get("value"))}


def stable_keys(options: Sequence[Dict[str, Any]]) -> List[Optional[str]]:
    """One position-free key per option of a recorded group (module docstring), or None where nothing
    position-free tells the option apart from the others."""
    ids = [identity_of(o) for o in options]
    out: List[Optional[str]] = []
    for i in ids:
        ident = i["identifier"]
        if ident and sum(1 for j in ids if j["identifier"] == ident) == 1:
            out.append(ID + ident)
        elif ident and i["cmd"] and sum(1 for j in ids if (j["identifier"], j["cmd"], j["mods"])
                                        == (ident, i["cmd"], i["mods"])) == 1:
            out.append(f"{ID}{ident}{CMD}{i['cmd']}/{i['mods']}{TITLE}{i['title']}")
        elif not ident and i["value"] and sum(1 for j in ids if not j["identifier"]
                                              and j["value"] == i["value"]) == 1:
            out.append(VALUE + i["value"])
        else:
            out.append(None)
    return out


def parse_key(key: str) -> Optional[Dict[str, str]]:
    """The identity a key names, or None for a string that is not a key."""
    key = _nfc(key)
    if key.startswith(VALUE):
        return {"identifier": "", "cmd": "", "mods": "0", "title": "", "value": key[len(VALUE):], "shared": False}
    if not key.startswith(ID):
        return None
    rest = key[len(ID):]
    if CMD not in rest:
        return {"identifier": rest, "cmd": "", "mods": "0", "title": "", "value": "", "shared": False} if rest else None
    ident, tail = rest.split(CMD, 1)
    if TITLE not in tail:
        return None
    cm, title = tail.split(TITLE, 1)
    c, _, m = cm.partition("/")
    return {"identifier": ident, "cmd": c.lower(), "mods": m or "0", "title": title, "value": "", "shared": True}


def domain(options: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The recorded group as a parameter's value domain: per option its stable key (None: not bindable), its
    recorded label and its recorded key."""
    return [{"key": k, "label": _nfc(o.get("label") if "label" in o else o.get("title")),
             "recorded_key": o.get("key")} for o, k in zip(options, stable_keys(options))]


NO_IDENTITY = ("has no position-free identity (no identifier, shortcut or value tells it apart): it cannot be "
               "bound by identity")


def resolve_value(value: Any, options: Sequence[Dict[str, Any]], *,
                  require_key: bool = True) -> Tuple[Optional[int], str]:
    """(index into ``options``, why not): ``value`` names exactly one option of the RECORDED group by its stable
    key, its recorded key or its recorded label. A label is matched against the recorded labels only. With
    ``require_key`` an option without a stable key is refused (``NO_IDENTITY``)."""
    if not isinstance(value, str) or not value.strip():
        return None, "an empty choice value"
    keys = stable_keys(options)
    v = _nfc(value)
    hit = [i for i, k in enumerate(keys) if k is not None and k == v]
    if not hit:
        hit = [i for i, o in enumerate(options) if o.get("key") and _nfc(o.get("key")) == v]
    if not hit:
        hit = [i for i, o in enumerate(options)
               if _norm(o.get("label") if "label" in o else o.get("title")) == _norm(v)]
    if len(hit) != 1:
        return None, f"{len(hit)} options of the recorded group are named {value!r} (by key or recorded label)"
    if require_key and keys[hit[0]] is None:
        return None, f"the option {value!r} {NO_IDENTITY}"
    return hit[0], ""


def identity_for(options: Sequence[Dict[str, Any]], index: int) -> Optional[Dict[str, Any]]:
    """The identity the replay carries to the guest for option ``index`` (None: no stable key)."""
    key = stable_keys(options)[index]
    if key is None:
        return None
    ident = parse_key(key) or {}
    return dict(ident, key=key, title=identity_of(options[index])["title"])


def resolve_live(identity: Dict[str, Any], items: Sequence[Dict[str, Any]]) -> Tuple[Optional[int], str]:
    """(index into the LIVE ``items``, why not). ``items`` are live identities (identifier, cmd, mods, title[,
    value]) in whatever order and language the screen shows. The rule of the module docstring; never the
    position, and the title only where the identifier is shared."""
    live = [identity_of(it) for it in items]
    ident, value = _nfc(identity.get("identifier")), _nfc(identity.get("value"))
    if not ident and value:
        hit = [k for k, it in enumerate(live) if not it["identifier"] and it["value"] == value]
        return (hit[0], "") if len(hit) == 1 else (None, f"{len(hit)} live items carry the value {value!r}")
    if not ident:
        return None, "the option has no identity to find it by"
    same = [k for k, it in enumerate(live) if it["identifier"] == ident]
    if not identity.get("shared"):
        return (same[0], "") if len(same) == 1 else (None, f"{len(same)} live items carry the identifier {ident!r}")
    title = _norm(identity.get("title"))
    by_title = [k for k in same if title and _norm(live[k]["title"]) == title]
    if len(by_title) == 1:
        return by_title[0], ""
    if by_title:
        return None, f"{len(by_title)} live items of {ident!r} are titled {identity.get('title')!r}"
    cmd, mods = _nfc(identity.get("cmd")).lower(), _nfc(identity.get("mods")) or "0"
    by_cmd = [k for k in same if cmd and live[k]["cmd"] == cmd and live[k]["mods"] == mods]
    if len(by_cmd) == 1:
        return by_cmd[0], ""
    return None, (f"no live item of {ident!r} is titled {identity.get('title')!r} and {len(by_cmd)} carry its "
                  f"shortcut {cmd!r}")
