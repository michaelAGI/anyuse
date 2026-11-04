"""The web value check, second part: deterministic value cleaning in the screen, value
crops beside the screen record, and the offline measurement of the cleaning on ``web_valuecheck``'s labelled set.
Offline only: no site, no model.

    web_valuecheck2.py tolabel <R>      the cleaned outputs of the 794-value set that need a label (-> <R>/labels/)
    web_valuecheck2.py measure <R>      the cleaning table: the rules vs rules + cleaning, OpenJev's saved answers

The screen hooks (``execution/web_runtime/web_arch4_live.py``; both OFF by default, so frozen runs stay byte-identical):

* ``VALUE_CLEAN = True``: every candidate of the rules' draw, in the rules' own seeded order, is CLEANED
  (``execution/value_clean.py``) before any check - the OpenJev value check (``value_check="openjev"``) and the
  rules' search check; a dropped one is skipped and the next of the order is drawn (:func:`cleaned_values`); the
  try record keeps the rules' values and every original -> cleaned pair;
* ``VALUE_CROPS = True``: in the seed render the screen already has, a small PNG crop of every candidate's
  element (its box plus a margin) - local CDP reads only, no request, nothing scrolled, only boxes inside the
  viewport; never a page with a login or a personal-data form, a challenge, a block, a login wall, a consent
  wall or an error page; the capture redaction covers e-mail addresses, phone numbers and form fields
  (:func:`capture_crops`); the PNGs go beside the screen record (``screen/crops/<site>/``).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

LABELS_FILE = os.path.join(HERE, "web_valuecheck_cleaned.json")


def _a4():
    import web_arch4_live as A4
    return A4


def _vk():
    import web_valuecheck as VK
    return VK


def _cl():
    from anyuse.execution import value_clean as CL
    return CL


# ---------------------------------------------------------------------------
# 1. the screen hook: cleaning before any check
# ---------------------------------------------------------------------------

def page_site_names(read: Dict[str, Any], site: str) -> List[str]:
    """The page's own site names: the host, the page title, og:site_name."""
    CL = _cl()
    return CL.site_names_of(site, str((read or {}).get("title") or ""), CL.og_site_name(read))


def draw_order(stratum: str, rows: Sequence[Tuple[str, str]], site: str, seed: str) -> List[Tuple[str, Optional[int]]]:
    """The rules' own candidates in their seeded order: shops (candidate, index of its title); others (word, None)."""
    VK = _vk()
    titles = [t for t, _u in rows]
    if stratum == "shop":
        return [(c, i) for c, i in VK.product_pool(titles, site)]
    return [(w, None) for w in VK.word_pool(titles, site, seed)]


def cleaned_values(stratum: str, rows: Sequence[Tuple[str, str]], site: str, seed: str, read: Dict[str, Any],
                   rules_values: Sequence[str], checker: Any, k: int) -> Tuple[List[str], Dict[str, Any]]:
    """``VALUE_CLEAN``: the rules' candidates in the rules' own order, each CLEANED before any check; a dropped
    one, a duplicate and (shops) a word-subset of a kept one are skipped; with a ``checker`` (the OpenJev value
    check) the CLEANED value is asked and kept only on its accept rule; up to ``k`` kept. Returns (kept, what
    the try record adds: the rules' values, every original -> cleaned pair, the verdicts)."""
    CL, VK, W = _cl(), _vk(), _a4().W
    names = page_site_names(read, site)
    titles = [t for t, _u in rows]
    kept: List[str] = []
    cleaning: List[Dict[str, Any]] = []
    asked: List[Dict[str, Any]] = []
    for c, i in draw_order(stratum, rows, site, seed):
        if len(kept) >= k:
            break
        siblings = [t for j, t in enumerate(titles) if j != i]
        cl = CL.clean_value(c, names, siblings)
        rec = {"original": cl.original, "cleaned": cl.cleaned, "dropped": cl.dropped, "why": cl.why, "steps": cl.steps}
        cleaning.append(rec)
        if cl.dropped:
            continue
        v = cl.cleaned
        if W.fold(v) in {W.fold(x) for x in kept}:
            rec["skipped"] = "a duplicate of a kept value"
            continue
        if stratum == "shop" and VK._subset_conflict(v, kept):
            rec["skipped"] = "a word-subset of a kept value"
            continue
        if checker is not None:
            ver = checker.check(VK.candidate_of(stratum, v, rows, site, read, index=i))
            asked.append({"value": v, "original": c, "keep": ver.keep, "by": ver.by, "reason": ver.reason,
                          "fallback": ver.fallback, "answers": ver.answers})
            if not ver.keep:
                continue
        kept.append(v)
    out: Dict[str, Any] = {"value_clean": True, "rules_values": list(rules_values), "cleaning": cleaning,
                           "site_names": names}
    if checker is not None:
        out.update(value_check="openjev", checked=asked, fallbacks=sum(1 for a in asked if a["fallback"]))
    return kept, out


