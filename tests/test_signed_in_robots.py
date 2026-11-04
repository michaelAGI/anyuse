"""ANYUSE_ROBOTS=respect (the default): a signed-in task you started is not crawling. Pinned here:
* a task the user started (``user:*`` - a CLI agent run or replay, an MCP call) that signs in with the user's
  credentials (a ``${secret:NAME}`` value) is not held to robots.txt on its site's own origin (the start host and its
  www sibling): the open-task browser's gate, the guard under it and the run's start check let its account, cart and
  checkout pages through, and each request robots.txt would refuse is recorded on the run
  (``traffic.robots_override`` "a signed-in task you started");
* a compiled request replayed with the user's imported ``login=replay`` session is the same, in a call the user
  started;
* other hosts keep their own robots.txt; a task that does not sign in, background work (no initiation), breadth
  exploration and recompiles (``auto:*``) stay held.
No browser, network or model: robots files are injected, the site is an offline fake.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh tests/test_signed_in_robots.py
"""
import pathlib
import sys
import tempfile
import urllib.parse
from unittest import mock

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

import pytest  # noqa: E402

from anyuse.execution import api_permission as AP  # noqa: E402
from anyuse.execution import cdp_guard as G  # noqa: E402
from anyuse.execution import discover as D  # noqa: E402
from anyuse.execution import harness_product as HP  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import secret_refs as SR  # noqa: E402
from anyuse.execution import web_flow as WF  # noqa: E402
from anyuse.execution import web_flow_offline as WFO  # noqa: E402

SITE = "www.shop-acct.test"
BASE = f"https://{SITE}"
APEX = "https://shop-acct.test"
FILE = ("User-agent: *\nDisallow: /kundenkonto\nDisallow: /warenkorb\nDisallow: /kasse\nDisallow: /login\n"
        "Disallow: /sale\n")
CREDS = {"username": "u@x.test", "password": "${secret:shop}"}
ORDERS = BASE + "/kundenkonto/bestellungen"
REASON = AP.ROBOTS_SIGNED_IN


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots_cache.json"))
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path))
    for k in ("ANYUSE_ROBOTS", "ANYUSE_ROBOTS_POLICY", "ANYUSE_REFUSE_GOV_HOSTS"):
        monkeypatch.delenv(k, raising=False)
    AP._ROBOTS_CACHE.clear()
    AP._ROBOTS_NOTES.clear()
    assert AP.robots_allows(BASE + "/", fetcher=lambda u: FILE)[0] is True        # the site's file, read
    yield monkeypatch
    AP._ROBOTS_CACHE.clear()
    AP._ROBOTS_NOTES.clear()


class Shop:
    """An offline shop: a login link in the header, a password form, account pages once signed in."""

    def __init__(self):
        self.signed_in = False

    def page(self, body: str) -> str:
        links = ['<a href="/login?return_to=%2Fkundenkonto%2Fbestellungen">Anmelden</a>',
                 '<a href="/warenkorb">Warenkorb</a>', '<a href="/sale">Sale</a>']
        if self.signed_in:
            links += ['<a href="/kundenkonto/">Mein Konto</a>', '<a href="/kundenkonto/bestellungen">Bestellungen</a>',
                      '<a href="/logout">Abmelden</a>']
        return (f"<html><head><title>Shop</title></head><body><header><nav>{' '.join(links)}</nav></header>"
                f"<main>{body}</main></body></html>")

    def fetch(self, method, url, pairs):
        path = urllib.parse.urlsplit(url).path
        if path == "/login" and method == "POST":
            self.signed_in = dict(pairs or ()).get("pw") == "hunter2-long-secret"
            return 200, BASE + "/kundenkonto/", self.page("<h1>Mein Konto</h1>")
        if path == "/login" or (path.startswith("/kundenkonto") and not self.signed_in):
            return 200, BASE + "/login", self.page(
                '<h1>Anmelden</h1><form method="post" action="/login"><label>E-Mail <input type="email" name="email">'
                '</label><label>Passwort <input type="password" name="pw"></label>'
                '<button type="submit">Anmelden</button></form>')
        if path.startswith("/kundenkonto"):
            return 200, url, self.page("<h1>Bestellungen</h1><table><tr><td>Bestellung 1001</td><td>12,00 EUR</td>"
                                       "</tr></table>")
        return 200, url, self.page("<h1>Shop</h1><p>Willkommen</p>")


