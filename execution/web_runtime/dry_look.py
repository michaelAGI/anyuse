"""The dry look — admit a workflow on ONE consistent derivation, never overwrite it.

An earlier recorder admitted an entry when
`derive_list_items([rec, fresh1])` yielded a selector, then loaded `fresh2`,
re-derived over all three and stored THAT. A bad third load (a redirect to a
different page shape, a 151-byte document that had not rendered) silently
turned an admitted entry into `no_ground_truth` — the class the dry look
exists to keep out of the denominator.

The rule:

1. **Admission** is decided on the first two loads and is final: the selector
   `derive_list_items` finds across `rec` and `fresh1`, with at least
   `MIN_GROUND_TRUTH` values on each, or the entry is rejected.
2. **The third load is checked against the admitted selector, never re-derived.**
   It passes if the same selector extracts at least `MIN_GROUND_TRUTH` values
   from it and those values are not the first load's values (the list changes
   with the input). Otherwise the third load is a **recorder fault**: the
   recorder re-loads it once, and if it still fails the entry is REPLACED and
   named in the ledger as `recorder_fault_third_load` — it is not admitted with
   no ground truth, and the admitted selector is not overwritten.
3. Every stored recording carries the admitted selector and the items it
   extracts, so what the ladder grades against is the derivation the dry look
   admitted on.
"""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from anyuse.execution.html_extract import derive_list_items, extract

#: The fixed minimum number of derived values for an entry to be admitted.
MIN_GROUND_TRUTH = 3


def _norm(items: Sequence[str]) -> set:
    return {" ".join(str(i).split()).lower() for i in items if str(i).strip()}


def admit(rec: dict, fresh1: dict, min_values: int = MIN_GROUND_TRUTH) -> Tuple[str, str]:
    """``(selector, why)`` — the dry look on the first two loads.

    A selector is returned only if it extracts at least ``min_values`` values
    from BOTH loads and the two lists differ. ``why`` names the reason when
    ``selector`` is empty.
    """
    doms = [r.get("dom") for r in (rec, fresh1)]
    if not all(doms) or any(r.get("transport_error") for r in (rec, fresh1)):
        return "", "fewer than two pages loaded, so no repeated group could be compared"
    from anyuse.execution.api_compile import error_document
    for r in (rec, fresh1):
        err = error_document(r)
        if err:
            return "", f"the result page is an error page ({err}), not a result list"
    selector, _items = derive_list_items(doms)
    if not selector:
        return "", "no repeated group changed between the two inputs"
    a = extract(rec["dom"], selector, limit=30)
    b = extract(fresh1["dom"], selector, limit=30)
    if len(a) < min_values or len(b) < min_values:
        return "", (f"only {min(len(a), len(b))} value(s) derived on one of the two "
                    f"loads, {min_values} required")
    if _norm(a) == _norm(b):
        return "", "the repeated group is identical for both inputs"
    return selector, ""


def check_third(selector: str, rec: dict, fresh2: dict,
                min_values: int = MIN_GROUND_TRUTH) -> Tuple[bool, str, List[str]]:
    """Does the THIRD load answer the ADMITTED selector? Never re-derives.

    ``(ok, why, items)``. A third load that fails is a recorder fault, not a
    property of the workflow: the entry was admitted on evidence that the page
    answers its input, and one more load cannot un-admit it.
    """
    if not fresh2.get("dom") or fresh2.get("transport_error"):
        return False, "the third load produced no document", []
    items = extract(fresh2["dom"], selector, limit=30)
    if len(items) < min_values:
        return False, (f"the admitted selector {selector!r} extracts {len(items)} value(s) "
                       f"from the third load, {min_values} required — a different page "
                       f"shape or an unrendered document"), items
    if _norm(items) == _norm(extract(rec.get("dom") or "", selector, limit=30)):
        return False, "the third load shows the first load's list: it did not answer its input", items
    return True, "", items


def apply(recs: Sequence[dict], selector: str) -> None:
    """Store the ADMITTED selector and its items on every recording. No re-derivation."""
    for r in recs:
        r["ground_truth_selector"] = selector
        r["ui"]["items"] = (extract(r["dom"], selector, limit=30)
                            if selector and r.get("dom") else [])
        r["dry_look"] = {"admitted_on": ["rec", "fresh1"], "selector": selector,
                         "rule": "single derivation; third load checked, never re-derived"}


def summary(recs: Sequence[dict]) -> Dict[str, int]:
    return {r.get("tag", f"#{i}"): len((r.get("ui") or {}).get("items") or [])
            for i, r in enumerate(recs)}