# ---------------------------------------------------------------------------
# 2. value crops: a PNG of every candidate's element in the render the screen already has
# ---------------------------------------------------------------------------

CROP_MARGIN = 8
CROP_MAX_W = 960
CROP_MAX_H = 480
MIN_BOX = 4
#: a page whose ONLY personal-data fields are EMPTY e-mail fields, each the only text
#: field of its form (a newsletter sign-up), may still be captured - the tile's box only, redaction on, nothing
#: typed. A password, a phone field, a filled field or any form asking for more (name, address, a contact form)
#: still refuses the page
CROPS_ALLOW_EMPTY_NEWSLETTER = True
#: personal data in an element's text: an e-mail address, or a phone number (starting with + / 0, >= 9 digits:
#: "2025/2026" or a model number is not one)
EMAIL_TEXT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_TEXT = re.compile(r"(?:\+|(?<![\w.,])0)\d?[\d ()/.-]{6,}\d")


def personal_text(s: str) -> bool:
    s = str(s or "")
    return bool(EMAIL_TEXT.search(s)) or any(sum(ch.isdigit() for ch in m.group(0)) >= 9 for m in PHONE_TEXT.finditer(s))

#: the candidates' boxes (viewport coordinates), whether each is visible and on top, and the page's form fields
#: that ask for a password or personal data (search fields aside). A local read: nothing is sent.
CROPS_JS = r"""
((targets) => {
  const norm = u => { try { const x = new URL(u, document.baseURI); x.hash = ''; return x.href; } catch (e) { return ''; } };
  const vis = n => { const r = n.getBoundingClientRect(); if (!r.width || !r.height) return false;
    const cs = getComputedStyle(n);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity || '1') > 0.05; };
  const txt = n => (n.innerText || n.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
  const anchors = Array.from(document.querySelectorAll('a[href]'));
  const boxes = [];
  for (const t of targets) {
    const want = norm(t.href), title = String(t.title || '').toLowerCase();
    let best = null, score = -1;
    for (const a of anchors) {
      if (norm(a.getAttribute('href')) !== want) continue;
      const at = txt(a).toLowerCase();
      const s = (title && at === title ? 4 : (title && at && (at.includes(title) || title.includes(at)) ? 2 : 0)) +
                (vis(a) ? 1 : 0);
      if (s > score) { best = a; score = s; }
    }
    if (!best) { boxes.push({found: false}); continue; }
    const r = best.getBoundingClientRect();
    let covered = false;
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (vis(best) && cx >= 0 && cy >= 0 && cx < innerWidth && cy < innerHeight) {
      const top = document.elementFromPoint(cx, cy);
      covered = !!top && !(top === best || best.contains(top) || top.contains(best));
    }
    boxes.push({found: true, visible: vis(best), covered: covered, x: r.left, y: r.top, w: r.width, h: r.height,
                text: txt(best).slice(0, 200)});
  }
  const SEARCHY = /(^|[^a-z])(search|suche|such|query|keywords?|zoek|zoeken|recherche|busca|buscar|cerca|ricerca|sok|sök|søg|søk|haku|szukaj|q|s)([^a-z]|$)/i;
  const PD = /(^|[^a-z])(e-?mail|mail|phone|telefon|telephone|tel|mobile|mobil|handy|name|firstname|lastname|fullname|username|vorname|nachname|given-name|family-name|street|strasse|straße|address|adresse|address-line1|zip|zipcode|postal|postal-code|postcode|plz|city|birthday|bday|geburtsdatum|iban|cc-number|cc-name|cc-exp|cc-csc|cardnumber|cvc|cvv)([^a-z]|$)/i;
  const MAILISH = /(^|[^a-z])(e-?mail|mail|newsletter)([^a-z]|$)/i;
  const SKIPT = ['hidden', 'submit', 'button', 'image', 'reset', 'checkbox', 'radio', 'range', 'color', 'file'];
  let password = 0, personal = 0, newsletter = 0; const fields = [];
  for (const i of document.querySelectorAll('input, textarea, select')) {
    if (!vis(i)) continue;
    const type = (i.getAttribute('type') || 'text').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'checkbox', 'radio', 'range', 'color', 'file'].includes(type)) continue;
    const form = i.closest('form');
    const idn = [i.getAttribute('name'), i.id, i.getAttribute('autocomplete'), i.getAttribute('placeholder'),
                 i.getAttribute('aria-label')].filter(Boolean).join(' ');
    if (type === 'search' || i.closest('[role=search]') || SEARCHY.test(i.getAttribute('name') || '') ||
        SEARCHY.test(i.id || '') || (form && SEARCHY.test(form.getAttribute('action') || ''))) continue;
    if (type === 'password') { password++; fields.push('password'); continue; }
    if (type === 'email' || type === 'tel' || PD.test(idn)) {
      personal++; fields.push(type + ':' + String(i.getAttribute('name') || i.id || '').slice(0, 30));
      // an EMPTY e-mail field that is the only text field of its form (a newsletter sign-up): owner rule
      // 2026-11-03, the page may still be captured (the tile's box only, redaction on, never typed into)
      const others = form ? Array.from(form.querySelectorAll('input, textarea, select')).filter(o => o !== i &&
          vis(o) && !SKIPT.includes((o.getAttribute('type') || 'text').toLowerCase())).length : 0;
      if (type !== 'tel' && (type === 'email' || MAILISH.test(idn)) && !String(i.value || '').trim() && others === 0)
        newsletter++;
    }
  }
  return {vw: innerWidth, vh: innerHeight, sx: scrollX, sy: scrollY, boxes: boxes, password: password,
          personal: personal, newsletter: newsletter, fields: fields.slice(0, 10)};
})(%s)
"""