def browser(gate, values=CREDS):
    store = SR.SecretStore(runner=lambda argv: (0, "hunter2-long-secret\n", ""), use_test_file=False)
    page = WFO.FlowDomPage(WFO.FlowSite(Shop().fetch))
    return WF.FlowBrowserTools(page, BASE + "/", SITE, allow_url=gate, values=values, secrets=store,
                               related_origins=[APEX])


def guard(gate):
    b = G.RequestBudget(D.Pacer(0.0, clock=lambda: 1000.0, sleeper=lambda s: None), per_load_cap=30,
                        per_host_cap=30, site_cap=30)
    b.robots_overrides, b.robots_override = gate.overrides, gate.override
    b.write_grant = G.WriteGrant(BASE + "/")
    g = G.LoadGuard(b, allow_url=gate)
    g.start(BASE + "/")
    return b, g


def test_a_signed_in_task_you_started_opens_its_own_account_pages(env):
    with AP.user_initiated("cli"):
        gate = HP.robots_gate()
    assert gate(ORDERS) is False and gate.overrides == {}           # respect, until the task signs in
    tools = browser(gate)
    assert tools.signed_in == f"{REASON} (cli)"
    obs = tools.observe()
    out = tools.dispatch(T.Login(), obs)
    assert out.ok, out.why
    obs = tools.observe()
    for url in (ORDERS, BASE + "/sale", BASE + "/kasse"):
        assert tools.gate(T.Open(url), obs) == ("", None), url
    rec = gate.overrides[SITE]
    assert rec["overridden_by"] == f"{REASON} (cli)" and rec["robots_would"] == "disallow"
    assert rec["signed_in_by"] == WF.SIGNS_IN and rec["requests"] == 3
    # the www sibling is the same site; another host keeps its own robots.txt (never read here: fail closed)
    assert gate(APEX + "/kundenkonto/") is True
    assert gate("https://api.other-host.test/kundenkonto/") is False


def test_the_guard_lets_the_sites_own_requests_through_and_the_run_records_them(env):
    with AP.user_initiated("mcp"):
        gate = HP.robots_gate()
    assert WF.note_sign_in(gate, BASE + "/", [APEX], CREDS) == f"{REASON} (mcp)"
    b, g = guard(gate)
    assert g.decide(ORDERS, "Document") == ""
    assert g.decide(BASE + "/kundenkonto/api/orders.json", "XHR") == ""
    assert g.decide(BASE + "/login", "Document") == ""              # the own origin's login page (WriteGrant)
    tr = b.traffic(SITE)
    assert tr["robots_override"] == f"{REASON} (mcp)"
    assert tr["robots_overridden"][SITE]["requests"] == 3 and tr["robots_overridden"][SITE]["robots_would"] == "disallow"
    assert b.robots_refused == {}


@pytest.mark.parametrize("kind", ["breadth", "recompile"])
def test_background_breadth_and_recompiles_stay_held(env, kind):
    background = HP.robots_gate()                                    # no initiation: a watch run, a schedule
    with AP.user_initiated("mcp"):
        with AP.unattended(kind):
            auto = HP.robots_gate()
        explicit = HP.robots_gate(policy="respect")                  # an explicit policy decides alone
    for gate in (background, auto, explicit):
        assert WF.note_sign_in(gate, BASE + "/", [APEX], CREDS) == ""
        assert browser(gate).signed_in == ""
        assert gate(ORDERS) is False and gate.overrides == {}
        b, g = guard(gate)
        assert g.decide(ORDERS, "Document").startswith("robots_refused")
        assert "robots_override" not in b.traffic(SITE)


def test_a_task_that_does_not_sign_in_stays_held(env):
    with AP.user_initiated("cli"):
        gate = HP.robots_gate()
    tools = browser(gate, values={"q": "Akkuschrauber", "username": "u@x.test", "password": "plain-text"})
    assert tools.signed_in == "" and WF.signs_in({"note": "x ${secret:shop}"}) == ""
    obs = tools.observe()
    assert tools.gate(T.Open(ORDERS), obs) == ("blocked: robots.txt disallows this URL", None)
    assert gate.overrides == {}


def test_the_start_check_opens_a_signed_in_tasks_account_start_page(env):
    with AP.user_initiated("cli"):
        gate = HP.robots_gate()
    assert HP.robots_start_refusal(ORDERS, gate).startswith("robots.txt disallows the start URL")
    WF.note_sign_in(gate, BASE + "/", [APEX], CREDS)
    assert HP.robots_start_refusal(ORDERS, gate) == ""
    assert gate.overrides[SITE]["overridden_by"] == f"{REASON} (cli)"


