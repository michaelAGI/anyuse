"""Matching a query against result text in every script (execution/unicode_text.py,
execution/query_match.py).

* digits of every script compare by value; the accents of letters with case are
  dropped on both sides (ΑΘΗΝΑ = Αθήνα, Wrocław = Wroclaw), and so are the
  optional vowel marks of Arabic and Hebrew; a letter of another cased script
  without a decomposition stays a letter of its own (Kazakh қ is not к);
* in a script written without spaces each occurrence of the query has a
  position: on its own, at the start, at the end or inside a longer run;
* a new contract accepts a one- or two-result answer whose key holds the query
  on its own or at the start of a run, plus the positions the site's own result
  lists showed on the FITTING inputs (never the validation inputs); on a product
  call the configured check model may read the rest; a contract made before
  positions existed matches as it did.

Offline: fake transports, a fake driver, a fake model (``model_check.using``).
"""
import json
import random
import sys
import unicodedata
import urllib.parse

import pytest

from anyuse.execution import empty_page as EP
from anyuse.execution import model_check as MC
from anyuse.execution import query_match as QM
from anyuse.execution import result_contract as RC
from anyuse.execution import unicode_text as U
from anyuse.execution.api_compile import compiled_record, normalise, persist_compiled, \
    with_runtime_rungs
from anyuse.execution.api_permission import acknowledge_host
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, load_command
from anyuse.execution.api_verify import compile_and_verify
from anyuse.execution.cdp_replay import PageResult, ReplayExecutor, ReplayTemplate
from anyuse.execution.html_extract import parse
from anyuse.execution.workflow_schema import template_digest
from anyuse.mcpserver.compiled_tools import run_compiled

HOST = "fix.test"


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    for name in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
                 "ANYUSE_AGENT_MODEL_PROVIDER", "ANYUSE_EMPTY_PROBE", "ANYUSE_AUTO_HEAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANYUSE_MODEL_CHECKS", "none")
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))
    monkeypatch.setenv("ANYUSE_API_HOSTS_FILE", str(tmp_path / "hosts.json"))
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots.json"))


def nfd(s):
    return unicodedata.normalize("NFD", s)


# ---------------------------------------------------------------------------
# folding
# ---------------------------------------------------------------------------

def test_digits_of_every_script_read_as_their_value():
    assert normalise("٢٠٢٤") == "2024"
    assert normalise("๒๕๖๗") == "2567"
    assert normalise("१२३") == "123"
    assert normalise("Model ٣") == "model 3"
    assert normalise("مكتبة ١") == normalise("مكتبة 1") == "مكتبة 1"
    assert normalise("ＡＢＣ－１２３") == "abc 123"


def test_accents_of_cased_letters_are_ignored_on_both_sides():
    assert normalise("ΑΘΗΝΑ") == normalise("Αθήνα") == normalise(nfd("Αθήνα")) == "αθηνα"
    assert normalise("MÜNCHEN") == normalise("Munchen") == "munchen"
    assert normalise("Wrocław") == "wroclaw"
    assert normalise("Øresund") == "oresund"
    assert normalise("Đà Nẵng") == normalise("DA NANG") == "da nang"
    assert normalise("İstanbul") == "istanbul"
    assert normalise("Hội An") == "hoi an"


def test_letters_without_a_decomposition_stay_letters_outside_latin():
    # the "X WITH ..." reading is for Latin letters; Kazakh қ, ғ, ң are letters of their own
    assert normalise("Қазақстан") != normalise("Казакстан")
    assert normalise("Ғалым") != normalise("Галым")
    assert U.base_letter("қ") == "қ" and U.base_letter("ł") == "l"


def test_optional_marks_of_arabic_and_hebrew_are_ignored():
    assert normalise("مَكْتَبَة") == normalise("مكتبة") == "مكتبة"
    assert normalise("مكـــتبة") == "مكتبة"
    # hamza forms that decompose fold with their letter
    assert normalise("أحمد") == normalise("احمد")
    assert normalise("إسلام") == normalise("اسلام")
    assert normalise("שָׁלוֹם") == normalise("שלום") == "שלום"