def crop_targets(read: Dict[str, Any], site: str, stratum: str, seed: str) -> List[Dict[str, str]]:
    """The candidates of this render and their elements (PURE, the screen's own rules): shops - the pool's
    tiles (their link and title); other sites - the pool's words (the first result link naming the word)."""
    A4, VK = _a4(), _vk()
    out: List[Dict[str, str]] = []
    if stratum == "shop":
        rows = A4.product_rows(read, cluster_only=True)
        for c, i in VK.product_pool([t for t, _u in rows], site):
            out.append({"value": c, "href": rows[i][1], "title": rows[i][0]})
        return out
    rows, _how = A4.result_titles(str(read.get("dom") or ""), str(read.get("url") or ""), seed)
    rows = [(t, u) for t, u in rows if len(t.split()) >= 3]
    for w in VK.word_pool([t for t, _u in rows], site, seed):
        naming = VK._titles_naming(w, rows)
        if naming:
            out.append({"value": w, "href": naming[0][1], "title": naming[0][0]})
        else:
            out.append({"value": w, "href": "", "title": ""})
    return out


def page_refusal(read: Dict[str, Any], network: Sequence[Dict[str, Any]] = ()) -> str:
    """Why this render is never captured ("" = it may be): the capture rules of the screenshot dataset - a
    challenge page (the OpenJev challenge guard), a block (the bench's block rules), and what the block detector
    reads as a login wall, a consent wall or an error page (``execution/web_runtime/shots.classify``)."""
    from anyuse.execution.openjev import challenge_guard
    import shots
    if read.get("blocked"):
        return f"blocked: {str(read['blocked'])[:80]}"
    why = challenge_guard({"url": read.get("url"), "text": read.get("text"), "title": read.get("title")})
    if why:
        return f"challenge: {why[:80]}"
    cls = shots.classify(str(read.get("text") or ""), str(read.get("url") or ""), list(network or []))
    if cls:
        return {"never": "a challenge or block page", "login": "a login wall", "consent": "a consent wall",
                "error": "an error page"}.get(cls, cls)
    return ""


