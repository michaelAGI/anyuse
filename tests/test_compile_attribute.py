"""An agent run's compile, offline. Pinned here: ``agent run --compile`` builds a PRODUCT-ATTRIBUTE
tool (product_lookup) when the run answered one item's facts or the task asks for an attribute, and a site_search
tool for a list (harness_compile.workflow_of, used by harness_product._compile); a product run on the fake shop
compiles to a product_lookup tool that its proof replays on fresh product names at 0 model calls; the verifier's
second stage gets a NEW resolver - its own run budget - per answer, never one shared budget per process (an injected
resolver still wins). No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_compile_attribute.py
"""
import os as _os_arm
# the extractor's / replay's own answers as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import agent_harness as H  # noqa: E402
from anyuse.execution import attribute_verify as AV  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402

import test_harness_compile as THC  # noqa: E402
import test_repair_ladder as SHOP  # noqa: E402

BASE, SITE = SHOP.BASE, SHOP.SITE


def product_run(task="find the price and availability of {q}", done=None):
    site = SHOP.Shop()
    tools = HT.BrowserTools(D.DomPage(site), BASE + "/", SITE)

    def click(t, o, h):
        return T.Click(next(e.index for e in o.elements if "/p/bosch-gsr" in e.href))
    pol = P.ScriptedPolicy([T.Search(0, "Bosch GSR 12V Akkuschrauber"), click, done or T.Done(
        {"name": "Bosch GSR 12V Akkuschrauber", "price": "89,90 €", "availability": "Auf Lager"})])
    tr = H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="attr").run(
        task, {"q": "Bosch GSR 12V Akkuschrauber"})
    return tr, site


def test_the_workflow_follows_what_the_run_answered_and_the_task_asked():
    tr, _ = product_run()
    assert tr.ok and HC.workflow_of(tr) == "product_lookup"
    lst = T.Trace(kind="web", task="search for x", stop="done", result={"results": [{"title": "a", "url": "u"}]})
    assert HC.workflow_of(lst) == "site_search"
    assert HC.workflow_of(T.Trace(kind="web", task="search for x", stop="done", result={"no_results": True})) == \
        "site_search"
    words = T.Trace(kind="web", task="Wie viel kostet {q}? Preis bitte", stop="done", result={"name": "X"})
    assert HC.workflow_of(words) == "product_lookup"
    assert HC.workflow_of(T.Trace(kind="web", task="search for x", stop="done", result={"summary": "ok"})) == \
        "site_search"


def test_a_product_run_compiles_to_a_product_lookup_tool_that_its_proof_replays():
    tr, site = product_run()
    c = HC.compile_web(tr, HC.workflow_of(tr))
    assert c["ok"], c["why"]
    assert c["tool"]["workflow"] == "product_lookup"
    with THC.Tripwire():
        rep = HC.compile_and_save_web(tr, THC.storage(), site, ["Makita DF333 Akkuschrauber",
                                                               "Metabo PowerMaxx Akkuschrauber"],
                                      workflow=HC.workflow_of(tr))
    assert rep["status"] == "ready", rep["reasons"]
    assert all(c["status"] == "ok" for c in rep["proof"]["cases"]) and rep["proof"]["model_calls"] == 0


def test_the_product_cli_path_passes_the_workflow():
    src = (_HERE / "execution" / "harness_product.py").read_text(encoding="utf-8")
    assert "wf = HC.workflow_of(trace)" in src and "workflow=wf" in src


def test_the_verifier_gets_its_own_budget_per_answer():
    AV._DEFAULT.clear()
    a, b = AV.default_resolver(), AV.default_resolver()
    assert a is not b                                         # never one resolver (one budget) per process
    inj = AV.Resolver(None)
    AV._DEFAULT["r"] = inj
    try:
        assert AV.default_resolver() is inj
    finally:
        AV._DEFAULT.clear()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