def test_scripts_without_case_keep_their_marks():
    for s in ("ภาษาไทย", "हिन्दी भाषा", "ガイド", "서울", "บ้าน"):
        assert normalise(s) == unicodedata.normalize("NFC", s), s
    assert normalise(nfd("ガイド")) == "ガイド" and normalise("ｶﾞｲﾄﾞ") == "ガイド"
    assert normalise("ガイド") != normalise("カイト")


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------

def test_occurrence_positions():
    table = [("東京タワー", "東京", "word"), ("東京駅", "東京", "start"), ("東京都", "京都", "end"),
             ("東京都庁", "京都", "inside"), ("iphoneケース", "iphone", "word"),
             ("ホテル京都", "京都", "word"), (normalise("「京都」の検索結果"), "京都", "word"),
             ("กรุงเทพตลาด", "กรุงเทพ", "start"), ("서울역", "서울", "start"),
             ("부산서울", "서울", "end"), ("москва сити", "москва", "word"),
             ("東京2号", "東京", "word"), ("ラーメン", "ラー", "start")]
    for text, word, want in table:
        got = U.best_occurrence(text, word)
        assert got is not None and got[2] == want, (text, word, got)
    # a Thai query that ends in a tone mark is placed by its last letter
    assert U.best_occurrence("บ้านสวน", "บ้าน")[2] == "start"
    assert U.best_occurrence("москвариум", "москва") is None
    assert U.occurrences("東京都庁", "京都", U.STRICT_POSITIONS) == []


def test_a_modifier_letter_or_a_mark_at_the_edge_of_the_input():
    # ー and 々 continue the run the input ends in: the script changes after them
    assert U.best_occurrence("コーヒーと紅茶", "コーヒー")[2] == "word"
    assert U.best_occurrence("アイスコーヒーと", "コーヒー")[2] == "end"
    assert U.best_occurrence("人々の", "人々")[2] == "word"
    # the text goes on with a tone mark the input lacks: the input ends inside a letter
    for text, word in (("ไข่ไก่", "ไข"), ("ใหม่", "ใหม")):
        assert U.best_occurrence(text, word)[2] == "inside", (text, word)
        assert U.occurrences(text, word, U.STRICT_POSITIONS) == [], (text, word)
        assert U.occurrences(text, word) == [(0, len(word), "inside")], (text, word)
    c = RC.replay_contract("q")
    assert not sparse(c, "ไข", ["ไข่ไก่"]).ok
    assert sparse(c, "ไข่", ["ไข่ไก่"]).ok


def _old_word_spans(text, words):
    """The whole-word rule before positions, kept here to pin that every position
    still finds exactly its spans."""
    out = []
    if not words or not text:
        return out
    open_start, open_end = U.unspaced(words[0]), U.unspaced(words[-1])
    start = text.find(words)
    while start >= 0:
        end = start + len(words)
        if (start == 0 or text[start - 1] == " " or open_start or U.unspaced(text[start - 1])) \
                and (end == len(text) or text[end] == " " or open_end or U.unspaced(text[end])):
            out.append((start, end))
            start = text.find(words, end)
        else:
            start = text.find(words, start + 1)
    return out


def test_every_position_finds_the_spans_of_the_old_rule():
    from anyuse.execution.api_compile import word_spans
    rng = random.Random(5)
    alphabet = "ab 東京都タワーカ서울กร์"
    for _ in range(5000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 14)))
        word = "".join(rng.choice(alphabet.replace(" ", "")) for _ in range(rng.randint(1, 3)))
        assert word_spans(text, word) == _old_word_spans(text, word), (text, word)


# ---------------------------------------------------------------------------
# the runtime contract: strict, the model, an old contract
# ---------------------------------------------------------------------------

def sparse(contract, value, names, ctx=None):
    items = [{"name": n} for n in names]
    return RC.check_json(contract, {"results": items}, "results", {"q": value}, items, empty=ctx)


