"""Tier 2 of the repair ladder: a SMALL-MODEL patch. Cerebras gpt-oss-120b
(through ``model_adapter``'s Cerebras preset; never OpenRouter) gets the failing check, the tool's spec and a COMPACT
ELEMENT TABLE of the live page, and answers in the closed patch language of ``repair_patch`` - nothing it writes is
run until ``repair_patch.validate`` / ``apply`` accept it, and nothing is kept until the ladder's gate does.

What the model sees (``prompt``; never the page's whole text, never a credential):

* the step that failed and the run's own reason;
* the tool's spec: the workflow, the search (mode, URL template, input), the result structure (cards: chains, title
  paths, roles; the URL pattern), the refinement, the pager - not its checks, fields' rules or hosts;
* the last known good answer's result titles (the anchors);
* the element table (``element_table``): the page's links of the result URL pattern and its controls (``i``, area, tag,
  label, the link's path and query, the link's repeated-container signature) and, per step, the CANDIDATES a patch
  can name: the repeated containers holding the pattern's links as ready card specs (``card_candidates``), the GET
  search forms, the drop-downs with their names and options, the pagination links.

``patch_from_text``: the answer's first JSON object holding ``ops``. ``CerebrasProposer``: one call per ``propose``
(the ladder's daily budget and the adapter's run budget cap the spend); the adapter is injectable (tests).
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any, Dict, List, Optional, Sequence

from anyuse.execution import demo_compile as C
from anyuse.execution import web_arch as W

MAX_TOKENS = 1600
TABLE_ROWS = 70
CARD_CANDIDATES = 6
FORBIDDEN_MODEL_HOSTS = ("openrouter.ai",)

LANGUAGE = """Answer with ONE JSON object and nothing else: {"ops": [ ...1 to 3 operations... ], "why": "<one sentence>"}.
The ONLY operations:
1. {"op": "replace_anchor", "anchor": "cards", "value": [<card spec>, ...]}  - a card spec exactly as listed under
   CARD CANDIDATES ({"kind", "chain", "rels", "roles"}); copy it, never invent descriptors.
2. {"op": "replace_anchor", "anchor": "search_input", "value": {"name": "<input name>"}}  (typed searches only)
3. {"op": "replace_anchor", "anchor": "refine_control", "index": <i>, "value": {"option_text": "<label>"} or {"text": "<label>"}}
4. {"op": "replace_anchor", "anchor": "pager", "value": {"mode": "param", "key": "<name>", "first": 1, "stride": 1}
   or {"mode": "path_append", "suffix": "page/{page}"} or {"mode": "link", "link": {"text": "<next label>", "rel": "next" or ""}}}
