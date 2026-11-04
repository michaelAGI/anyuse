"""Synthetic screen renders for the web value check, offline.

Two made-up sites (``www.shop.test``, ``www.mag.test``; no real host) whose seed, value and empty-term renders
feed ``execution/web_runtime/web_arch4_live.screen_values`` through fakes of ``render_search`` / ``in_page``. The shop's seed
page mixes clean product tiles with the value shapes the frozen rules let through (a CTA prefix,
a brand tile, a category tile, a price label). ``rules_golden.json`` is the output of the screen's value drawing
on these renders before the value check existed: ``value_check="rules"`` must
reproduce it exactly.
"""
from __future__ import annotations

import asyncio
import html
import re
from typing import Any, Dict, List, Sequence, Tuple

SHOP = "www.shop.test"
MAG = "www.mag.test"

#: the shop's seed page (seed "Hoekbank"): (title, path, price)
SHOP_TILES: List[Tuple[str, str, str]] = [
    ("Hoekbank Regalo", "/p/hoekbank-regalo", "€ 899,-"),
    ("Se alle størrelser på produktet JANET", "/p/janet-water-repellent-down-jacket", "€ 1.299,-"),
    ("SCITEC NUTRITION", "/p/scitec-nutrition", "€ 29,95"),
    ("Vloerkleed Winnipeg Rood", "/p/vloerkleed-winnipeg-rood", "€ 149,-"),
    ("3 zits bank Carino Bruin", "/p/3-zits-bank-carino-bruin", "€ 699,-"),
    ("Poef Winnipeg Multicolor", "/p/poef-winnipeg-multicolor", "€ 79,-"),
    ("FAST CROP RUNWAY PINK NIKE Ursprungligt pris: Reapris (- )", "/p/fast-crop-runway-pink", "€ 39,-"),
    ("Lage vitrinekast Marsciano Terra", "/p/lage-vitrinekast-marsciano-terra", "€ 349,-"),
    ("Fauteuil Demenza", "/p/fauteuil-demenza", "€ 299,-"),
    ("Vin & Champagne", "/p/vin-champagne", "€ 99,-"),
    ("Hoekbank Carino Rechts", "/p/hoekbank-carino-rechts", "€ 1.099,-"),
    ("Spot Atlanta Zwart", "/p/spot-atlanta-zwart", "€ 24,95"),
]

#: the magazine's seed page (seed "Garten"): article titles
MAG_TITLES: List[str] = [
    "Garten im Herbst: winterharte Rosen pflanzen",
    "Winterharte Stauden für den schattigen Garten",
    "Tomaten im Garten richtig düngen",
    "Tomaten im Kübel: die besten Sorten für den Garten",
    "Rasen im Garten vertikutieren: wann und wie",
    "Rasen düngen im Garten im Frühjahr",
    "Hochbeet im Garten anlegen: Schritt für Schritt",
    "Hochbeet bepflanzen: Gemüse für den Garten",
    "Garten winterfest machen: die Checkliste",
    "Oktober im Garten: was jetzt zu tun ist",
]


def slug(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:48] or "x"


def shop_read(url: str, tiles: Sequence[Tuple[str, str, str]], text: str = "") -> Dict[str, Any]:
    els: List[Dict[str, Any]] = [
        {"i": "1", "tag": "a", "text": "Home", "area": "header", "href": f"https://{SHOP}/"},
        {"i": "2", "tag": "a", "text": "Sofa's", "area": "nav", "href": f"https://{SHOP}/c/sofas"},
    ]
    for n, (title, path, price) in enumerate(tiles):
        href = f"https://{SHOP}{path}"
        els.append({"i": str(10 + 2 * n), "tag": "a", "text": title, "area": "body", "href": href,
                    "ctx": f"{title} {price}"})
        els.append({"i": str(11 + 2 * n), "tag": "a", "text": "Bekijk product", "area": "body", "href": href,
                    "ctx": f"{title} {price}"})
    els.append({"i": "99", "tag": "a", "text": "Klantenservice", "area": "footer", "href": f"https://{SHOP}/service"})
    body = " ".join(f"{t} {p}" for t, _u, p in tiles)
    dom = (f'<html lang="nl"><head><title>Zoeken</title></head><body><main>'
           + "".join(f'<div class="tile"><a href="https://{SHOP}{u}">{html.escape(t)}</a><span>{p}</span></div>'
                     for t, u, p in tiles) + f"<p>{html.escape(text)}</p></main></body></html>")
    return {"url": url, "title": "Zoeken", "headings": [], "h1": [], "elements": els, "text": (text + " " + body).strip(),
            "main_text": (text + " " + body).strip(), "facts": [], "priced_links": [], "meta": {}, "microdata": {},
            "link_sigs": {}, "dom": dom, "blocked": "", "how": "url", "fresh": {"context": True}}