def box_skip(box: Dict[str, Any], vw: float, vh: float) -> str:
    """Why this candidate's element gets no crop ("" = it gets one)."""
    if not box or not box.get("found"):
        return "no element"
    if not box.get("visible") or float(box.get("w") or 0) < MIN_BOX or float(box.get("h") or 0) < MIN_BOX:
        return "hidden"
    if box.get("covered"):
        return "covered by another element"
    x, y, w, h = (float(box.get(k) or 0) for k in ("x", "y", "w", "h"))
    if x < 0 or y < 0 or x + w > vw or y + h > vh:
        return "off-screen"
    if personal_text(str(box.get("text") or "")):
        return "personal data in its text"
    return ""


def crop_clip(box: Dict[str, Any], info: Dict[str, Any], margin: int = CROP_MARGIN) -> Dict[str, float]:
    """The box plus the margin, inside the viewport, at most CROP_MAX_W x CROP_MAX_H, in page coordinates."""
    vw, vh = float(info.get("vw") or 0), float(info.get("vh") or 0)
    x0 = max(0.0, float(box["x"]) - margin)
    y0 = max(0.0, float(box["y"]) - margin)
    x1 = min(vw, float(box["x"]) + float(box["w"]) + margin, x0 + CROP_MAX_W)
    y1 = min(vh, float(box["y"]) + float(box["h"]) + margin, y0 + CROP_MAX_H)
    return {"x": round(x0 + float(info.get("sx") or 0), 1), "y": round(y0 + float(info.get("sy") or 0), 1),
            "width": round(x1 - x0, 1), "height": round(y1 - y0, 1), "scale": 1}


async def capture_crops(page, read: Dict[str, Any], site: str, stratum: str, seed: str) -> Dict[str, Any]:
    """The crops of one render (module docstring): local CDP reads of the open page only. Returns
    {"page": "" or why the page is refused, "crops": [{value, href, box, clip, png_b64} or {value, skipped}]}."""
    from anyuse.execution import web_arch_run as RR
    targets = crop_targets(read, site, stratum, seed)
    try:
        net = await page.network(want_bodies=False)
    except Exception:                                  # noqa: BLE001 - no request log: the text rules only
        net = []
    why = page_refusal(read, net)
    if not why and targets:
        info = await page.js(CROPS_JS % json.dumps([{"href": t["href"], "title": t["title"]} for t in targets]))
        if not isinstance(info, dict) or len(info.get("boxes") or []) != len(targets):
            why = "no box read"
        elif info.get("password"):
            why = "a login form (a password field)"
        elif info.get("personal") and not (CROPS_ALLOW_EMPTY_NEWSLETTER and
                                           int(info.get("personal") or 0) == int(info.get("newsletter") or 0)):
            why = f"a personal-data form ({', '.join(info.get('fields') or [])[:80]})"
    else:
        info = {}
    if why:
        return {"page": why, "crops": [{"value": t["value"], "href": t["href"], "skipped": "page refused"}
                                        for t in targets]}
    crops: List[Dict[str, Any]] = []
    vw, vh = float(info.get("vw") or 0), float(info.get("vh") or 0)
    redacted = False
    try:
        await page.js(RR.REDACT_JS)
        redacted = True
        for t, b in zip(targets, info.get("boxes") or []):
            skip = box_skip(b, vw, vh) if t["href"] else "no element"
            if skip:
                crops.append({"value": t["value"], "href": t["href"], "skipped": skip})
                continue
            clip = crop_clip(b, info)
            res = await page.call("Page.captureScreenshot", {"format": "png", "clip": clip,
                                                             "captureBeyondViewport": False, "fromSurface": True})
            data = (res or {}).get("data") or ""
            box = {k: round(float(b[k]), 1) for k in ("x", "y", "w", "h")}
            if not data:
                crops.append({"value": t["value"], "href": t["href"], "box": box, "skipped": "capture failed"})
                continue
            crops.append({"value": t["value"], "href": t["href"], "box": box, "clip": clip, "png_b64": data})
    finally:
        if redacted:
            try:
                await page.js(RR.REDACT_OFF_JS)
            except Exception:                          # noqa: BLE001
                pass
    return {"page": "", "viewport": [vw, vh], "crops": crops}


