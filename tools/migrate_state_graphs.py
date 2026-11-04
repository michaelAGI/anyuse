"""Re-key stored state graphs onto THE state id, and repair their fingerprints.

Why this exists
---------------
Two producers used to write into the same ``StateGraph.states``
keyspace with incompatible id schemes:

* ``memory/recorder`` minted ``{app}_{role-distribution-hash[:12]}`` —
  title-INsensitive, so the same screen with different content merged;
* ``guides/generator`` minted ``{app}_{sha256(sorted (role,title) pairs)[:12]}``
  — title-SENSITIVE, so any badge, filename or timestamp minted a new node.

The ids are indistinguishable by shape, so a stored graph cannot tell you which
producer made a node. This tool does not need to know: it recomputes the
canonical id from data every state already carries, and any state whose stored
id already equals the canonical one is left completely alone.

How the canonical id is recovered
---------------------------------
``generate_state_id`` hashes the ROLE DISTRIBUTION of the actionable elements.
A persisted state carries exactly that list in
``fingerprint.required_elements`` (both producers built it from their actionable
subset), and ``_role_frequency`` reads nothing but ``role``. So

    canonical = generate_state_id(fingerprint.required_elements, state["app"])

reproduces a recorder-minted id byte-for-byte and gives a generator-minted one
its new, mergeable identity. States with no ``app`` (self-healing placeholders,
which carry ``app=""`` until the generator fills them in) are reported and
skipped rather than guessed at.

What it changes when two states collapse onto one id
----------------------------------------------------
The fingerprint of the state with the most evidence (highest ``times_seen``)
wins outright; ``times_seen`` is summed, ``elements`` are unioned by
(role, title), and url/title patterns are unioned. The fingerprints are NOT
blended: intersecting ``required_elements`` would gut the anchor list whenever
titles drifted, and unioning it would make the drift gate fire on content that
is only sometimes present. The report names the winner for every merge.
Fingerprints with no ``role_frequency`` get one derived from
``required_elements`` (approximate — it is the actionable subset, not the full
snapshot — which is the same derivation ``UIFingerprint.from_dict`` already
does on load, made permanent here).

Safety
------
* **Dry run by default.** Nothing is written without ``--write``.
* **No default path.** ``--path`` is required; there is no implicit
  ``~/.anyuse``.
* Writing anywhere under the real ``~/.anyuse`` additionally requires
  ``--yes-this-is-my-live-data``, so no script, test or agent can migrate the
  user's live guides by accident.
* ``--write`` keeps a ``<file>.pre-migration.bak`` next to each file it
  rewrites unless ``--no-backup`` is given.

usage:
    python -m anyuse.tools.migrate_state_graphs --path DIR_OR_FILE
    python -m anyuse.tools.migrate_state_graphs --path DIR_OR_FILE --write
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from anyuse.guides.state_graph import _role_frequency, generate_state_id


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class FileReport:
    path: str
    states_scanned: int = 0
    ids_unchanged: int = 0
    ids_rekeyed: int = 0
    states_merged: int = 0
    role_frequency_added: int = 0
    transitions_rewritten: int = 0
    transitions_dangling: int = 0
    skipped_no_app: int = 0
    skipped_no_fingerprint: int = 0
    rekeys: List[Tuple[str, str]] = field(default_factory=list)
    merges: List[Tuple[str, str, str]] = field(default_factory=list)  # (winner, loser, new id)
    error: Optional[str] = None

    @property
    def changed(self) -> bool:
        return bool(
            self.ids_rekeyed
            or self.states_merged
            or self.role_frequency_added
            or self.transitions_rewritten
        )

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["changed"] = self.changed
        return d


@dataclass
class Report:
    dry_run: bool = True
    files: List[FileReport] = field(default_factory=list)

    def totals(self) -> Dict[str, int]:
        keys = [
            "states_scanned", "ids_unchanged", "ids_rekeyed", "states_merged",
            "role_frequency_added", "transitions_rewritten", "transitions_dangling",
            "skipped_no_app", "skipped_no_fingerprint",
        ]
        return {k: sum(getattr(f, k) for f in self.files) for k in keys}

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "files_scanned": len(self.files),
            "files_changed": sum(1 for f in self.files if f.changed),
            "totals": self.totals(),
            "files": [f.to_dict() for f in self.files],
        }


# ---------------------------------------------------------------------------
# Migration of one guide document (pure — operates on the parsed JSON)
# ---------------------------------------------------------------------------

def _element_key(el: dict) -> Tuple[str, str]:
    return (el.get("role", "") or el.get("element_type", ""), el.get("title", "") or el.get("label", ""))


def _merge_states(winner: dict, loser: dict) -> dict:
    """Merge ``loser`` into ``winner``; the winner's fingerprint is kept whole."""
    merged = json.loads(json.dumps(winner))  # deep copy, JSON-safe by construction
    merged["times_seen"] = int(winner.get("times_seen", 0) or 0) + int(loser.get("times_seen", 0) or 0)
    # Most recently seen wins the timestamp.
    for key in ("last_seen",):
        a, b = winner.get(key) or "", loser.get(key) or ""
        merged[key] = max(a, b)
    if not merged.get("app"):
        merged["app"] = loser.get("app", "")
    # Union list-valued evidence.
    for key in ("url_patterns", "title_patterns", "custom_render_notes"):
        seen, out = set(), []
        for item in list(winner.get(key) or []) + list(loser.get(key) or []):
            if item not in seen:
                seen.add(item)
                out.append(item)
        if out:
            merged[key] = out
    # Union elements by (role, title).
    seen_el, out_el = set(), []
    for el in list(winner.get("elements") or []) + list(loser.get("elements") or []):
        k = _element_key(el)
        if k not in seen_el:
            seen_el.add(k)
            out_el.append(el)
    if out_el:
        merged["elements"] = out_el
    return merged


