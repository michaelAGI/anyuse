"""Page 2 of a proved listing — deterministic pagination for a compiled rung.

Milestone 3 compiled the FIRST page of a result list and stopped there, which is
a real limit: "the next 20 results" is one of the commonest things a person
wants from a list, and going back to the UI for it throws away the rung that was
just proved. This module adds page 2, and only page 2's *mechanism* — it does
not loop, does not fetch "all pages", and does not guess a page count.

Two mechanisms, both read out of evidence the first response already carries:

``param``
    The listing is addressed by a number: ``?page=2``, ``&offset=20``,
    ``&start=21``. Evidence is required from the response itself — a
    ``total_pages`` / ``per_page`` / ``totalCount`` style field, or the page
    number echoed back — because a query key called ``page`` in a URL is a
    *guess* until the server says it paginates. Page 2 is then the SAME
    template with one more parameter, not a chain.

``cursor``
    The listing hands out an opaque continuation: ``next_cursor``,
    ``nextPageToken``, a ``next`` URL. Page 2 genuinely needs two requests —
    fetch page 1, read the cursor out of the response, send it back — so this
    compiles to a two-step :class:`RequestPlan` and reuses the chain executor
    and the chain's ``bind`` dataflow unchanged.

What is deliberately NOT here
-----------------------------
* **No loop.** A compiled rung that walks pages until exhaustion is an
  unbounded crawl against someone else's server. A user who wants page 3 asks
  for page 3.
* **No inferred page size.** Offset pagination needs a stride; it is taken from
  the response (``per_page``/``page_size``/``limit``) or from the recorded
  second request, never assumed to be 10, 20 or 25.
* **No proof by construction.** A derived pagination is a *candidate* exactly
  like a derived template: it is persisted only after ``api_verify`` shows it
  reproduces the UI for a page the compiler never saw.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from anyuse.execution.api_step import (
    PlanStep,
    RequestPlan,
    RequestTemplate,
    json_leaves,
    json_path,
)

#: Query keys that address a page by number. Ordered: the earlier ones are the
#: more explicit, so when a URL carries two we prefer the unambiguous one.
PAGE_PARAM_KEYS = ("page", "page_number", "pagenumber", "pageindex", "page_index", "p", "pg")
#: Query keys that address a page by item offset.
OFFSET_PARAM_KEYS = ("offset", "start", "from", "skip", "startindex", "start_index")

#: Response fields that are EVIDENCE the endpoint paginates by number.
PAGE_EVIDENCE_KEYS = ("total_pages", "totalpages", "page_count", "pagecount", "num_pages",
                      "last_page", "total", "total_count", "totalcount", "total_results",
                      "totalresults", "count", "num_results")
#: Response fields that give the stride for offset pagination.
PAGE_SIZE_KEYS = ("per_page", "perpage", "page_size", "pagesize", "limit", "page_len",
                  "results_per_page", "size")
#: Response fields that carry a continuation token.
CURSOR_KEYS = ("next_cursor", "nextcursor", "next_page_token", "nextpagetoken",
               "continuation_token", "continuationtoken", "next_token", "nexttoken",
               "cursor", "after", "next_page", "nextpage", "next", "next_url", "nexturl")

#: A cursor that looks like a whole URL is only usable if it is same-origin.
_ABS_URL = re.compile(r"^https?://", re.I)


@dataclass
class Pagination:
    """How to ask this endpoint for the next page, and the evidence for it."""
    kind: str = ""                     # "param" | "cursor" | ""
    #: param kind: the query key that carries the page/offset
    key: str = ""
    #: param kind: "page" (1-based index) or "offset" (item index)
    unit: str = ""
    #: param kind: the recorded first-page value, as an int
    first_value: int = 0
    #: param kind: items per page, when the response stated one
    page_size: int = 0
    #: cursor kind: the JSON path in the first response that holds the token
    cursor_path: str = ""
    #: cursor kind: the query key the token is sent back in
    cursor_key: str = ""
    #: what in the response made this more than a guess
    evidence: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in ("", 0)}

    def describe(self) -> str:
        if self.kind == "param":
            return (f"{self.unit} parameter {self.key!r} "
                    f"(first page {self.first_value}"
                    + (f", {self.page_size} per page" if self.page_size else "") + ")")
        if self.kind == "cursor":
            return f"cursor {self.cursor_path!r} sent back as {self.cursor_key!r}"
        return "none"


# ---------------------------------------------------------------------------
# reading the evidence
# ---------------------------------------------------------------------------

def _int(value: Any) -> Optional[int]:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n


def _find_key(data: Any, names: Tuple[str, ...], want_str: bool = False
              ) -> Tuple[str, Any]:
    """First (path, value) whose final key is one of ``names``. ``("", None)`` if none.

    ``names`` is searched in order, so an explicit ``next_cursor`` beats a bare
    ``next`` even when the bare one sits shallower.
    """
    leaves: List[Tuple[str, Any]] = list(json_leaves(data))
    for wanted in names:
        for path, value in leaves:
            key = path.rsplit(".", 1)[-1].split("[")[0].lower()
            if key != wanted:
                continue
            if want_str:
                if isinstance(value, str) and value.strip():
                    return path, value
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return path, value
                continue
            if _int(value) is not None:
                return path, value
    return "", None


def _query_of(template: RequestTemplate) -> Dict[str, str]:
    return dict(template.query or {})


def derive_pagination(template: RequestTemplate, first_response: Any,
                      recorded_url: str = "") -> Tuple[Optional[Pagination], str]:
    """Work out how to ask for page 2. Returns ``(Pagination, "")`` or ``(None, why)``.

    ``first_response`` is the DECODED page-1 payload — the JSON body for an api
    rung, or the embedded payload for an embedded-JSON rung. An HTML-selector
    rung has no structured response to read evidence out of, and is refused
    rather than guessed at.
    """
    if template is None:
        return None, "no template"
    if getattr(template, "extract_kind", "json") == "html":
        return None, ("an HTML-selector rung has no structured response to read a "
                      "page marker from; pagination is not derived for it")

    # --- cursor first: an explicit continuation beats a numeric guess --------
    cursor_path, cursor_value = _find_key(first_response, CURSOR_KEYS, want_str=True)
    if cursor_path:
        key, why = _cursor_key_for(template, cursor_path, cursor_value, recorded_url)
        if key:
            return Pagination(kind="cursor", cursor_path=cursor_path, cursor_key=key,
                              evidence=f"the response carries {cursor_path!r}"), ""
        # fall through to the numeric mechanism, but remember the reason
        cursor_why = why
    else:
        cursor_why = "no continuation token in the response"

    # --- numeric: only with evidence from the RESPONSE -----------------------
    query = _query_of(template)
    recorded_query = dict(urllib.parse.parse_qsl(
        urllib.parse.urlsplit(recorded_url or template.url).query, keep_blank_values=True))

    ev_path, ev_value = _find_key(first_response, PAGE_EVIDENCE_KEYS)
    if not ev_path:
        return None, (f"{cursor_why}, and the response states no page/total count, "
                      f"so a numeric page parameter would be a guess")

    size_path, size_value = _find_key(first_response, PAGE_SIZE_KEYS)
    page_size = _int(size_value) or 0

    for keys, unit in ((PAGE_PARAM_KEYS, "page"), (OFFSET_PARAM_KEYS, "offset")):
        for want in keys:
            for key in list(query) + list(recorded_query):
                if key.lower() != want:
                    continue
                raw = query.get(key, recorded_query.get(key, ""))
                if raw.startswith("{") and raw.endswith("}"):
                    continue          # already a parameter of this template
                first = _int(raw)
                if first is None:
                    continue
                if unit == "offset" and not page_size:
                    return None, (f"the URL carries an offset key {key!r} but the response "
                                  f"states no page size, so the stride for page 2 is unknown")
                return Pagination(kind="param", key=key, unit=unit, first_value=first,
                                  page_size=page_size,
                                  evidence=f"the response states {ev_path}={ev_value!r}"), ""
    return None, (f"{cursor_why}, and no page or offset key was found in the request URL "
                  f"(response evidence was present: {ev_path}={ev_value!r})")


def _cursor_key_for(template: RequestTemplate, cursor_path: str, cursor_value: Any,
                    recorded_url: str) -> Tuple[str, str]:
    """Which query key does the cursor go back in?

    Evidence, in order: the cursor IS a same-origin URL that carries it under
    some key (the server told us the key); otherwise the request already has a
    key of that name; otherwise the leaf key itself, which is the convention
    every cursor API in the wild follows.
    """
    if isinstance(cursor_value, str) and _ABS_URL.match(cursor_value):
        target = urllib.parse.urlsplit(cursor_value)
        here = urllib.parse.urlsplit(template.url)
        if target.netloc.lower() != here.netloc.lower():
            return "", (f"the response's next-page URL points at {target.netloc}, not at "
                        f"{here.netloc}; a compiled rung does not follow a cross-origin hop")
        # the server built the URL; take whichever key changed as the cursor key
        recorded = dict(urllib.parse.parse_qsl(
            urllib.parse.urlsplit(recorded_url or template.url).query, keep_blank_values=True))
        for key, val in urllib.parse.parse_qsl(target.query, keep_blank_values=True):
            if recorded.get(key) != val:
                return key, ""
        return "", "the next-page URL is identical to the recorded one"

    leaf = cursor_path.rsplit(".", 1)[-1].split("[")[0]
    if leaf.lower() in ("next", "next_page", "nextpage") and not isinstance(cursor_value, str):
        # a bare numeric "next" is a page NUMBER, not a token — let the numeric
        # mechanism handle it, with its own evidence requirement
        return "", "the response's 'next' is a number, which is a page index, not a cursor"
    return leaf, ""


# ---------------------------------------------------------------------------
# emitting page 2
# ---------------------------------------------------------------------------

def next_page_params(pagination: Pagination, page: int = 2) -> Dict[str, Any]:
    """The parameter binding that asks for ``page`` (1-based). ``{}`` for a cursor."""
    if pagination.kind != "param" or page < 1:
        return {}
    if pagination.unit == "page":
        return {pagination.key: pagination.first_value + (page - 1)}
    return {pagination.key: pagination.first_value + (page - 1) * pagination.page_size}


def paginated_template(template: RequestTemplate, pagination: Pagination
                       ) -> Tuple[Optional[RequestTemplate], str]:
    """A copy of ``template`` whose page marker is a parameter. ``param`` kind only."""
    if pagination.kind != "param":
        return None, "not a numeric pagination"
    data = template.to_dict()
    query = dict(data.get("query") or {})
    query[pagination.key] = "{%s}" % pagination.key
    data["query"] = query
    data["params"] = sorted(set(list(data.get("params") or []) + [pagination.key]))
    schema = [row for row in (data.get("parameter_schema") or [])
              if f"query:{pagination.key}" not in (row.get("destinations") or [])
              and row.get("name") != pagination.key]
    schema.append({"name": pagination.key, "kind": "input", "type": "integer",
                   "minimum": 1, "maximum": 1_000_000,
                   "destinations": [f"query:{pagination.key}"]})
    data["parameter_schema"] = schema
    data["notes"] = (str(data.get("notes") or "") +
                     f" Paginated by {pagination.describe()}; {pagination.evidence}. "
                     f"One page per call — this rung never loops.")
    out = RequestTemplate.from_dict(data)
    problems = out.problems()
    if problems:
        return None, "; ".join(problems)
    return out, ""


def paginated_plan(template: RequestTemplate, pagination: Pagination
                   ) -> Tuple[Optional[RequestPlan], str]:
    """A two-step plan: fetch page 1, bind its cursor, fetch page 2.

    Reuses the chain executor and its ``bind`` dataflow unchanged — page 2 is
    the same shape of problem as "search, then open the first result", so it
    gets the same machinery and the same all-or-nothing failure behaviour.
    """
    if pagination.kind != "cursor":
        return None, "not a cursor pagination"
    slot = "next_cursor"
    first = RequestTemplate.from_dict(template.to_dict())

    second_data = template.to_dict()
    query = dict(second_data.get("query") or {})
    query[pagination.cursor_key] = "{%s}" % slot
    second_data["query"] = query
    second_data["params"] = sorted(set(list(second_data.get("params") or []) + [slot]))
    second = RequestTemplate.from_dict(second_data)

    plan = RequestPlan(
        steps=[PlanStep(name="page 1", template=first, bind={slot: pagination.cursor_path}),
               PlanStep(name="page 2", template=second)],
        params=sorted(set(first.params)),
        shape=template.shape,
        origin=template.origin,
        notes=(f"page 2 of a proved listing via {pagination.describe()}; "
               f"{pagination.evidence}. Two calls, never a loop: the cursor is read "
               f"from page 1's response and sent back once. If page 2 fails, the "
               f"whole plan fails and the recorded UI path runs, because half a "
               f"listing is not the outcome the recording proved."),
    )
    problems = plan.problems()
    if problems:
        return None, "; ".join(problems)
    return plan, ""


def compile_next_page(template: RequestTemplate, first_response: Any,
                      recorded_url: str = ""
                      ) -> Tuple[Optional[Any], Optional[Pagination], str]:
    """Derive pagination and emit the thing that fetches page 2.

    Returns ``(template_or_plan, pagination, "")`` or ``(None, None, why)``. The
    result is a CANDIDATE: like every other compiled rung it must pass
    ``api_verify`` on a page the compiler never saw before it may be persisted.
    """
    pagination, why = derive_pagination(template, first_response, recorded_url)
    if pagination is None:
        return None, None, why
    if pagination.kind == "param":
        out, why2 = paginated_template(template, pagination)
    else:
        out, why2 = paginated_plan(template, pagination)
    if out is None:
        return None, None, why2
    return out, pagination, ""
