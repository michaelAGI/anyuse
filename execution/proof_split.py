"""One rule for every compile path: which inputs FIT, which SELECT, which VALIDATE.

"Proved" overstates what was checked when an input that helped build or choose
the answer is then counted as the evidence for it. Every compile path (attach,
discovery, learn loop, recording/HAR/recompile) asks this module for the split,
so the rule and the words that describe it are the same everywhere.

Roles, in the caller's order and nothing else (deterministic, no shuffling):

* **compile** — the first input; the template is extracted from it;
* **fitting** — further inputs that shape the template or its ground truth:
  page chrome, volatile query parameters, path binding, chain bindings and a
  derived result-list selector (``ground_truth_selector``);
* **selection** — inputs that choose among equally-plausible candidates
  (runner-up requests, alternative binding sites);
* **validation** — inputs that are untouched until the one final check.

Statuses:

* ``proved`` — the final check passed on >= ``min_validation`` validation
  inputs, and no fitting or selection step saw any of them;
* ``checked (selection not held out)`` — the check passed, but a validation
  input took part in fitting or selection (for example: too few inputs to hold
  any out, or a result-list selector derived across every page). Never
  ``proved``;
* ``not proved`` — the check did not pass.

The proof scope (:func:`scope_text`) is built from counts and enums only, so it
is safe on an MCP instruction surface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

PROVED = "proved"
CHECKED = "checked (selection not held out)"
NOT_PROVED = "not proved"

#: Validation inputs a "proved" needs (the fresh-input rule, api_verify).
MIN_VALIDATION = 2
#: Selection inputs set aside when there are enough inputs to do so.
N_SELECTION = 1


def status_for(passed: bool, held_out: bool) -> str:
    if not passed:
        return NOT_PROVED
    return PROVED if held_out else CHECKED


def weaker(a: str, b: str) -> str:
    """The weaker of two statuses (a recompile never upgrades what it inherits)."""
    order = {NOT_PROVED: 0, CHECKED: 1, PROVED: 2}
    return a if order.get(a, 0) <= order.get(b, 0) else b


def value_of(rec: Any) -> str:
    """The normalised input value of a recording (``param.value``) or a case."""
    from anyuse.execution.api_compile import normalise
    if isinstance(rec, dict):
        if "param" in rec:
            return normalise(str(((rec.get("param") or {}).get("value")) or ""))
        if "value" in rec:
            return normalise(str(rec.get("value") or ""))
    return normalise(str(rec or ""))


@dataclass
class ProofSplit:
    """Which inputs play which role, and whether validation is held out."""
    compile: Any = None
    fitting: List[Any] = field(default_factory=list)
    selection: List[Any] = field(default_factory=list)
    validation: List[Any] = field(default_factory=list)
    held_out: bool = True
    min_validation: int = MIN_VALIDATION
    why: str = ""
    #: filled while the proof runs: the values each phase actually used
    seen_fit: List[str] = field(default_factory=list)
    seen_selection: List[str] = field(default_factory=list)
    #: how many of the fitting inputs the compile generated from the site's own
    #: search (execution/chain_fit.py) rather than took from a recording
    generated: int = 0

    # -- instrumentation --------------------------------------------------
    def note_fit(self, values: Iterable[str]) -> None:
        self.seen_fit.extend(v for v in values if v)

    def note_selection(self, values: Iterable[str]) -> None:
        self.seen_selection.extend(v for v in values if v)

    def validation_values(self) -> List[str]:
        return [value_of(r) for r in self.validation]

    def leaked(self) -> List[str]:
        """Validation values that a fitting or selection phase saw."""
        seen = set(self.seen_fit) | set(self.seen_selection)
        seen.update(value_of(r) for r in [self.compile, *self.fitting, *self.selection]
                    if r is not None)
        return sorted({v for v in self.validation_values() if v in seen})

    def ground_truth_leaked(self) -> List[str]:
        """Validation recordings whose DERIVED ground truth may include themselves.

        A recording that carries a derived ``ground_truth_selector`` is held out
        only when it also names the inputs the selector was fitted on
        (``ground_truth_fitted_on``) and its own value is not among them. A
        selector with no recorded provenance is treated as fitted on every
        recording: that is how the bench recorders and the learn loop derived it
        before this module existed.
        """
        out = []
        for rec in self.validation:
            if not isinstance(rec, dict) or not rec.get("ground_truth_selector"):
                continue
            fitted = rec.get("ground_truth_fitted_on")
            me = value_of(rec)
            if not isinstance(fitted, list) or me in {value_of(v) for v in fitted}:
                out.append(me)
        return out

    def finalize(self) -> bool:
        """Re-check the split after fitting and selection ran. Downgrades only."""
        bits = []
        leak = self.leaked()
        if leak:
            bits.append(f"{len(leak)} validation input(s) were also used for fitting or selection")
        gt = self.ground_truth_leaked()
        if gt:
            bits.append(f"the ground truth of {len(gt)} validation input(s) was derived "
                        f"with them in the fitting set")
        if len(self.validation) < self.min_validation:
            bits.append(f"{len(self.validation)} validation input(s), "
                        f"{self.min_validation} required")
        if bits:
            self.held_out = False
            self.why = "; ".join(([self.why] if self.why else []) + bits)
        return self.held_out

    def status(self, passed: bool) -> str:
        return status_for(passed, self.held_out)

    def to_dict(self, passed_count: Optional[int] = None) -> Dict[str, Any]:
        out = {"compile": 1 if self.compile is not None else 0,
               "fitting": len(self.fitting), "selection": len(self.selection),
               "validation": len(self.validation),
               "validation_passed": passed_count,
               "held_out": bool(self.held_out), "why": self.why}
        if self.generated > 0:
            out["fitting_generated"] = self.generated
        return out


def assign(items: Sequence[Any], *, min_validation: int = MIN_VALIDATION,
           n_selection: int = N_SELECTION, n_fitting: int = 0) -> ProofSplit:
    """Split ``items`` (compile first) deterministically, in the given order.

    With at least ``1 + n_fitting + n_selection + min_validation`` items the
    roles are disjoint and ``held_out`` is True. With fewer, nothing can be
    held out: every item is fitting (a derived ground truth needs them all),
    no runner-up is selected, the items after the first are validation, and
    the best the proof can earn is :data:`CHECKED`.
    """
    items = list(items)
    if not items:
        return ProofSplit(held_out=False, min_validation=min_validation,
                          why="no inputs")
    need = 1 + n_fitting + n_selection + min_validation
    if len(items) >= need:
        a = 1 + n_fitting
        b = a + n_selection
        return ProofSplit(compile=items[0], fitting=items[1:a], selection=items[a:b],
                          validation=items[b:], held_out=True,
                          min_validation=min_validation)
    return ProofSplit(compile=items[0], fitting=items[1:], selection=[],
                      validation=items[1:], held_out=False,
                      min_validation=min_validation,
                      why=(f"{len(items)} input(s): fewer than {need} (1 compile"
                           + (f", {n_fitting} fitting" if n_fitting else "")
                           + f", {n_selection} selection, {min_validation} validation), "
                           f"so fitting and selection saw the validation inputs"))


def declared(compile_rec: Any, validation: Sequence[Any], *,
             fitting: Sequence[Any] = (), selection: Sequence[Any] = (),
             min_validation: int = MIN_VALIDATION,
             ground_truth_held_out: bool = True, why: str = "",
             generated: int = 0) -> ProofSplit:
    """A split whose roles the caller already declared (recording files, HAR
    order, a stored recompile set). ``held_out`` still has to survive
    :meth:`ProofSplit.finalize`. ``generated``: how many of ``fitting`` were
    generated from the site's own search."""
    split = ProofSplit(compile=compile_rec, fitting=list(fitting),
                       selection=list(selection), validation=list(validation),
                       held_out=bool(ground_truth_held_out),
                       min_validation=min_validation, why=why,
                       generated=max(0, int(generated or 0)))
    return split