def test_the_products_toolbox_tells_its_gate_before_the_start_check(env, tmp_path):
    from anyuse.execution import agent_browser as AB
    env.setenv("ANYUSE_LEARN_ENDPOINT", "http://192.168.64.9:9222")
    env.setattr(AB, "assert_container_endpoint", lambda ep, opener=None: {"Browser": "fake"})
    real, seen = HP.robots_start_refusal, []

    def start_check(start, gate, pacer=None, **kw):
        seen.append(real(start, gate, **kw))       # the run's real start check; the run stops before a page opens
        return "stopped here"
    env.setattr(HP, "robots_start_refusal", start_check)
    with AP.user_initiated("cli"):
        for values in (CREDS, {"q": "Akkuschrauber"}):
            made = HP.make_toolbox(site=ORDERS, run_id="r", values=values, sandbox=object(),
                                   workspace=str(tmp_path / "ws"))
            assert made["refusal"] == {"kind": "robots", "reason": "stopped here"}, made
    assert seen[0] == "" and seen[1].startswith("robots.txt disallows the start URL"), seen


# ------------------------------------------------------------------------------------------- the user's session
class Transport:
    def __init__(self):
        self.calls = []

    def send_with_policy(self, method, url, headers, body, timeout_s, *, allowed_origins, user_agent="",
                         user_agent_domain="", cookies=None):
        from anyuse.execution.api_step import ApiResponse
        self.calls.append(url)
        return ApiResponse(status=200, headers={"Content-Type": "application/json"},
                           text='{"orders":[{"id":"1001"}]}')


def template(**kw):
    from anyuse.execution.api_step import RequestTemplate
    base = dict(method="GET", url="https://api.example.com/konto/orders", query={"q": "{q}"},
                headers={"accept": "application/json"}, extract={"items": "orders[*].id"},
                expect={"status_in": [200], "content_type": "json", "path": "orders", "min_items": 1,
                        "required_keys": ["id"]}, params=["q"])
    base.update(kw)
    return RequestTemplate(**base)


def test_a_compiled_request_with_your_session_is_not_held_in_a_call_you_started(env, tmp_path):
    from anyuse.execution import site_modes
    from anyuse.execution.api_step import ApiStepExecutor
    path = str(tmp_path / "site_modes.toml")
    site_modes.set_modes("example.com", login="replay", route="replay",
                         session={"browser": "chrome", "profile": "P", "host": "example.com", "db_path": "/nonexistent",
                                  "cookie_names": ["sid"]}, path=path)
    assert AP.robots_allows("https://api.example.com/", fetcher=lambda u: "User-agent: *\nDisallow: /konto\n")[0]
    cookie = [{"name": "sid", "value": "v", "domain": "example.com", "host_only": False, "path": "/",
               "secure": False}]

    def run(tmpl):
        cap = Transport()
        ex = ApiStepExecutor(transport=cap, apply_site_modes=True, site_modes_path=path)
        with mock.patch.object(ex, "_import_cookies", return_value=cookie):
            return ex.execute(tmpl, {"q": "a"}), cap
    with AP.user_initiated("mcp"):
        r, cap = run(template(cookie_refs=["sid"]))
    assert r["success"], r
    assert f"overridden: {REASON} (mcp)" in r["robots_overridden"] and len(cap.calls) == 1
    # the same request outside a call the user started, and one without the session, stay held
    r, cap = run(template(cookie_refs=["sid"]))
    assert r.get("robots_refused") and cap.calls == []
    with AP.user_initiated("mcp"):
        r, cap = run(template())
        assert r.get("robots_refused") and cap.calls == []
        with AP.unattended("recompile"):
            r, cap = run(template(cookie_refs=["sid"]))
            assert r.get("robots_refused") and cap.calls == []



def test_the_override_names_the_rule_that_let_a_signed_in_request_through(env):
    with AP.user_initiated("mcp"):
        assert f"overridden: {REASON} (mcp)" in AP.replay_override(ORDERS, signed_in=True)
        env.setenv("ANYUSE_ROBOTS", "user_initiated")
        assert f"overridden: {REASON} (mcp)" in AP.replay_override(ORDERS, signed_in=True)
        assert "overridden: ANYUSE_ROBOTS=user_initiated (mcp)" in AP.replay_override(ORDERS)
        assert "overridden: ignore (explicit policy)" in AP.replay_override(
            ORDERS, policy="ignore", signed_in=True)
        env.setenv("ANYUSE_ROBOTS", "ignore")
        assert "overridden: ANYUSE_ROBOTS=ignore" in AP.replay_override(ORDERS, signed_in=True)

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