def reads(word):
    """A fake check model that reads ``word`` as the word at the marked place."""
    asked = []

    def fake(instructions, state, obs):
        asked.append(state)
        return 0.95 if state.splitlines()[-1] == ">> " + word else 0.05
    fake.asked = asked
    return fake


def boom(*_a):
    raise AssertionError("the check model was asked")


def product():
    return EP.Context(probe=False, learn=False)


def test_strict_rule_on_a_new_contract():
    c = RC.replay_contract("q")
    assert c["query_positions"] == ["word", "start"]
    v = sparse(c, "京都", ["東京都庁"])
    assert not v.ok and "inside a longer word" in v.reason, v.reason
    assert sparse(c, "京都", ["京都駅"]).ok
    assert sparse(c, "京都", ["京都タワー", "ホテル京都"]).ok
    assert not sparse(c, "大学", ["東京大学"]).ok
    # a contract made before positions accepts every one, as it did
    legacy = dict(c)
    legacy.pop("query_positions")
    assert sparse(legacy, "京都", ["東京都庁"]).ok
    # Latin keys are whole words, as before
    assert sparse(c, "boost", ["mingw-w64-boost"]).ok and not sparse(c, "boost", ["boostnote"]).ok


def test_the_model_reads_an_ambiguous_key():
    c = RC.replay_contract("q")
    yes, ctx = reads("大学"), product()
    with MC.scope("product"), MC.using(yes):
        v = sparse(c, "大学", ["東京大学"], ctx)
    assert v.ok and v.kind == "sparse" and v.model_calls >= 1, v.reason
    assert ctx.model_calls == v.model_calls and "check model" in v.reason
    # the query is the first reading; the others are longer words that stand there
    assert yes.asked[0].splitlines()[-1] == ">> 大学"
    for state in yes.asked:
        assert "東京⟦大学⟧" in state and state.splitlines()[-1][3:] in "東京大学"
    # the model reads a longer word: refused
    with MC.scope("product"), MC.using(reads("東京大学")):
        v = sparse(c, "大学", ["東京大学"], product())
    assert not v.ok and "inside a longer word" in v.reason
    # no answer, an exception or an unclear answer: no opinion, the strict rule refuses
    for fake in (lambda i, s, o: None, boom, lambda i, s, o: 0.5, lambda i, s, o: 0.05):
        with MC.scope("product"), MC.using(fake):
            assert not sparse(c, "大学", ["東京大学"], product()).ok


def test_the_model_is_asked_only_where_the_position_is_open_on_a_product_call():
    c = RC.replay_contract("q")
    legacy = dict(c)
    legacy.pop("query_positions")
    with MC.scope("product"), MC.using(boom):
        assert sparse(c, "東京", ["東京駅"], product()).ok
        assert sparse(c, "東京", ["東京タワー"], product()).ok
        assert sparse(c, "boost", ["mingw-w64-boost"], product()).ok
        assert not sparse(c, "boost", ["boostnote"], product()).ok
        assert sparse(legacy, "大学", ["東京大学"], product()).ok
        # a list is judged by the list contract, never by its keys
        assert sparse(c, "大学", ["東京大学", "大阪大学", "京都大学"], product()).ok
        # without the call's context (a proof, a ready read) nothing is asked
        assert not sparse(c, "大学", ["東京大学"]).ok
    # outside a product call's scope (a watch run, a heal) nothing is asked
    with MC.using(boom):
        assert not sparse(c, "大学", ["東京大学"], product()).ok
    with MC.scope("heal"), MC.using(boom):
        assert not sparse(c, "大学", ["東京大学"], product()).ok


def test_empty_statement_binds_the_query_by_position():
    strict = QM.Reading(U.STRICT_POSITIONS)
    phrase, why = RC.empty_evidence_text("没有找到与东京都相关的结果", "京都", reading=strict)
    assert not phrase, why
    assert RC.empty_evidence_text("没有找到与东京都相关的结果", "京都")[0]
    assert RC.empty_evidence_text("没有找到与“京都”相关的结果", "京都", reading=strict)[0]
    assert not RC.empty_evidence_text("没有找到与东京相关的结果", "东京", reading=strict)[0]
    with MC.scope("product"), MC.using(reads("东京")):
        named = QM.Reading(U.STRICT_POSITIONS, model=True)
        assert RC.empty_evidence_text("没有找到与东京相关的结果", "东京", reading=named)[0]
    assert named.model_calls >= 1