5. {"op": "rebind_param", "param": "q", "to": {"in": "query", "key": "<name>", "path": "/<same-site path>"}}  (path optional)
6. {"op": "rename_url_param", "scope": "search" | "refine" | "pager", "from": "<old name>", "to": "<new name>"}
7. {"op": "insert_nav_step", "at": "after_search", "link": {"text": "<link label>", "path": "/<same-site path>"}}
8. {"op": "skip_nav_step", "index": 0}
Nothing else exists: no checks, hosts, budgets, forms, POST, login or other sites can be changed. If no operation can
repair the step from what the page shows, answer {"ops": [], "why": "<why>"}."""


def _path_query(u: str) -> str:
    try:
        p = urllib.parse.urlsplit(str(u or ""))
    except ValueError:
        return ""
    return (p.path or "/") + (("?" + p.query) if p.query else "")


def card_candidates(read: Dict[str, Any], tool: Dict[str, Any], limit: int = CARD_CANDIDATES) -> List[Dict[str, Any]]:
    """The repeated containers of the page holding links of the tool's result URL pattern, as READY card specs (the
    compile's own reading, ``demo_cards.result_cards``, with every pattern link as a wanted result), the biggest first:
    ``[{"spec", "items", "titles"}]``."""
    from anyuse.execution import demo_cards as K
    import re
    dom = str(read.get("dom") or "")
    if not dom:
        return []
    rx = (tool.get("region") or {}).get("regex") or ""
    try:
        pat = re.compile(rx) if rx else None
    except re.error:
        pat = None
    links = [e for e in read.get("elements") or [] if e.get("tag") == "a" and e.get("href") and
             e.get("area") not in ("header", "nav", "footer") and (pat is None or pat.match(C.strip_fragment(str(e["href"]))))]
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for e in links:
        sig = ((read.get("link_sigs") or {}).get(str(e["href"]).split("#")[0]) or [""])[0]
        if sig:
            groups.setdefault(sig, []).append(e)
    with K.bem_free(C.cards_v11(tool.get("region") or {})):
        # a version-11 region's cards are described without BEM modifiers (``demo_cards.bem_free``)
        return _card_candidates(groups, dom, read, rx, limit)


def _card_candidates(groups: Dict[str, List[Dict[str, Any]]], dom: str, read: Dict[str, Any], rx: str,
                     limit: int) -> List[Dict[str, Any]]:
    from anyuse.execution import demo_cards as K
    out: List[Dict[str, Any]] = []
    for sig, es in sorted(groups.items(), key=lambda x: -len(x[1])):
        if len(es) < 2:
            continue
        wanted = [(C.strip_fragment(str(e["href"])), str(e.get("text") or "")) for e in es[:8]]
        st = K.result_cards(dom, str(read.get("url") or ""), wanted, rx)
        specs = st.get("cards") or []
        if not specs or len(specs) > 4:
            continue
        spec = specs[0]
        if any(o["spec"] == spec for o in out):
            continue
        if any(it.get("widget") for it in K.structure_items(dom, str(read.get("url") or ""), [spec], v10=True)
               if it.get("area") not in ("header", "nav", "footer")):
            continue                                   # a sidebar / widget structure is never offered
        out.append({"spec": spec, "items": len({C._norm(str(e['href'])) for e in es}),
                    "titles": [str(e.get("text") or "")[:60] for e in es[:3]]})
        if len(out) >= limit:
            break
    return out


def element_table(read: Dict[str, Any], tool: Dict[str, Any], step: str, limit: int = TABLE_ROWS) -> str:
    """The compact element table (one line per element): ``i | area | tag | label | path?query | container``."""
    import re
    rx = (tool.get("region") or {}).get("regex") or ""
    try:
        pat = re.compile(rx) if rx else None
    except re.error:
        pat = None
    rows: List[str] = []
    els = read.get("elements") or []

    def pri(e: Dict[str, Any]) -> int:
        if e.get("tag") in ("select", "input", "button"):
            return 0
        if e.get("tag") == "a" and pat is not None and pat.match(C.strip_fragment(str(e.get("href") or ""))):
            return 1
        if e.get("tag") == "a" and (urllib.parse.urlsplit(str(e.get("href") or "")).query or
                                    str(e.get("text") or "").strip().isdigit() or "next" in str(e.get("rel") or "")):
            return 2
        return 3
    pick = sorted([e for e in els if pri(e) < 3 and e.get("area") not in ("footer",)], key=lambda e: (pri(e), e.get("i", 0)))
    for e in pick[:limit]:
        sig = ((read.get("link_sigs") or {}).get(str(e.get("href") or "").split("#")[0]) or [""])[0] if e.get("href") else ""
        extra = ""
        if e.get("tag") == "select":
            extra = " options=" + json.dumps([[o.get("value"), o.get("text")] for o in (e.get("options") or [])[:12]],
                                              ensure_ascii=False)
        rows.append(f"{e.get('i')} | {e.get('area')} | {e.get('tag')} | {str(e.get('text') or '')[:60]!r} | "
                    f"{_path_query(e.get('href') or '')[:120]} | {sig[:140]}{extra}")
    return "\n".join(rows)


def _forms(read: Dict[str, Any]) -> List[Dict[str, Any]]:
    from anyuse.execution import demo_cards as K
    from anyuse.execution import dom_page as D
    if not read.get("dom"):
        return []
    root, base = K.parse(str(read.get("dom") or ""))
    ins = [x for x in K.walk(root) if x.tag == "input"]
    out = []
    for i in D.search_inputs_of(root):
        el = ins[i["index"]]
        f = D.form_of(el)
        out.append({"input_name": el.attrs.get("name") or "", "input_id": el.attrs.get("id") or "",
                    "placeholder": (el.attrs.get("placeholder") or "")[:40],
                    "form": None if f is None else {"method": (f.attrs.get("method") or "get").lower(),
                                                    "action": _path_query(urllib.parse.urljoin(str(read.get("url") or ""),
                                                                                               f.attrs.get("action") or ""))}})
    return out[:6]


def spec_view(tool: Dict[str, Any]) -> Dict[str, Any]:
    """The tool's spec as the model sees it: its anchors, never its checks, fields' rules or hosts."""
    reg = tool.get("region") or {}
    v = {"workflow": tool.get("workflow"), "site": tool.get("site"),
         "search": {k: (tool.get("search") or {}).get(k) for k in ("mode", "template", "input", "fallback_templates")},
         "result_structure": {"url_pattern": reg.get("regex"), "cards": reg.get("cards")}}
    for k in ("refine", "pager", "applied", "repair_nav"):
        if tool.get(k):
            v[k] = tool[k]
    return v


def prompt(tool: Dict[str, Any], step: str, out0: Dict[str, Any], ev: Dict[str, Any],
           anchors: Sequence[Sequence[str]] = ()) -> str:
    read = ev.get("result") or {}
    parts = [f"A compiled READ-ONLY web tool for {tool.get('site')} failed at its {step.upper()} step.",
             f"The run's reason: {out0.get('status')}: {str(out0.get('why') or '')[:300]}",
             "The tool's spec:\n" + json.dumps(spec_view(tool), ensure_ascii=False)[:2500]]
    if anchors:
        parts.append("Its last good answer's results (title | path): " + "; ".join(
            f"{str(t)[:60]} | {_path_query(u)[:80]}" for u, t in list(anchors)[:6]))
    parts.append(f"The live page now: {_path_query(read.get('url') or '')[:160]} (title {str(read.get('title') or '')[:80]!r})")
    if step == "region":
        cc = card_candidates(read, tool)
        parts.append("CARD CANDIDATES (repeated containers holding result-pattern links):\n" + "\n".join(
            f"C{k}: {c['items']} items, e.g. {c['titles']}; spec = {json.dumps(c['spec'], ensure_ascii=False)}"
            for k, c in enumerate(cc)) if cc else "CARD CANDIDATES: none found")
    if step == "search":
        parts.append("SEARCH FORMS on the page: " + json.dumps(_forms(read), ensure_ascii=False))
        if ev.get("start"):
            parts.append("SEARCH FORMS on the start page: " + json.dumps(_forms(ev["start"]), ensure_ascii=False))
    parts.append("ELEMENT TABLE (i | area | tag | label | path?query | repeated-container chain):\n" +
                 element_table(read, tool, step))
    parts.append(LANGUAGE)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# design 2: the old anchors' context, tier 1's near misses as ranked candidates, few-shot examples
# ---------------------------------------------------------------------------

#: the anchors whose context the model sees, the neighbours on each side, the near misses and the examples shown
CONTEXT_ANCHORS = 4
NEIGHBOURS = 2
NEAR_MISSES = 4
EXAMPLES = 3


def _sig(read: Dict[str, Any], href: str) -> str:
    return ((read.get("link_sigs") or {}).get(str(href or "").split("#")[0]) or [""])[0]


def anchor_context(read: Dict[str, Any], tool: Dict[str, Any], anchors: Sequence[Sequence[str]]) -> List[str]:
    """Where the last known good answer's results are on the live page now: per anchor found among the page's links,
    its index, label, area, role (a heading link or not, from its reader context), repeated-container chain and its
    NEIGHBOURS in the reader's order (the elements just before and after it)."""
    els = read.get("elements") or []
    pos = {C._norm(C.strip_fragment(str(e.get("href") or ""))): k for k, e in enumerate(els)
           if e.get("tag") == "a" and e.get("href")}
    out = []
    for u, t in list(anchors)[:12]:
        k = pos.get(C._norm(C.strip_fragment(str(u))))
        if k is None:
            out.append(f"- {str(t)[:50]!r} ({_path_query(u)[:60]}): NOT on the page")
            continue
        e = els[k]
        nb = [f"{x.get('tag')} {str(x.get('text') or '')[:30]!r}" for x in els[max(0, k - NEIGHBOURS):k]] + ["<this>"] + \
            [f"{x.get('tag')} {str(x.get('text') or '')[:30]!r}" for x in els[k + 1:k + 1 + NEIGHBOURS]]
        out.append(f"- i={e.get('i')} {str(e.get('text') or '')[:50]!r} area={e.get('area')} role="
                   f"{e.get('role') or 'link'} chain={_sig(read, str(e.get('href')))[:120]!r} card="
                   f"{str(e.get('ctx') or '')[:60]!r} neighbours: " + " | ".join(nb))
        if len([x for x in out if "NOT on" not in x]) >= CONTEXT_ANCHORS:
            break
    return out