def scope_text(proof: Dict[str, Any], shape: str = "list",
               fields: Sequence[str] = ()) -> str:
    """The exact, bounded claim, from counts and enums only (MCP-safe)."""
    split = proof.get("split") if isinstance(proof.get("split"), dict) else None
    count = int(proof.get("case_count") or len(proof.get("cases") or []))
    status = str(proof.get("status") or (PROVED if proof.get("passed") else NOT_PROVED))
    held = (split or {}).get("held_out")
    if held is None:
        # records written before the split existed: the status is the only fact
        held = status != CHECKED
    inputs = ("held-out input(s) never used for fitting or selection" if held
              else "input(s) that also took part in fitting or selection (not held out)")
    if proof.get("rung") == "browser_replay":
        # learn_loop._try_tier2: coverage of the page's labels by the replayed rows
        what = "Browser replay proof: >=80% of the first 10 UI labels"
    elif shape == "list":
        what = "List proof: >=80% of the first 10 UI labels, in UI order"
    else:
        what = f"Field proof: named fields ({', '.join(fields) or 'declared fields'}) matched"
    text = f"{what}, on {count} {inputs}"
    if split:
        generated = int(split.get("fitting_generated") or 0)
        text += (f"; fit on {split.get('compile', 1)} compile + {split.get('fitting', 0)} "
                 f"fitting input(s)"
                 + (f" ({generated} generated from the site's own search)" if generated else "")
                 + f", selection on {split.get('selection', 0)}")
    text += f"; status {status}; not a completeness guarantee."
    return text