def test_an_empty_page_on_a_new_contract_binds_the_query_strictly():
    page = "<html><body><main><p>没有找到与东京都相关的结果</p></main></body></html>"

    def verdict(contract, ctx=None):
        return RC.check_html(contract, page, "ul.r li", {"q": "京都"}, [], empty=ctx)
    assert not verdict(RC.replay_contract("q")).ok
    assert verdict(RC.replay_contract("q", positions=None)).ok
    # on a product call the check model reads the query where it stands; its reading of
    # the line itself (the empty-page step before the statement rule) is left unclear here
    word = reads("京都")

    def fake(instructions, state, obs):
        return word(instructions, state, obs) if "⟦" in state else 0.5
    ctx = product()
    with MC.scope("product"), MC.using(fake):
        v = verdict(RC.replay_contract("q"), ctx)
    assert v.ok and v.empty_by == "page_statement" and word.asked, v.reason
    assert ctx.model_calls >= 1 + len(word.asked)


# ---------------------------------------------------------------------------
# the proof calibrates the positions on the fitting inputs
# ---------------------------------------------------------------------------

SUBSTRING = {"京都": ["京都駅", "京都大学", "東京都庁", "京都タワー"],
             "大阪": ["大阪城", "大阪駅", "東大阪市役所", "大阪ドーム"],
             "札幌": ["札幌駅", "札幌ドーム", "新札幌駅", "札幌大学"],
             "神戸": ["東神戸駅"]}
PREFIX = {"京都": ["京都駅", "京都大学", "京都御所", "京都タワー"],
          "大阪": ["大阪城", "大阪駅", "大阪大学", "大阪ドーム"],
          "札幌": ["札幌駅", "札幌ドーム", "札幌時計台", "札幌大学"],
          "神戸": ["東神戸駅"]}


def _body(names):
    return json.dumps({"results": [{"name": n, "id": i} for i, n in enumerate(names)]},
                      ensure_ascii=False)


def _recording(value, names):
    q = urllib.parse.quote(value)
    return {"param": {"name": "q", "value": value}, "site": HOST,
            "final_url": f"https://{HOST}/search?q={q}",
            "ui": {"items": list(names), "text": "\n".join(names)},
            "network": [{"url": f"https://{HOST}/search?q={q}", "method": "GET", "status": 200,
                         "mime": "text/html", "body": "<html><div id=app></div></html>"},
                        {"url": f"https://{HOST}/api/search?q={q}", "method": "GET",
                         "status": 200, "mime": "application/json", "body": _body(names)}]}


class Site:
    def __init__(self, lists):
        self.lists, self.log = lists, []

    def send(self, method, url, headers, body, timeout_s):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0]
        self.log.append(q)
        return ApiResponse(200, {"Content-Type": "application/json"},
                           _body(self.lists.get(q, [])))


def prove(fit, validation, site_lists=None):
    """Compile on the fitting lists ``fit`` (value -> labels, the first is the
    recording) and prove on ``validation``; the site serves ``site_lists``."""
    values = list(fit)
    rec = _recording(values[0], fit[values[0]])
    fitting = [_recording(v, fit[v]) for v in values[1:]]
    fresh = [_recording(v, names) for v, names in validation.items()]
    site = Site(site_lists or {**fit, **validation})
    return compile_and_verify(rec, fresh, executor=ApiStepExecutor(transport=site),
                              preflight=lambda _c: "", fitting_recordings=fitting or None)