def mag_read(url: str, titles: Sequence[str], text: str = "") -> Dict[str, Any]:
    links = "".join(f'<li><a href="https://{MAG}/artikel/{slug(t)}">{html.escape(t)}</a></li>' for t in titles)
    dom = (f'<html lang="de"><head><title>Suche</title></head><body><header><nav><a href="https://{MAG}/">Start</a>'
           f'<a href="https://{MAG}/kontakt">Kontakt</a></nav></header><main><p>{html.escape(text)}</p><ul>{links}</ul>'
           f'</main><footer><a href="https://{MAG}/impressum">Impressum</a></footer></body></html>')
    els = [{"i": str(n), "tag": "a", "text": t, "area": "body", "href": f"https://{MAG}/artikel/{slug(t)}"}
           for n, t in enumerate(titles)]
    return {"url": url, "title": "Suche", "headings": [], "h1": [], "elements": els,
            "text": (text + " " + " ".join(titles)).strip(), "main_text": (text + " " + " ".join(titles)).strip(),
            "facts": [], "priced_links": [], "dom": dom, "blocked": "", "how": "url", "fresh": {"context": True}}


def shop_rec() -> Dict[str, Any]:
    return {"stratum": "shop", "search_urls": [f"https://{SHOP}/zoeken?q={{q}}"], "tile_seeds": ["Hoekbank"],
            "categories": ["Sofa's", "Verlichting"], "item_terms": ["Vloerkleed", "Fauteuil"]}


def mag_rec() -> Dict[str, Any]:
    return {"stratum": "other", "search_urls": [f"https://{MAG}/?s={{q}}"], "tile_seeds": [],
            "categories": ["Garten", "Balkon"], "item_terms": ["Tomaten", "Rasen"]}


class Renders:
    """The fake ``render_search``: the seed page for the site's first seed, the site's "no results" page for
    its seeded empty terms (``empties``), else a result page naming the searched value."""

    def __init__(self, a4, empties: Dict[str, Sequence[str]]):
        self.a4, self.empties, self.calls = a4, {k: set(v) for k, v in empties.items()}, []

    async def __call__(self, page, start_url: str, value: str, templates: Sequence[str]) -> Dict[str, Any]:
        site = start_url.split("/")[2]
        self.calls.append((site, value))
        url = templates[0].replace("{q}", value.replace(" ", "+")) if templates else start_url
        if site == SHOP:
            if value in self.empties.get(site, ()):
                return shop_read(url, [], text=f"Geen resultaten gevonden voor {value}")
            if value == "Hoekbank":
                return shop_read(url, SHOP_TILES)
            return shop_read(url, [(value, "/p/" + slug(value), "€ 10,-"), ("Kussen Lino Groen", "/p/kussen-lino-groen",
                                                                          "€ 19,-"),
                                   ("Plaid Mora Grijs", "/p/plaid-mora-grijs", "€ 29,-")])
        if value in self.empties.get(site, ()):
            return mag_read(url, [], text=f"Leider nichts gefunden für {value}")
        if value == "Garten":
            return mag_read(url, MAG_TITLES)
        return mag_read(url, [f"{value} im Überblick: alles Wichtige", f"Neu: {value} und mehr im Test",
                              f"Ratgeber {value}: die häufigsten Fragen", "Kontaktformular und Newsletter hier"])


class Ctx:
    """The screen's per-site context, faked: an ample share, a budget whose check never trips."""

    class _B:
        def check(self):
            return None

    def __init__(self):
        self.budget = self._B()

    def left(self):
        return 200

    def bytes_left(self):
        return 50 * 1024 * 1024


def run_screen_values(a4, site: str, rec: Dict[str, Any], **kw) -> Tuple[Dict[str, Any], List[Tuple[str, str]]]:
    """``screen_values`` on the synthetic site with every browser call faked; returns (record, renders)."""
    empties = {SHOP: a4.empty_terms(SHOP), MAG: a4.empty_terms(MAG)}
    fake = Renders(a4, empties)
    saved = (a4.render_search, a4.in_page, a4.dump)
    try:
        a4.render_search = fake
        a4.in_page = lambda ctx, endpoint, fn: asyncio.run(fn(None))
        a4.dump = lambda *a, **k: None
        out = a4.screen_values(Ctx(), "http://127.0.0.1:1", site, rec["stratum"], dict(rec), False, **kw)
    finally:
        a4.render_search, a4.in_page, a4.dump = saved
    return out, list(fake.calls)