async def render_with_crops(page, render: Callable, start_url: str, seed: str, templates: Sequence[str], site: str,
                            stratum: str) -> Dict[str, Any]:
    """The screen's seed render (``render``: ``web_arch4_live.render_search``), then the crops of its candidates
    on the same open page (``r["value_crops"]``, popped by the screen before anything is dumped)."""
    r = await render(page, start_url, seed, templates)
    try:
        r["value_crops"] = await capture_crops(page, r, site, stratum, seed)
    except Exception as exc:                           # noqa: BLE001 - a crop never fails the screen
        r["value_crops"] = {"page": f"capture failed: {type(exc).__name__}", "crops": []}
    return r


def save_crops(crops: Optional[Dict[str, Any]], crops_dir: str, site: str, try_index: int) -> Dict[str, Any]:
    """The crops' PNGs written beside the screen record (``<crops_dir>/<site>/t<try>_<k>.png``); the try record
    gets each crop's file (relative to the screen record's folder), size and digest, or why it has none."""
    crops = crops or {"page": "no capture", "crops": []}
    out: List[Dict[str, Any]] = []
    d = os.path.join(crops_dir, _a4().slug(site)) if crops_dir else ""
    for k, c in enumerate(crops.get("crops") or []):
        c = dict(c)
        data = c.pop("png_b64", "")
        if data:
            raw = base64.b64decode(data)
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                c["skipped"] = "not a PNG"
            elif not d:
                c["skipped"] = "no crops folder"
            else:
                os.makedirs(d, exist_ok=True)
                name = f"t{try_index}_{k:02d}.png"
                with open(os.path.join(d, name), "wb") as fh:
                    fh.write(raw)
                c.update(file=os.path.join(os.path.basename(crops_dir.rstrip("/")), os.path.basename(d), name),
                         bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()[:16])
        out.append(c)
    return {"crops_page": crops.get("page") or "", "crops": out}


# ---------------------------------------------------------------------------
# 3. the offline measurement on ``web_valuecheck``'s labelled set (DEV evidence: the set was seen)
# ---------------------------------------------------------------------------

REAL_HOME = os.environ.get("ANYUSE_REAL_HOME") or os.path.expanduser("~")
RESEARCH = os.path.join(REAL_HOME, "Desktop", "analysis", "research")
#: the benches' screen records read for each site's page title and each try's list titles (never written)
CONTEXT_SOURCES = ("web-arch4b-2026-11-03/screen/screen.json", "web-arch4b-2026-11-03/dev/screen/screen.json",
                   "web-arch4b-2026-11-03/dev/screen/screen_pass1.json", "web-arch4-2026-11-03/screen/screen.json",
                   "web-arch4-2026-11-03/dev/screen/screen.json", "web-arch3-2026-11-03/screen/screen.json")
PASS1 = os.path.join(RESEARCH, "web-valuecheck-2026-11-03")
T_FROZEN = 0.99