def test_the_proof_calibrates_positions_from_the_fitting_inputs():
    compiled, proof = prove({"京都": SUBSTRING["京都"]},
                            {v: SUBSTRING[v] for v in ("大阪", "札幌")}, SUBSTRING)
    assert proof.passed, proof.reason
    c = RC.contract_of(compiled.template)
    assert "inside" in c["query_positions"] and "list page" in c["query_calibration"], c
    out = ApiStepExecutor(transport=Site(SUBSTRING)).execute(compiled.template, {"q": "神戸"})
    assert out["success"] and out["result_contract"]["kind"] == "sparse", out.get("error")
    # a site whose own lists only ever start a word with the query
    compiled, proof = prove({"京都": PREFIX["京都"]},
                            {v: PREFIX[v] for v in ("大阪", "札幌")}, PREFIX)
    assert proof.passed, proof.reason
    c = RC.contract_of(compiled.template)
    assert c["query_positions"] == ["word", "start"], c
    out = ApiStepExecutor(transport=Site(PREFIX)).execute(compiled.template, {"q": "神戸"})
    assert not out["success"] and "inside a longer word" in out["error"], out


def test_validation_inputs_never_calibrate_the_positions():
    # the fitting list starts every word with its query; only the validation lists do not
    compiled, proof = prove({"京都": PREFIX["京都"]},
                            {v: SUBSTRING[v] for v in ("大阪", "札幌")})
    assert proof.passed, proof.reason
    c = RC.contract_of(compiled.template)
    assert c["query_positions"] == ["word", "start"], c
    assert "none of 1 fitting list page(s)" in c["query_calibration"]


def test_a_sparse_proof_case_uses_the_fitting_lists_positions():
    end_list = ["京都駅", "京都大学", "東京都", "京都タワー"]
    validation = {"大阪": PREFIX["大阪"], "神戸": ["東神戸"]}
    compiled, proof = prove({"京都": end_list}, validation)
    assert proof.passed, proof.reason
    assert "end" in RC.contract_of(compiled.template)["query_positions"]
    # the same sparse validation page, with a fitting list that never did that
    _compiled, proof = prove({"京都": PREFIX["京都"]}, validation)
    assert not proof.passed and "only inside a longer word" in proof.reason, proof.reason
    # a sparse page is never its own evidence: a short fitting list counts for nothing
    _compiled, proof = prove({"京都": PREFIX["京都"], "東京": ["東京都", "北東京"]}, validation)
    assert not proof.passed, proof.reason


def test_old_records_keep_their_matching():
    compiled, proof = prove({"京都": PREFIX["京都"]}, {v: PREFIX[v] for v in ("大阪", "札幌")},
                            PREFIX)
    assert proof.passed, proof.reason
    site = Site({**PREFIX, "京都": ["東京都庁"]})
    d = compiled.template.to_dict()
    out = ApiStepExecutor(transport=site).execute(load_command(d), {"q": "京都"})
    assert not out["success"], out
    for key in ("query_positions", "query_calibration"):
        d["expect"]["result_contract"].pop(key)
    old = load_command(d)
    assert template_digest(old.to_dict()) == template_digest(d)
    out = ApiStepExecutor(transport=site).execute(old, {"q": "京都"})
    assert out["success"] and out["result_contract"]["kind"] == "sparse", out.get("error")


def test_a_heal_keeps_the_positions_of_the_tool_it_repairs():
    prev = ReplayTemplate(url="https://h.test/s?q={q}", params=["q"], extract={"items": "li"},
                          expect={RC.CONTRACT_KEY: RC.replay_contract("q", positions=None)})
    new = ReplayTemplate(url="https://h.test/s?q={q}", params=["q"], extract={"items": "li"},
                         expect={RC.CONTRACT_KEY: RC.replay_contract("q")})
    assert RC.carry_positions(prev, new)
    assert "query_positions" not in RC.contract_of(new)
    prev.expect[RC.CONTRACT_KEY]["query_positions"] = ["word", "start", "end"]
    prev.expect[RC.CONTRACT_KEY]["query_calibration"] = "seen"
    assert RC.carry_positions(prev, new)
    assert RC.contract_of(new)["query_positions"] == ["word", "start", "end"]


class HtmlSite:
    """``https://ex.test/search?q=``: six result rows named after the input."""

    def send(self, method, url, headers, body, timeout_s):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0]
        rows = "".join(f"<tr class='row'><td><a class='name'>{q}-lib-{i}</a></td></tr>"
                       for i in range(6))
        return ApiResponse(200, {"content-type": "text/html; charset=utf-8"},
                           f"<html><body><main><h1>Results for {q}</h1><table class='results'>"
                           f"<tbody>{rows}</tbody></table></main></body></html>")


