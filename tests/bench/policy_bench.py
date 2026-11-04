"""A/B bench: what the traffic policy lets a run do, OLD tree against NEW tree, offline.

Three groups of cases, each decided by the product's own gates with no browser, network or model:

* ``gov``: government, ``.int``, EU-institution and military hosts with the settings at their defaults
  (``web_scope.contact_refusal``, ``harness_tools.gov_refusal``, the run's robots gate under
  ``ANYUSE_ROBOTS=ignore``), then the same with ``ANYUSE_REFUSE_GOV_HOSTS=on``; a never-contact host in both.
* ``signed_in``: an offline shop whose robots.txt disallows its account, cart, checkout, login and sale pages. The
  open-task browser (``web_flow.FlowBrowserTools``) logs in with a ``${secret:NAME}`` password, then each page is
  asked of the browser's gate and of the guard under it (``cdp_guard.LoadGuard`` with the run's robots gate). Runs:
  a CLI run and an MCP call the user started with credentials, the same CLI run without credentials, a gate built
  with no initiation (a watch run or a schedule), and breadth exploration inside an MCP call.
* ``provider``: requests a site's page sends under ``ANYUSE_THIRD_PARTY=search`` after typing ``drill``
  (``LoadGuard.decide`` on a scope from the settings, a small pinned blocklist): the site's own search calls to an
  uncatalogued host (``want: allow``) and requests that must stay refused (``want: refuse``);
* ``any``: two of those shapes under ``ANYUSE_THIRD_PARTY=any``, where the third-party policy still decides.

Usage::

    python tests/bench/policy_bench.py run --old <tree> --new <tree> [--python <py>] [--out <dir>]

``--old`` is a checkout at the commit the branch forked from (read only: bytecode writes are off), ``--new`` the
branch. Each arm runs as a subprocess whose PYTHONPATH is a pkgroot directory (in ``--out``) holding an ``anyuse``
symlink to its tree; HOME, the data directory and the robots cache point into ``--out``. The arms write ``OLD.json``
and ``NEW.json``; ``compare`` prints the table.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from typing import Any, Dict, List

SITE = "www.shop-acct.test"
BASE = f"https://{SITE}"
ROBOTS = ("User-agent: *\nDisallow: /kundenkonto\nDisallow: /warenkorb\nDisallow: /kasse\nDisallow: /login\n"
          "Disallow: /sale\n")
PAGES = ["/kundenkonto/bestellungen", "/warenkorb", "/kasse", "/login", "/sale"]
GOV = ["www.bundestag.de", "api.data.europa.eu", "www.who.int", "www.army.mil", "data.agency.gov"]
SHOP = "www.toolnut.test"
API = "https://api.findit.test"
BLOCK = frozenset({"google-analytics.com", "doubleclick.net", "stats-y.test"})
JSON_CT = {"content-type": "application/json"}
#: (case, want, url, resource type, method, body, headers)
PROVIDER_CASES = [
    ("query param (XHR)", "allow", f"{API}/v2/query?q=drill&lang=de", "XHR", "GET", "", None),
    ("follow-up under the same path, no value", "allow", f"{API}/v2/facets?lang=de", "XHR", "GET", "", None),
    ("JSONP script", "allow", "https://suggest.findify.test/v3/autocomplete?q=drill&callback=cb", "Script", "GET", "",
     None),
    ("value as a path segment (fetch)", "allow", "https://search.shopx.test/api/suggest/drill", "Fetch", "GET", "",
     None),
    ("other parameter name (fetch)", "allow", "https://eu1.searchcloud.test/5/search?term=drill&hashid=x", "Fetch",
     "GET", "", None),
    ("search POST after typing", "allow", "https://api.findit.test/v1/search", "XHR", "POST",
     json.dumps({"query": "drill", "page": 1}), JSON_CT),
    ("multi-query POST body", "allow", "https://q1.searchcloud.test/1/indexes/*/queries", "XHR", "POST",
     json.dumps({"requests": [{"indexName": "p", "params": "query=drill&hitsPerPage=20"}]}), JSON_CT),
    ("ad/tracker host with the value", "refuse", "https://region1.stats-y.test/g/q?q=drill", "XHR", "GET", "", None),
    ("value inside a reported page address", "refuse",
     "https://px.stats-x.test/c?dl=https%3A%2F%2Fwww.toolnut.test%2Fsearch%3Fq%3Ddrill", "XHR", "GET", "", None),
    ("image named after the value", "refuse", "https://i.img.test/drill.jpg?q=drill", "Image", "GET", "", None),
    ("telemetry path", "refuse", f"{API}/collect?q=drill", "XHR", "GET", "", None),
    ("state-changing path", "refuse", f"{API}/wishlist/add?q=drill", "XHR", "GET", "", None),
    ("a page of another site", "refuse", f"{API}/v2/query?q=drill", "Document", "GET", "", None),
    ("DELETE", "refuse", f"{API}/v2/query?q=drill", "XHR", "DELETE", "", None),
    ("no value, fresh host", "refuse", "https://api.fresh.test/v1/list?page=2", "XHR", "GET", "", None),
    ("POST with a state-changing field", "refuse", "https://api.findit.test/v1/search", "XHR", "POST",
     json.dumps({"query": "drill", "addToCart": 1}), JSON_CT),
    ("preflight of a search POST after typing", "allow", "https://s.searchcloud2.test/v1/search", "Preflight",
     "OPTIONS", "", {"Access-Control-Request-Method": "POST"}),
    ("preflight to a telemetry path after typing", "refuse", "https://t.unlisted.test/collect?q=drill", "Preflight",
     "OPTIONS", "", {"Access-Control-Request-Method": "POST"}),
]
#: the same shape under ANYUSE_THIRD_PARTY=any: what the provider rule refuses is still the third-party policy's call
ANY_CASES = [
    ("content API whose path names events", "allow", "https://api.events-x.test/events/search?q=drill", "XHR", "GET",
     "", None),
    ("ad/tracker host with the value", "refuse", "https://region1.stats-y.test/g/q?q=drill", "XHR", "GET", "", None),
]


# ------------------------------------------------------------------------------------------------------------- arm
def _gov_rows() -> List[Dict[str, Any]]:
    from anyuse.execution import harness_product as HP
    from anyuse.execution import harness_tools as HT
    from anyuse.execution import site_modes
    from anyuse.execution import web_scope as W
    rows = []
    for setting in ("default", "on"):
        if setting == "on":
            os.environ["ANYUSE_REFUSE_GOV_HOSTS"] = "on"
        os.environ["ANYUSE_ROBOTS"] = "ignore"
        gate = HP.robots_gate()
        os.environ.pop("ANYUSE_ROBOTS", None)
        for h in GOV:
            refused = bool(W.contact_refusal(h)) or bool(HT.gov_refusal(f"https://{h}/")) or not gate(f"https://{h}/x")
            rows.append({"group": "gov", "case": f"{h} ({setting})", "want": "allow" if setting == "default" else
                         "refuse", "allowed": not refused})
        os.environ.pop("ANYUSE_REFUSE_GOV_HOSTS", None)
    real = site_modes._never_contact
    site_modes._never_contact = lambda: {"never.example.test"}
    try:
        rows.append({"group": "gov", "case": "never-contact host", "want": "refuse",
                     "allowed": not W.contact_refusal("never.example.test")})
    finally:
        site_modes._never_contact = real
    return rows


class _Shop:
    def __init__(self):
        self.signed_in = False

    def page(self, body: str) -> str:
        links = ['<a href="/login">Anmelden</a>', '<a href="/warenkorb">Warenkorb</a>', '<a href="/sale">Sale</a>']
        if self.signed_in:
            links += ['<a href="/kundenkonto/bestellungen">Bestellungen</a>', '<a href="/logout">Abmelden</a>']
        return (f"<html><head><title>Shop</title></head><body><header><nav>{' '.join(links)}</nav></header>"
                f"<main>{body}</main></body></html>")

    def fetch(self, method, url, pairs):
        path = urllib.parse.urlsplit(url).path
        if path == "/login" and method == "POST":
            self.signed_in = True
            return 200, BASE + "/kundenkonto/", self.page("<h1>Mein Konto</h1>")
        if path == "/login":
            return 200, url, self.page('<form method="post" action="/login"><label>E-Mail <input type="email" '
                                       'name="email"></label><label>Passwort <input type="password" name="pw">'
                                       '</label><button type="submit">Anmelden</button></form>')
        return 200, url, self.page("<h1>Shop</h1>")


def _signed_in_rows() -> List[Dict[str, Any]]:
    from anyuse.execution import api_permission as AP
    from anyuse.execution import cdp_guard as G
    from anyuse.execution import discover as D
    from anyuse.execution import harness_product as HP
    from anyuse.execution import harness_types as T
    from anyuse.execution import secret_refs as SR
    from anyuse.execution import web_flow as WF
    from anyuse.execution import web_flow_offline as WFO
    AP._ROBOTS_CACHE.clear()
    AP.robots_allows(BASE + "/", fetcher=lambda u: ROBOTS)
    creds = {"username": "u@x.test", "password": "${secret:shop}"}
    runs = [("CLI run with credentials", "cli", "", creds, "allow"),
            ("MCP call with credentials", "mcp", "", creds, "allow"),
            ("CLI run without credentials", "cli", "", {"q": "drill"}, "refuse"),
            ("no initiation (watch, schedule)", "", "", creds, "refuse"),
            ("breadth inside an MCP call", "mcp", "breadth", creds, "refuse")]
    rows = []
    for name, surface, auto, values, want in runs:
        if surface and auto:
            with AP.user_initiated(surface):
                with AP.unattended(auto):
                    gate = HP.robots_gate()
        elif surface:
            with AP.user_initiated(surface):
                gate = HP.robots_gate()
        else:
            gate = HP.robots_gate()
        store = SR.SecretStore(runner=lambda argv: (0, "hunter2-long-secret\n", ""), use_test_file=False)
        tools = WF.FlowBrowserTools(WFO.FlowDomPage(WFO.FlowSite(_Shop().fetch)), BASE + "/", SITE, allow_url=gate,
                                    values=values, secrets=store)
        obs = tools.observe()
        if SR.is_ref(values.get("password", "")):
            tools.dispatch(T.Login(), obs)
            obs = tools.observe()
        b = G.RequestBudget(D.Pacer(0.0, clock=lambda: 1000.0, sleeper=lambda s: None), per_load_cap=50,
                            per_host_cap=50, site_cap=50)
        b.write_grant = G.WriteGrant(BASE + "/")
        g = G.LoadGuard(b, allow_url=gate)
        g.start(BASE + "/")
        for p in PAGES:
            at_gate = tools.gate(T.Open(BASE + p), obs) == ("", None)
            at_guard = g.decide(BASE + p, "Document") == ""
            rows.append({"group": "signed_in", "case": f"{name}: {p}", "want": want, "allowed": at_gate and at_guard})
        rows.append({"group": "signed_in", "case": f"{name}: recorded", "want": "record" if want == "allow" else "-",
                     "allowed": bool(getattr(gate, "overrides", {}))})
    return rows


def _provider_rows(mode: str = "search", cases=PROVIDER_CASES, group: str = "provider") -> List[Dict[str, Any]]:
    from anyuse.execution import cdp_guard as G
    from anyuse.execution import discover as D
    from anyuse.execution import web_scope as W
    sc = W.WebScope.from_settings(mode=mode)
    sc._blocklist = BLOCK
    b = G.RequestBudget(D.Pacer(0.0, clock=lambda: 1000.0, sleeper=lambda s: None), web_scope=sc, search_post=True,
                        site_cap=200, per_load_cap=100)
    g = G.LoadGuard(b)
    g.start(f"https://{SHOP}/")
    g.typed.append("drill")
    sc.note_values(SHOP, ["drill"])
    g.acting = 1
    rows = []
    for case, want, url, rtype, method, body, headers in cases:
        why = g.decide(url, rtype, method, body, headers)
        rows.append({"group": group, "case": case, "want": want, "allowed": why == "", "why": why[:120]})
    return rows


def run_arm(label: str, out_file: str) -> None:
    rows = _gov_rows() + _signed_in_rows() + _provider_rows() + _provider_rows("any", ANY_CASES, "any")
    with open(out_file, "w", encoding="utf-8") as fh:
        json.dump({"arm": label, "rows": rows}, fh, indent=1)


# --------------------------------------------------------------------------------------------------------- compare
def compare(files: List[str]) -> str:
    arms = {}
    for f in files:
        with open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        arms[d["arm"]] = d["rows"]
    old, new = arms.get("OLD", []), arms.get("NEW", [])
    lines = [f"{'group':10} {'case':58} {'want':7} {'OLD':6} {'NEW':6}"]
    for o, n in zip(old, new):
        lines.append(f"{o['group']:10} {o['case'][:58]:58} {o['want']:7} {'pass' if o['allowed'] else 'stop':6} "
                     f"{'pass' if n['allowed'] else 'stop':6}")
    lines.append("")
    for group in ("gov", "signed_in", "provider", "any"):
        for arm, rows in (("OLD", old), ("NEW", new)):
            rs = [r for r in rows if r["group"] == group and r["want"] in ("allow", "refuse", "record")]
            right = sum(1 for r in rs if r["allowed"] == (r["want"] in ("allow", "record")))
            want_allow = [r for r in rs if r["want"] in ("allow", "record")]
            lines.append(f"{group:10} {arm}: {right}/{len(rs)} as wanted; "
                         f"{sum(1 for r in want_allow if r['allowed'])}/{len(want_allow)} wanted passes pass; "
                         f"{sum(1 for r in rs if r['want'] == 'refuse' and r['allowed'])} wanted refusals let through")
    return "\n".join(lines)


def run(args) -> int:
    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="anyuse-policy-bench-"))
    os.makedirs(out, exist_ok=True)
    trees = {"OLD": os.path.abspath(args.old), "NEW": os.path.abspath(args.new)}
    for t in trees.values():
        if out.startswith(t + os.sep):
            raise SystemExit("--out must be outside both trees")
    me = os.path.join(out, "policy_bench.py")
    shutil.copyfile(os.path.abspath(__file__), me)
    results = []
    for label, tree in trees.items():
        home = os.path.join(out, f"home_{label}")
        os.makedirs(os.path.join(home, ".anyuse"), exist_ok=True)
        pkgroot = os.path.join(out, f"pkgroot_{label}")
        os.makedirs(pkgroot, exist_ok=True)
        link = os.path.join(pkgroot, "anyuse")
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(tree, link)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANYUSE_")}
        env.update({"PYTHONPATH": pkgroot, "PYTHONDONTWRITEBYTECODE": "1", "HOME": home,
                    "ANYUSE_DATA_DIR": os.path.join(home, ".anyuse"),
                    "ANYUSE_ROBOTS_CACHE_FILE": os.path.join(home, "robots_cache.json"),
                    "XDG_CONFIG_HOME": os.path.join(home, ".config"),
                    "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
                    "XDG_CACHE_HOME": os.path.join(home, ".cache"),
                    "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"})
        path = os.path.join(out, f"{label}.json")
        proc = subprocess.run([args.python or sys.executable, "-B", me, "arm", "--label", label, "--out-file", path],
                              cwd=out, env=env)
        if proc.returncode:
            return proc.returncode
        results.append(path)
    text = compare(results)
    with open(os.path.join(out, "summary.txt"), "w") as fh:
        fh.write(text + "\n")
    print(text)
    print(f"\nrows: {', '.join(results)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--old", required=True)
    r.add_argument("--new", required=True)
    r.add_argument("--python", default="")
    r.add_argument("--out", default="")
    a = sub.add_parser("arm")
    a.add_argument("--label", required=True, choices=["OLD", "NEW"])
    a.add_argument("--out-file", required=True)
    c = sub.add_parser("compare")
    c.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return run(args)
    if args.cmd == "arm":
        run_arm(args.label, os.path.abspath(args.out_file))
        return 0
    print(compare(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