def _load(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def site_titles(research: str = RESEARCH) -> Dict[str, str]:
    """Per site: the home page's title as the benches' screens kept it (``snippets.title``) - the site names'
    source for the offline measurement (no og:site_name was recorded). Stored in ``LABELS_FILE``."""
    titles: Dict[str, str] = {}
    for f in CONTEXT_SOURCES:
        d = _load(os.path.join(research, f), {}) or {}
        for site, rec in (d.get("sites") or {}).items():
            t = (rec.get("snippets") or {}).get("title")
            if t and site not in titles:
                titles[site] = str(t)
    return titles


def try_titles(research: str = RESEARCH) -> Dict[str, List[str]]:
    """Per (site, seed): the try's list titles (a cross-check of the siblings; the measurement uses the set's
    own neighbours, which the committed set keeps)."""
    tries: Dict[str, List[str]] = {}
    for f in CONTEXT_SOURCES:
        d = _load(os.path.join(research, f), {}) or {}
        for site, rec in (d.get("sites") or {}).items():
            for tr in rec.get("value_tries") or []:
                tries.setdefault(f"{site}\t{tr.get('seed')}", [str(x) for x in tr.get("titles") or []])
    return tries


def clean_item(it: Dict[str, Any], titles: Dict[str, str], siblings: Optional[Sequence[str]] = None
               ) -> Dict[str, Any]:
    """PURE: one labelled value cleaned with its site's names (the host and the recorded home-page title) and
    its list's titles (default: the set's own neighbours of the value)."""
    CL = _cl()
    names = CL.site_names_of(it["site"], titles.get(it["site"], ""))
    sib = [x for x in (siblings if siblings is not None else it.get("neighbours") or []) if x != it["value"]]
    c = CL.clean_value(it["value"], names, sib)
    return {"id": it["id"], "original": c.original, "cleaned": c.cleaned, "dropped": c.dropped, "why": c.why,
            "steps": c.steps, "changed": c.changed}


def to_label(items: Sequence[Dict[str, Any]], cleaned: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The cleaned outputs that need a new label: changed and not dropped."""
    out = []
    for it in items:
        c = cleaned[it["id"]]
        if c["changed"] and not c["dropped"]:
            out.append({"id": it["id"], "site": it["site"], "split": it["split"], "old_label": it["label"],
                        "original": it["value"], "cleaned": c["cleaned"], "steps": c["steps"],
                        "neighbours": it.get("neighbours"), "lang": it.get("lang")})
    return out


def outcome(it: Dict[str, Any], c: Dict[str, Any], labels: Dict[str, Dict[str, Any]]) -> Tuple[str, Optional[str]]:
    """(the transition, the label after cleaning): bad -> recovered / dropped / still_bad; good -> unchanged /
    changed_good / damaged; "unlabelled" when a changed value has no label for its cleaned string."""
    good = it["label"] == "product"
    if c["dropped"]:
        return ("good_dropped" if good else "bad_dropped"), None
    if not c["changed"]:
        return ("good_unchanged" if good else "bad_unchanged"), it["label"]
    lab = labels.get(it["id"])
    if not lab or lab.get("cleaned") != c["cleaned"]:
        return "unlabelled", None
    new = lab["label"]
    if good:
        return ("good_changed_good" if new == "product" else "good_damaged"), new
    return ("bad_recovered" if new == "product" else "bad_changed_still_bad"), new


def table(items: Sequence[Dict[str, Any]], cleaned: Dict[str, Dict[str, Any]], labels: Dict[str, Dict[str, Any]]
          ) -> Dict[str, Any]:
    """The rules (as drawn: every value kept) vs rules + cleaning (a dropped value is not kept; a cleaned one
    counts with its new label): kept, bad let through, good lost, precision; and the transitions."""
    tr = Counter()
    kept_c = bad_c = good_c = 0
    for it in items:
        c = cleaned[it["id"]]
        o, lab = outcome(it, c, labels)
        tr[o] += 1
        if o == "unlabelled":
            continue
        if not c["dropped"]:
            kept_c += 1
            if lab == "product":
                good_c += 1
            else:
                bad_c += 1
    n = len(items)
    good = sum(1 for i in items if i["label"] == "product")
    bad = n - good
    return {"n": n, "good": good, "bad": bad,
            "rules": {"kept": n, "bad_let_through": bad, "good_kept": good, "precision": round(good / n, 4) if n else None},
            "rules_cleaning": {"kept": kept_c, "bad_let_through": bad_c, "good_kept": good_c,
                               "precision": round(good_c / kept_c, 4) if kept_c else None,
                               "good_lost": good - (good_c - tr["bad_recovered"])},
            "transitions": dict(sorted(tr.items()))}


def openjev_saved(items: Sequence[Dict[str, Any]], cleaned: Dict[str, Dict[str, Any]],
                  labels: Dict[str, Dict[str, Any]], answers: Dict[str, List[Dict[str, Any]]], t: float = T_FROZEN
                  ) -> Dict[str, Any]:
    """OpenJev's SAVED answers only (``web_valuecheck``'s both orders; no new inference): (a) a cleaned string that equals a
    string OpenJev already judged (another value of the set; the same site first) - that string's verdict at the
    frozen t; (b) "OpenJev + cleaning" on the values whose answer is known: unchanged values (their own answers),
    dropped values (no question), cleaned values with a judged match; the rest needs new inference (counted)."""
    VK = _vk()
    by_value: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        by_value.setdefault(" ".join(it["value"].split()).casefold(), []).append(it)
    matches = []
    keep: Dict[str, Optional[bool]] = {}
    for it in items:
        c = cleaned[it["id"]]
        if c["dropped"]:
            keep[it["id"]] = False
            continue
        if not c["changed"]:
            keep[it["id"]] = VK.keep_of(answers.get(it["id"]), t) if answers.get(it["id"]) else None
            continue
        hits = [o for o in by_value.get(" ".join(c["cleaned"].split()).casefold(), []) if o["id"] != it["id"]]
        hits.sort(key=lambda o: (o["site"] != it["site"], o["id"]))
        if hits and answers.get(hits[0]["id"]):
            h = hits[0]
            k = VK.keep_of(answers[h["id"]], t)
            keep[it["id"]] = k
            lab = (labels.get(it["id"]) or {}).get("label")
            matches.append({"id": it["id"], "site": it["site"], "original": it["value"], "cleaned": c["cleaned"],
                            "label_after": lab, "judged_as": h["id"], "judged_site": h["site"],
                            "same_site": h["site"] == it["site"],
                            "answers": [(a.get("label"), a.get("confidence")) for a in answers[h["id"]]],
                            "kept_at_t": k})
        else:
            keep[it["id"]] = None
    known = [it for it in items if keep[it["id"]] is not None]
    kept = [it for it in known if keep[it["id"]]]

    def lab_after(it):
        c = cleaned[it["id"]]
        if not c["changed"]:
            return it["label"]
        return (labels.get(it["id"]) or {}).get("label")
    kb = sum(1 for it in kept if lab_after(it) != "product")
    oj_only = [it for it in known if answers.get(it["id"])]
    ojk = [it for it in oj_only if VK.keep_of(answers[it["id"]], t)]
    # OpenJev's own key error (bad values it kept at t, on the ORIGINAL strings) by what cleaning does to them;
    # and the bounds of "OpenJev + cleaning"'s bad values let through without new inference: at least the
    # unchanged bad ones OpenJev keeps (the same string, the same saved answer), at most those plus every
    # changed value that is still bad and was not judged (worst case: all kept)
    through = [it for it in items if answers.get(it["id"]) and VK.keep_of(answers[it["id"]], t) and
               it["label"] != "product"]
    by_out = Counter(outcome(it, cleaned[it["id"]], labels)[0] for it in through)
    certain = sum(1 for it in items if keep[it["id"]] and lab_after(it) != "product")
    open_bad = sum(1 for it in items if keep[it["id"]] is None and lab_after(it) != "product")
    return {"t": t, "matches": matches,
            "openjev_bad_through": {"n": len(through), "by_cleaning": dict(sorted(by_out.items()))},
            "openjev_cleaning_bad_through_bounds": [certain, certain + open_bad],
            "recovered_or_changed_good_needing_inference": sum(1 for it in items if keep[it["id"]] is None and
                                                               lab_after(it) == "product"),
            "known": len(known), "needs_inference": len(items) - len(known),
            "openjev_cleaning_on_known": {"kept": len(kept), "bad_let_through": kb,
                                          "precision": round((len(kept) - kb) / len(kept), 4) if kept else None},
            "openjev_alone_on_known": {"kept": len(ojk), "bad_let_through": sum(1 for it in ojk if it["label"] != "product"),
                                       "precision": round(sum(1 for it in ojk if it["label"] == "product") / len(ojk), 4)
                                       if ojk else None},
            "unknown_ids": [it["id"] for it in items if keep[it["id"]] is None]}


def load_labels(path: str = LABELS_FILE) -> Dict[str, Dict[str, Any]]:
    return (_load(path, {}) or {}).get("labels") or {}


def load_titles(path: str = LABELS_FILE) -> Dict[str, str]:
    return (_load(path, {}) or {}).get("site_titles") or {}


def clean_set(items: Sequence[Dict[str, Any]], titles: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    return {it["id"]: clean_item(it, titles) for it in items}


def cmd_tolabel(root: str) -> int:
    VK = _vk()
    s = VK.load_set()
    titles = site_titles()
    titles = {k: v for k, v in titles.items() if k in {it["site"] for it in s["items"]}}
    cleaned = clean_set(s["items"], titles)
    # the cross-check: the try's full list titles as siblings give the same cleaning
    tries = try_titles()
    differ = [it["id"] for it in s["items"] if tries.get(f"{it['site']}\t{it.get('seed')}") and
              clean_item(it, titles, tries[f"{it['site']}\t{it.get('seed')}"]) != cleaned[it["id"]]]
    rows = to_label(s["items"], cleaned)
    os.makedirs(os.path.join(root, "labels"), exist_ok=True)
    with open(os.path.join(root, "labels", "to_label.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(root, "labels", "cleaned_all.json"), "w", encoding="utf-8") as fh:
        json.dump(cleaned, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(root, "labels", "site_titles.json"), "w", encoding="utf-8") as fh:
        json.dump(titles, fh, ensure_ascii=False, indent=1)
    print(json.dumps({"items": len(s["items"]), "changed": sum(1 for c in cleaned.values() if c["changed"]),
                      "dropped": sum(1 for c in cleaned.values() if c["dropped"]), "to_label": len(rows),
                      "sites_with_title": len(titles), "differ_with_full_list_titles": differ}))
    return 0


def measure(items: Sequence[Dict[str, Any]], cleaned: Dict[str, Dict[str, Any]], labels: Dict[str, Dict[str, Any]],
            answers: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """PURE: the cleaning table per split and stratum, the changed values, OpenJev's saved answers."""
    out: Dict[str, Any] = {}
    for sp in ("all", "selection", "check"):
        sub = [i for i in items if sp == "all" or i["split"] == sp]
        out[sp] = {"all": table(sub, cleaned, labels)}
        for st in ("shop", "other"):
            out[sp][st] = table([i for i in sub if i["stratum"] == st], cleaned, labels)
    rows = []
    for it in items:
        c = cleaned[it["id"]]
        if c["changed"]:
            o, lab = outcome(it, c, labels)
            rows.append({"id": it["id"], "split": it["split"], "site": it["site"], "label": it["label"],
                         "original": it["value"], "cleaned": c["cleaned"], "dropped": c["dropped"], "why": c["why"],
                         "steps": c["steps"], "label_after": lab, "outcome": o})
    out["changed"] = rows
    out["openjev_saved"] = {sp: openjev_saved([i for i in items if sp == "all" or i["split"] == sp], cleaned, labels,
                                              answers) for sp in ("all", "check")}
    return out


def cmd_measure(root: str) -> int:
    VK = _vk()
    s = VK.load_set()
    cleaned = clean_set(s["items"], load_titles())
    labels = load_labels()
    answers = VK.load_answers(os.path.join(PASS1, "runs", "label_answers.jsonl"))
    r = measure(s["items"], cleaned, labels, answers)
    r["labels_file"] = os.path.relpath(LABELS_FILE, os.path.dirname(os.path.dirname(HERE)))
    r["answers_file"] = os.path.join(PASS1, "runs", "label_answers.jsonl")
    VK._save(os.path.join(root, "results", "cleaning.json"), r)
    for sp in ("all", "selection", "check"):
        print(sp, json.dumps(r[sp]["all"], ensure_ascii=False))
    print("openjev saved (check):", json.dumps({k: v for k, v in r["openjev_saved"]["check"].items()
                                                 if k not in ("matches", "unknown_ids")}))
    unl = sum(1 for x in r["changed"] if x["outcome"] == "unlabelled")
    return 0 if not unl else 6


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd")
    ap.add_argument("root")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    if args.cmd == "tolabel":
        return cmd_tolabel(root)
    if args.cmd == "measure":
        return cmd_measure(root)
    raise SystemExit(f"unknown command {args.cmd!r}")


if __name__ == "__main__":
    raise SystemExit(main())