def test_a_reproof_keeps_the_positions_of_the_tool_it_repairs():
    from anyuse.execution import step_repair as SR
    from anyuse.execution.api_step import RequestTemplate
    sel = "table.results tbody tr.row a.name"

    def tool(positions):
        t = RequestTemplate(method="GET", url="https://ex.test/search", query={"q": "{q}"},
                            extract={"items": sel}, extract_kind="html", shape="list",
                            params=["q"], expect={"status_in": [200], "content_type": "html",
                                                  "selector": sel, "min_items": 6})
        RC.attach(t)
        c = RC.contract_of(t)
        c.pop("query_positions")
        if positions is not None:
            c.update(query_positions=positions, query_calibration="seen on the fitting lists")
        return t
    vals = ["alpha", "beta", "delta", "epsilon"]
    for positions in (None, ["word", "start", "end"]):
        old = tool(positions)
        new = SR.with_extraction(old, f"main {sel}", "test: the extraction relearned")
        chosen, proof = SR.reprove(new, vals, ApiStepExecutor(transport=HtmlSite()),
                                   previous=old)
        assert proof.passed, proof.reason
        c = RC.contract_of(chosen)
        if positions is None:
            # a tool proved before positions stays so after a heal
            assert "query_positions" not in c, c
        else:
            assert c["query_positions"] == positions and c["query_calibration"], c


def test_a_heading_or_a_summary_names_the_input_by_position():
    bare = "検索結果はありません。"
    tops = (lambda q: f"<h2>「{q}」の検索結果</h2><p class='empty'>{bare}</p>",
            lambda q: f"<p class='summary'>「{q}」の検索結果</p><ul class='r'></ul>"
                      f"<p class='empty'>{bare}</p>")

    def run(body, contract):
        expect = {"selector": "ul.r li", "min_items": 3, "identity": {"final_url": "rendered_url"}}
        if contract is not None:
            expect[RC.CONTRACT_KEY] = contract
        t = ReplayTemplate(url="https://h.test/s?q={q}", params=["q"],
                           extract={"items": "ul.r li"}, expect=expect)
        # a line above names the input on its own, so the page's text near the statement
        # does; the heading or the summary right above the statement decides
        dom = f"<html><body><main><p class='crumb'>京都</p>{body}</main></body></html>"
        return ReplayExecutor(driver=Driver(dom)).execute(t, {"q": "京都"})
    for top in tops:
        assert run(top("京都"), RC.replay_contract("q"))["success"]
        # the heading or the summary holds 京都 only at the end of 東京都
        out = run(top("東京都"), RC.replay_contract("q"))
        assert not out["success"], out
        # a template without a contract, or proved before positions, reads it anywhere
        assert run(top("東京都"), None)["success"]
        assert run(top("東京都"), RC.replay_contract("q", positions=None))["success"]


def test_the_browser_rung_reads_the_calibrated_positions():
    q = urllib.parse.quote("京都")
    recording = {"param": {"name": "q", "value": "京都"}, "site": HOST,
                 "final_url": f"https://{HOST}/s?q={q}", "ground_truth_selector": "ul li",
                 "ui": {"items": SUBSTRING["京都"]}}
    contract = RC.replay_contract("q")
    contract["query_positions"] = ["word", "start", "inside"]
    contract["query_calibration"] = "seen on the fitting lists"
    record = {"template": {"expect": {RC.CONTRACT_KEY: contract}}}
    with_runtime_rungs(record, recording)
    browser = record["browser"]["template"]["expect"][RC.CONTRACT_KEY]
    assert browser["query_positions"] == ["word", "start", "inside"]
    assert browser["query_calibration"] == "seen on the fitting lists"