def migrate_document(doc: dict) -> Tuple[dict, FileReport]:
    """Migrate one guide document in memory. Returns (new_doc, report).

    ``doc`` is the raw JSON of a guide file: it carries ``ui_states`` (a dict of
    state-id -> state) and ``ui_transitions`` (a list). Anything else in the
    document is passed through untouched.
    """
    rep = FileReport(path="<memory>")
    states: Dict[str, dict] = doc.get("ui_states") or {}
    transitions: List[dict] = doc.get("ui_transitions") or []

    remap: Dict[str, str] = {}
    new_states: Dict[str, dict] = {}

    for old_id, state in states.items():
        rep.states_scanned += 1
        if not isinstance(state, dict):
            continue
        fp = state.get("fingerprint")
        if not isinstance(fp, dict):
            rep.skipped_no_fingerprint += 1
            new_states[old_id] = state
            remap[old_id] = old_id
            continue

        # Repair the fingerprint first — this is independent of the re-keying.
        required = fp.get("required_elements") or []
        if not fp.get("role_frequency"):
            derived = _role_frequency(required)
            if derived:
                fp = dict(fp)
                fp["role_frequency"] = derived
                state = dict(state)
                state["fingerprint"] = fp
                rep.role_frequency_added += 1

        app = state.get("app") or ""
        if not app:
            # A self-healing placeholder: no app, so no canonical id exists.
            rep.skipped_no_app += 1
            new_states[old_id] = state
            remap[old_id] = old_id
            continue

        canonical = generate_state_id(required, app)
        remap[old_id] = canonical
        if canonical == old_id:
            rep.ids_unchanged += 1
        else:
            rep.ids_rekeyed += 1
            rep.rekeys.append((old_id, canonical))
            state = dict(state)
            state["state_id"] = canonical

        if canonical in new_states:
            a, b = new_states[canonical], state
            winner, loser = (a, b) if int(a.get("times_seen", 0) or 0) >= int(b.get("times_seen", 0) or 0) else (b, a)
            rep.states_merged += 1
            rep.merges.append((winner.get("state_id", canonical), loser.get("state_id", canonical), canonical))
            merged = _merge_states(winner, loser)
            merged["state_id"] = canonical
            new_states[canonical] = merged
        else:
            new_states[canonical] = state

    new_transitions: List[dict] = []
    for t in transitions:
        if not isinstance(t, dict):
            new_transitions.append(t)
            continue
        src, dst = t.get("from_state", ""), t.get("to_state", "")
        new_src, new_dst = remap.get(src, src), remap.get(dst, dst)
        if (new_src, new_dst) != (src, dst):
            rep.transitions_rewritten += 1
            t = dict(t)
            t["from_state"], t["to_state"] = new_src, new_dst
        if new_src not in new_states or new_dst not in new_states:
            rep.transitions_dangling += 1
        new_transitions.append(t)

    # Collapse transitions that became duplicates after the re-key.
    deduped: List[dict] = []
    seen_t = set()
    for t in new_transitions:
        if not isinstance(t, dict):
            deduped.append(t)
            continue
        key = (t.get("from_state"), t.get("to_state"), t.get("intent"))
        if key in seen_t:
            continue
        seen_t.add(key)
        deduped.append(t)

    new_doc = dict(doc)
    new_doc["ui_states"] = new_states
    new_doc["ui_transitions"] = deduped
    return new_doc, rep