def ranked_candidates(read: Dict[str, Any], tool: Dict[str, Any], anchors: Sequence[Sequence[str]],
                      near: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """The card candidates RANKED by the last known good answer's results they read (most first), then by the cards
    they read BEFORE the first such result (fewest first): ``[{"spec", "items", "anchors", "before", "titles",
    "tried"}]``; a candidate tier 1 already tried carries its outcome (``tried``)."""
    from anyuse.execution import demo_cards as K
    cands = card_candidates(read, tool, limit=CARD_CANDIDATES + 2)
    want = {C._norm(C.strip_fragment(str(u))) for u, _t in anchors}
    dom, url = str(read.get("dom") or ""), str(read.get("url") or "")
    tried = {json.dumps(((a.get("patch") or {}).get("ops") or [{}])[0].get("value"), sort_keys=True): a
             for a in near if ((a.get("patch") or {}).get("ops") or [{}])[0].get("anchor") == "cards"}
    out = []
    with K.bem_free(C.cards_v11(tool.get("region") or {})):
        for c in cands:
            items = [C._norm(C.strip_fragment(str(it.get("url") or ""))) for it in
                     K.structure_items(dom, url, [c["spec"]], v10=True) if it.get("area") not in ("header", "nav", "footer")]
            hits = [k for k, u in enumerate(items) if u in want]
            t = tried.get(json.dumps([c["spec"]], sort_keys=True))
            out.append(dict(c, anchors=len(set(items) & want), before=(hits[0] if hits else len(items)),
                            tried=(f"{t.get('verdict')}: {str(t.get('why') or '')[:90]}" if t else "")))
    out.sort(key=lambda c: (-c["anchors"], c["before"], -c["items"]))
    return out[:CARD_CANDIDATES]


def near_misses(attempts: Sequence[Dict[str, Any]]) -> List[str]:
    """Tier 1's attempts that did not pass the gate, with why (the model should not repeat them blindly)."""
    out = []
    for a in attempts:
        if a.get("tier") == 1 and a.get("patch") and a.get("verdict") not in ("accepted",):
            out.append(f"- {json.dumps((a.get('patch') or {}).get('ops'), ensure_ascii=False)[:260]} -> "
                       f"{a.get('verdict')}: {str(a.get('why') or '')[:140]}")
        if len(out) >= NEAR_MISSES:
            break
    return out


def example_text(examples: Sequence[Dict[str, Any]]) -> str:
    """Few-shot examples: tier-1 repairs the gate ACCEPTED on other tools (``{"failure", "evidence", "patch"}``)."""
    rows = []
    for k, x in enumerate(list(examples)[:EXAMPLES]):
        rows.append(f"Example {k + 1}: failure: {str(x.get('failure') or '')[:160]}\n  evidence: "
                    f"{str(x.get('evidence') or '')[:400]}\n  accepted patch: "
                    f"{json.dumps(x.get('patch'), ensure_ascii=False)[:500]}")
    return "\n".join(rows)


def prompt2(tool: Dict[str, Any], step: str, out0: Dict[str, Any], ev: Dict[str, Any],
            anchors: Sequence[Sequence[str]] = (), near: Sequence[Dict[str, Any]] = (),
            examples: Sequence[Dict[str, Any]] = (), steps: Sequence[str] = ()) -> str:
    """Design 2: ``prompt`` plus the OLD ANCHORS' CONTEXT on the live page (``anchor_context``), the
    card candidates RANKED by the anchors they read with tier 1's near misses marked (``ranked_candidates``,
    ``near_misses``), the search forms whenever the search is among the localized steps, and few-shot EXAMPLES of
    accepted tier-1 repairs on other tools (``example_text``). The same closed language; nothing else changes."""
    read = ev.get("result") or {}
    steps = list(steps or [step])
    parts = [f"A compiled READ-ONLY web tool for {tool.get('site')} failed. The step to repair: {step.upper()}"
             + (f" (the other suspect step: {', '.join(x.upper() for x in steps if x != step)})" if len(steps) > 1 else "")
             + ".",
             f"The run's reason: {out0.get('status')}: {str(out0.get('why') or '')[:300]}",
             "The tool's spec:\n" + json.dumps(spec_view(tool), ensure_ascii=False)[:2500]]
    if anchors:
        parts.append("Its last good answer's results and WHERE THEY ARE ON THE LIVE PAGE NOW (index, label, area, role, "
                     "repeated-container chain, neighbours):\n" + "\n".join(anchor_context(read, tool, anchors)))
    parts.append(f"The live page now: {_path_query(read.get('url') or '')[:160]} (title {str(read.get('title') or '')[:80]!r})")
    if "region" in steps:
        rc = ranked_candidates(read, tool, anchors, near)
        parts.append("CARD CANDIDATES, RANKED (most last-good results read first; 'before' = cards read before the first "
                     "of them; 'tried' = tier 1's outcome with it):\n" + "\n".join(
                         f"C{k}: {c['items']} items, {c['anchors']} last-good results, before={c['before']}"
                         + (f", tried={c['tried']!r}" if c["tried"] else "") + f", e.g. {c['titles']}; spec = "
                         f"{json.dumps(c['spec'], ensure_ascii=False)}" for k, c in enumerate(rc))
                     if rc else "CARD CANDIDATES: none found")
    if "search" in steps:
        parts.append("SEARCH FORMS on the page: " + json.dumps(_forms(read), ensure_ascii=False))
        if ev.get("start"):
            parts.append("SEARCH FORMS on the start page: " + json.dumps(_forms(ev["start"]), ensure_ascii=False))
    nm = near_misses(near)
    if nm:
        parts.append("TIER 1 ALREADY TRIED (deterministic re-locates the acceptance gate refused):\n" + "\n".join(nm))
    parts.append("ELEMENT TABLE (i | area | tag | label | path?query | repeated-container chain):\n" +
                 element_table(read, tool, step, limit=TABLE_ROWS - 20))
    ex = example_text(examples)
    if ex:
        parts.append("EXAMPLES of accepted repairs on other sites (the same language):\n" + ex)
    parts.append(LANGUAGE + "\nIf two steps broke (e.g. renamed result containers AND a renamed search parameter), one "
                            "patch may hold one operation for each.")
    return "\n\n".join(parts)


def patch_from_text(text: str) -> Optional[Dict[str, Any]]:
    """The answer's first JSON object holding ``ops`` (a non-empty list), or None."""
    from anyuse.execution import demo_oracle as O
    for o in O.json_objects(text):
        if isinstance(o, dict) and isinstance(o.get("ops"), list) and o["ops"]:
            return o
    return None


class CerebrasProposer:
    """Tier 2's model: one Cerebras chat call per ``propose`` through the model adapter (``budget_usd``: the adapter's
    run budget). ``adapter`` is injectable (tests). ``design`` 2: ``prompt2`` with ``examples``
    (accepted tier-1 repairs of other tools) and the ladder's tier-1 near misses."""

    def __init__(self, adapter: Any = None, budget_usd: float = 0.05, design: int = 1,
                 examples: Sequence[Dict[str, Any]] = ()):
        self.design, self.examples = int(design), list(examples)
        if adapter is None:
            from anyuse.execution import model_adapter as M
            p = M.PROVIDER_PRESETS["cerebras"]
            price_in, price_out = M._ROLE_PRICES["agent"]
            cfg = M.ModelConfig(provider="cerebras", base_url=p.base_url, model=p.model, api_key_env=p.api_key_env,
                                timeout=90.0, max_tokens=MAX_TOKENS, retries=1, price_in=price_in, price_out=price_out)
            adapter = M.ModelAdapter(cfg, budget=M.RunBudget(budget_usd))
        cfg = getattr(adapter, "config", None)
        host = (urllib.parse.urlsplit(str(getattr(cfg, "base_url", "") or "")).hostname or "").lower()
        if cfg is not None and (any(host == f or host.endswith("." + f) for f in FORBIDDEN_MODEL_HOSTS) or
                                getattr(cfg, "provider", "") != "cerebras"):
            raise ValueError("tier 2: the adapter is not Cerebras (never OpenRouter)")
        self.adapter = adapter
        self.model = getattr(cfg, "model", "")

    def propose(self, tool: Dict[str, Any], step: str, out0: Dict[str, Any], ev: Dict[str, Any],
                anchors: Sequence[Sequence[str]] = (), near: Sequence[Dict[str, Any]] = (),
                steps: Sequence[str] = ()) -> Dict[str, Any]:
        if self.design >= 2:
            msg = prompt2(tool, step, out0, ev, anchors, near, self.examples, steps)
        else:
            msg = prompt(tool, step, out0, ev, anchors)
        r = self.adapter.complete([{"role": "system", "content": "You repair compiled read-only web tools by "
                                                                  "answering a small JSON patch. You never invent "
                                                                  "selectors; you copy them from the candidates."},
                                   {"role": "user", "content": msg}], temperature=0.0, max_tokens=MAX_TOKENS)
        u = getattr(r, "usage", None)
        pt, ct = int(getattr(u, "prompt_tokens", 0) or 0), int(getattr(u, "completion_tokens", 0) or 0)
        usd = (pt * float(getattr(self.adapter, "price_in", 0.0)) + ct * float(getattr(self.adapter, "price_out", 0.0))) \
            / 1_000_000
        return {"text": (r.text or "") if r.ok else "", "error": "" if r.ok else str(r.error)[:200],
                "calls": int(getattr(r, "llm_calls", 0) or 0), "usd": round(usd, 6), "prompt_tokens": pt,
                "completion_tokens": ct, "model": self.model, "prompt_chars": len(msg)}