def test_a_learned_browser_replay_calibrates_on_its_fitting_pages():
    from anyuse.execution import learn_loop as LL

    class Replay:
        def __init__(self, lists):
            self.lists = lists

        def execute(self, template, params):
            return {"success": True, "extracted": {"items": list(self.lists[params["q"]])},
                    "elapsed_ms": 5.0}

    def rec(value, names):
        q = urllib.parse.quote(value)
        return {"param": {"name": "q", "value": value}, "site": HOST,
                "final_url": f"https://{HOST}/s?q={q}", "ground_truth_selector": "ul li",
                "ui": {"items": list(names)}}

    def learned(fit, validation):
        values = list(fit)
        out = LL._try_tier2(rec(values[0], fit[values[0]]),
                            [rec(v, names) for v, names in validation.items()], "q",
                            Replay({**fit, **validation}), set(),
                            fitting=[rec(v, fit[v]) for v in values[1:]])
        assert out["ok"], out
        return RC.contract_of(ReplayTemplate.from_dict(out["template"]))
    c = learned({"京都": SUBSTRING["京都"]}, {v: SUBSTRING[v] for v in ("大阪", "札幌")})
    assert "inside" in c["query_positions"] and "list page" in c["query_calibration"], c
    # only the validation pages hold the input inside a longer word: they set nothing
    c = learned({"京都": PREFIX["京都"]}, {v: SUBSTRING[v] for v in ("大阪", "札幌")})
    assert c["query_positions"] == ["word", "start"], c


# ---------------------------------------------------------------------------
# the runtime hands the call's context to the check
# ---------------------------------------------------------------------------

class Storage:
    def __init__(self, base):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id):
        return f"{self.base_dir}/workflow_{workflow_id}"

    def get(self, name):
        return None


def test_the_runtime_passes_the_configured_model(tmp_path):
    lists = {**PREFIX, "大学": ["東京大学"]}
    compiled, proof = prove({"京都": PREFIX["京都"]}, {v: PREFIX[v] for v in ("大阪", "札幌")},
                            lists)
    assert proof.passed, proof.reason
    record = compiled_record(compiled.template, proof.to_dict(), workflow="fix_search",
                             source={"site": HOST, "origin": "test"})
    storage = Storage(str(tmp_path))
    persist_compiled(storage, "fix_search", record)
    hosts = str(tmp_path / "hosts.json")
    acknowledge_host(HOST, path=hosts)

    def call(**kw):
        return run_compiled("fix_search", {"q": "大学"}, storage=storage, hosts_path=hosts,
                            executor=ApiStepExecutor(transport=Site(lists)), **kw)
    with MC.using(reads("大学")):
        out = call()
    assert out["ok"] is True and out["model_calls"] >= 1, out
    # a watch run asks no model and refuses the answer
    with MC.using(boom):
        out = call(cascade=False, record_outcome=False, post_card=False)
    assert out["ok"] is False and out["model_calls"] == 0, out
    # no model configured: the strict rule
    assert call()["ok"] is False


class Driver:
    def __init__(self, dom):
        self.dom = dom
        self.text = parse(dom, visible_only=True).text()

    def open(self, url, settle_s, timeout_s):
        return PageResult(url=url, dom=self.dom, text=self.text, status=200, elapsed_ms=10.0)


def test_the_browser_replay_passes_the_configured_model():
    dom = "<html><body><ul><li class='r'><h3><a>東京大学</a></h3><p>★ 4.5</p></li></ul></body></html>"
    t = ReplayTemplate(url="https://h.test/s?q={q}", params=["q"], extract={"items": "li.r"},
                       expect={"selector": "li.r", "min_items": 3,
                               "identity": {"final_url": "rendered_url"},
                               RC.CONTRACT_KEY: RC.replay_contract("q")})
    out = ReplayExecutor(driver=Driver(dom)).execute(t, {"q": "大学"}, empty=product())
    assert not out["success"] and "inside a longer word" in out["error"], out
    ex = ReplayExecutor(driver=Driver(dom))
    with MC.scope("product"), MC.using(reads("大学")):
        out = ex.execute(t, {"q": "大学"}, empty=product())
    assert out["success"] and out["model_calls"] >= 1 and ex.model_calls >= 1, out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
