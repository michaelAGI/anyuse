"""Cookie-consent policy: offline HTML fixtures and fake browser drivers only."""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from anyuse.cli import cli
from anyuse.execution.blocked import COOKIE_WALL, detect_blocked_ui
from anyuse.execution.cdp_replay import PageResult, ReplayExecutor, ReplayTemplate
from anyuse.execution.agent_browser import StepAgent
from anyuse.execution import human_inbox
from anyuse.execution.consent import (
    ACCEPT,
    ASK,
    NEVER,
    REJECT,
    automatic_action,
    choose_reject,
    choose_settings,
    controls_from_html,
    load_decisions,
    remember_decision,
)

FIXTURES = Path(__file__).resolve().parent.parent / "testdata" / "consent"
ROWS = ("<html><body><ul class='results'><li class='row'>Alpha Pkg</li>"
        "<li class='row'>Beta Pkg</li></ul></body></html>")


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def visible_text(markup: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup)).strip()


class FakePageDriver:
    """The replay driver seam: fixtures stand in for a live DOM."""

    def __init__(self, name: str, second_layer: str = ""):
        self.markup = fixture(name)
        self.second_layer = second_layer
        self.clicked = []

    def open(self, url, settle_s, timeout_s, readiness=None, url_policy=None,
             content_selector=""):
        dom = self.markup
        text = visible_text(dom)
        items = ["rendered"] if content_selector and "class=\"row\"" in dom else []
        state = detect_blocked_ui(text, "", url, [], items=items)
        steps = []
        consent = {}
        if state.blocked and state.reason == COOKIE_WALL:
            action = automatic_action(state.host)
            consent = {"policy": os.environ.get("ANYUSE_CONSENT_POLICY",
                                                "reject_nonessential"),
                       "status": action}
            if action == REJECT:
                controls = controls_from_html(dom)
                choice = choose_reject(controls)
                layer = 1
                if choice is None:
                    settings = choose_settings(controls)
                    if settings is not None and self.second_layer:
                        self.clicked.append(settings.label)
                        steps.append(settings.step(layer=1))
                        dom = fixture(self.second_layer)
                        choice = choose_reject(controls_from_html(dom))
                        layer = 2
                if choice is not None:
                    self.clicked.append(choice.label)
                    steps.append(choice.step(layer=layer))
                    dom, text = ROWS, visible_text(ROWS)
                    consent["status"] = "rejected"
                else:
                    consent["status"] = ASK
        return PageResult(url=url, dom=dom, text=text, status=200,
                          elapsed_ms=5.0, steps=steps, consent=consent)


class FakeAgentSession:
    fixture_name = "german.html"

    def __init__(self, endpoint, allow_url=None):
        self.url_now = ""
        self.markup = fixture(self.fixture_name)
        self.refused = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def goto(self, url):
        self.url_now = url

    async def dom(self):
        return self.markup

    async def text(self):
        return visible_text(self.markup)

    async def url(self):
        return self.url_now

    async def network(self):
        return []

    async def consent_controls(self):
        return controls_from_html(self.markup)

    async def click_consent(self, index):
        control = controls_from_html(self.markup)[index]
        if choose_reject([control]) is None:
            return False
        self.markup = ROWS
        return True

    async def observe(self):
        return {"url": self.url_now, "text": visible_text(self.markup), "elements": []}

    async def settle(self, seconds):
        return None


def template():
    return ReplayTemplate(url="https://example.test/search?q={q}", params=["q"],
                          extract={"items": "ul.results li.row"},
                          expect={"selector": "ul.results li.row", "min_items": 1})


class ConsentPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {name: os.environ.get(name) for name in (
            "ANYUSE_DATA_DIR", "ANYUSE_CONSENT_POLICY", "ANYUSE_API_HOSTS_FILE",
            "ANYUSE_BLOCKED_HOSTS_FILE", "ANYUSE_ROBOTS_CACHE_FILE")}
        os.environ["ANYUSE_DATA_DIR"] = self.tmp.name
        os.environ["ANYUSE_API_HOSTS_FILE"] = str(Path(self.tmp.name) / "hosts.json")
        os.environ["ANYUSE_BLOCKED_HOSTS_FILE"] = str(Path(self.tmp.name) / "blocked.json")
        os.environ["ANYUSE_ROBOTS_CACHE_FILE"] = str(Path(self.tmp.name) / "robots.json")
        os.environ.pop("ANYUSE_CONSENT_POLICY", None)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def replay(self, driver):
        with mock.patch("anyuse.execution.api_permission.replay_refusal", return_value=""):
            return ReplayExecutor(driver=driver).execute(template(), {"q": "alpha"})

    def test_default_rejects_generic_onetrust_cookiebot_and_german_controls(self):
        for name in ("generic.html", "onetrust.html", "cookiebot.html", "german.html"):
            with self.subTest(name=name):
                driver = FakePageDriver(name)
                out = self.replay(driver)
                self.assertTrue(out["success"], out.get("error"))
                self.assertEqual(len(out["steps"]), 1)
                self.assertEqual(out["steps"][0]["purpose"],
                                 "reject_nonessential_cookie_consent")
                clicked = " ".join(driver.clicked).lower()
                self.assertNotRegex(clicked, r"accept|agree|allow|akzept")

    def test_exact_reject_labels_cover_all_supported_languages(self):
        labels = ("Reject all", "Alle ablehnen", "Nur notwendige", "Tout refuser",
                  "Rechazar todo", "Rifiuta tutto", "Alles weigeren",
                  "Only necessary")
        for index, label in enumerate(labels):
            with self.subTest(label=label):
                choice = choose_reject([{"index": index, "text": label}])
                self.assertIsNotNone(choice)
                self.assertEqual(choice.action, REJECT)

    def test_accept_words_override_even_a_vendor_looking_reject_id(self):
        for label in ("Accept all", "Alle akzeptieren", "Tout accepter",
                      "Aceptar todo", "Accetta tutto", "Alles accepteren"):
            control = controls_from_html(
                f'<button id="onetrust-reject-all-handler">{label}</button>')[0]
            self.assertIsNone(choose_reject([control]), label)

    def test_one_settings_layer_may_reach_reject_all(self):
        driver = FakePageDriver("settings_layer.html", second_layer="generic.html")
        out = self.replay(driver)
        self.assertTrue(out["success"], out.get("error"))
        self.assertEqual([step["target"]["layer"] for step in out["steps"]], [1, 2])
        self.assertEqual(out["steps"][0]["purpose"], "open_cookie_settings")
        self.assertEqual(out["steps"][1]["purpose"],
                         "reject_nonessential_cookie_consent")

    def test_accept_only_falls_back_to_a_consent_card_without_clicking(self):
        driver = FakePageDriver("accept_only.html")
        out = self.replay(driver)
        self.assertFalse(out["success"])
        self.assertEqual(driver.clicked, [])
        self.assertEqual(out["blocked_state"]["reason"], COOKIE_WALL)
        self.assertEqual(out["needs_human"]["kind"], "consent")
        self.assertIn("Accept or reject cookies on example.test?",
                      out["blocked_state"]["message"])

    def test_consent_card_answer_is_remembered_for_the_host(self):
        out = self.replay(FakePageDriver("accept_only.html"))
        card_id = out["needs_human"]["card"]

        class Elicitor:
            def ask(self, message, schema):
                self.message, self.schema = message, schema
                return {"action": "accept", "content": {"choice": "reject"}}

        elicitor = Elicitor()
        answered = human_inbox.elicit_card(
            human_inbox.Inbox(), card_id,
            engine_for=lambda _run_id: (None, {"error": "not used"}),
            elicitor=elicitor)
        self.assertEqual(answered["human_result"], "reject")
        self.assertIn("Accept or reject cookies on example.test?", elicitor.message)
        self.assertEqual(load_decisions()["example.test"]["choice"], "reject")

    def test_a_banner_over_a_served_page_is_untouched(self):
        driver = FakePageDriver("full_page_banner.html")
        out = self.replay(driver)
        self.assertTrue(out["success"], out.get("error"))
        self.assertEqual(driver.clicked, [])
        self.assertEqual(out["steps"], [])

    def test_never_preserves_the_old_stop_without_clicking(self):
        os.environ["ANYUSE_CONSENT_POLICY"] = NEVER
        driver = FakePageDriver("generic.html")
        out = self.replay(driver)
        self.assertFalse(out["success"])
        self.assertEqual(driver.clicked, [])
        self.assertNotIn("needs_human", out)

    def test_ask_remembers_per_host_but_an_accept_is_never_automatic(self):
        os.environ["ANYUSE_CONSENT_POLICY"] = ASK
        remember_decision("example.test", REJECT)
        self.assertEqual(automatic_action("example.test"), REJECT)
        remember_decision("example.test", ACCEPT)
        self.assertEqual(automatic_action("example.test"), ASK)
        self.assertEqual(load_decisions()["example.test"]["choice"], ACCEPT)
        self.assertEqual((Path(self.tmp.name) / "consent_decisions.json").stat().st_mode
                         & 0o777, 0o600)

    def test_choices_are_settings_visible_and_resettable(self):
        remember_decision("example.test", REJECT)
        shown = CliRunner().invoke(cli, ["settings"])
        self.assertEqual(shown.exit_code, 0, shown.output)
        self.assertIn("REMEMBERED CONSENT CHOICES", shown.output)
        self.assertIn("example.test = reject", shown.output)
        reset = CliRunner().invoke(cli, ["settings", "reset-consent", "example.test"])
        self.assertEqual(reset.exit_code, 0, reset.output)
        self.assertEqual(load_decisions(), {})

    def test_setting_registry_default_and_values(self):
        from anyuse import settings
        self.assertEqual(settings.get("CONSENT_POLICY"), "reject_nonessential")
        settings.set_value("CONSENT_POLICY", "ask")
        self.assertEqual(settings.get("CONSENT_POLICY"), "ask")

    def test_agent_browser_reject_is_recorded_before_the_model_step(self):
        brain = mock.Mock(return_value=({"action": "done"}, {}, False))
        run = StepAgent(brain=brain, session_factory=FakeAgentSession,
                        endpoint="http://container.test:9222").run(
                            "read results", "https://example.test/", "alpha")
        self.assertEqual(run.stop, "done")
        self.assertEqual(run.steps[0]["purpose"],
                         "reject_nonessential_cookie_consent")
        self.assertEqual(run.steps[1]["action"], "done")
        brain.assert_called_once()

    def test_agent_browser_accept_only_stops_before_calling_the_model(self):
        class AcceptOnlySession(FakeAgentSession):
            fixture_name = "accept_only.html"

        brain = mock.Mock(return_value=({"action": "done"}, {}, False))
        run = StepAgent(brain=brain, session_factory=AcceptOnlySession,
                        endpoint="http://container.test:9222").run(
                            "read results", "https://example.test/", "alpha")
        self.assertEqual(run.stop, "blocked: cookie_wall")
        self.assertEqual(run.steps[-1]["action"], "consent_handoff")
        brain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