# ---------------------------------------------------------------------------
# Filesystem driver
# ---------------------------------------------------------------------------

def _guide_files(path: Path) -> List[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.glob("*.json") if p.name != "index.json")


def _is_under_real_home_anyuse(path: Path) -> bool:
    """True if ``path`` lives inside the REAL ``~/.anyuse``.

    ``Path.home()`` is deliberately not used: a redirected HOME is exactly the
    situation in which we still want the live directory protected. The real
    home is taken from the password database, which a HOME override cannot move.
    """
    try:
        import pwd
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:  # pragma: no cover - non-POSIX
        real_home = Path(os.path.expanduser("~"))
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    live = (real_home / ".anyuse").resolve() if (real_home / ".anyuse").exists() else (real_home / ".anyuse")
    return resolved == live or live in resolved.parents


def migrate_path(
    path: Path,
    write: bool = False,
    backup: bool = True,
    allow_live_home: bool = False,
) -> Report:
    """Migrate every guide file under ``path``. Dry run unless ``write``."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no such path: {path}")
    if write and _is_under_real_home_anyuse(path) and not allow_live_home:
        raise PermissionError(
            f"refusing to write inside the live AnyUse data directory ({path}). "
            "Re-run with --yes-this-is-my-live-data if that is really what you want, "
            "ideally on a copy first."
        )

    report = Report(dry_run=not write)
    for f in _guide_files(path):
        try:
            doc = json.loads(f.read_text())
        except Exception as exc:
            fr = FileReport(path=str(f), error=f"unreadable: {exc}")
            report.files.append(fr)
            continue
        if not isinstance(doc, dict) or "ui_states" not in doc:
            continue  # not a guide document
        new_doc, fr = migrate_document(doc)
        fr.path = str(f)
        report.files.append(fr)
        if write and fr.changed:
            if backup:
                bak = f.with_suffix(f.suffix + ".pre-migration.bak")
                if not bak.exists():
                    bak.write_text(json.dumps(doc, indent=2))
            f.write_text(json.dumps(new_doc, indent=2))
    return report


def _print_report(report: Report) -> None:
    t = report.totals()
    mode = "DRY RUN (nothing written)" if report.dry_run else "WRITTEN"
    print(f"=== state-graph migration — {mode} ===")
    print(f"files scanned: {len(report.files)}   files that would change: "
          f"{sum(1 for f in report.files if f.changed)}")
    for k in ("states_scanned", "ids_unchanged", "ids_rekeyed", "states_merged",
              "role_frequency_added", "transitions_rewritten", "transitions_dangling",
              "skipped_no_app", "skipped_no_fingerprint"):
        print(f"  {k:26s} {t[k]}")
    for f in report.files:
        if f.error:
            print(f"  ! {f.path}: {f.error}")
            continue
        if not f.changed:
            continue
        print(f"  {f.path}")
        for old, new in f.rekeys[:20]:
            print(f"      re-key {old} -> {new}")
        if len(f.rekeys) > 20:
            print(f"      … and {len(f.rekeys) - 20} more re-keys")
        for winner, loser, new in f.merges[:20]:
            print(f"      merge  {loser} into {winner} as {new}")
    if report.dry_run:
        print("Re-run with --write to apply. A .pre-migration.bak is kept per file.")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m anyuse.tools.migrate_state_graphs",
        description="Re-key stored state graphs onto the single state-id function.",
    )
    ap.add_argument("--path", required=True,
                    help="a guide JSON file or a directory of them (required; there is no default)")
    ap.add_argument("--write", action="store_true",
                    help="apply the migration (default: dry run, nothing is written)")
    ap.add_argument("--no-backup", action="store_true",
                    help="do not keep a .pre-migration.bak next to each rewritten file")
    ap.add_argument("--yes-this-is-my-live-data", action="store_true", dest="allow_live_home",
                    help="permit --write inside the real ~/.anyuse (refused without it)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)

    try:
        report = migrate_path(Path(args.path), write=args.write,
                              backup=not args.no_backup,
                              allow_live_home=args.allow_live_home)
    except (FileNotFoundError, PermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
