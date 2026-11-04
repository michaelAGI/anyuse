"""Labels in any script: the normalisation the compiler and the proof match with.

``api_compile.normalise`` once kept only ``[0-9a-z]``: a Japanese, Russian,
Greek or Arabic label normalised to "" and was dropped, so such a site had no
ground truth and could not compile. These tests pin what replaced it:

* ASCII text normalises exactly as before;
* letters and digits of every script are kept, and a precomposed and a
  decomposed accent, full-width and half-width forms compare equal;
* the accents of letters with case are dropped (ΑΘΗΝΑ = Αθήνα, MÜNCHEN =
  München), digits of every script compare by value (٣ = 3), and the optional
  vowel marks of Arabic and Hebrew are dropped; other marks stay;
* the word checks (the sparse contract, the result contract's identity key,
  the "no results" binding, the landing title, discovery's page checks) find
  whole words in every script, and any substring in a script written without
  spaces between words;
* a two-ideograph search value binds inside a nested layer and as part of a
  path segment, like a three-letter one;
* a search whose labels are Japanese, Russian, Greek or Arabic compiles and
  passes the fresh-input proof.

Offline: inline recordings and an injected transport. No desktop, no network.
"""
import json
import random
import re
import unicodedata
import urllib.parse

from anyuse.execution import api_compile as AC
from anyuse.execution import result_contract as RC
from anyuse.execution.api_compile import compile_recording, normalise, visible_items
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
from anyuse.execution.api_verify import compile_and_verify, names_value, sparse_identity


def _old_rule(text):
    """The ASCII-only rule, kept here to pin that ASCII output did not change."""
    if text is None:
        return ""
    s = re.sub(r"[^0-9a-z]+", " ", str(text).lower())
    return re.sub(r"\s+", " ", s).strip()


def nfd(s):
    return unicodedata.normalize("NFD", s)


# ---------------------------------------------------------------------------
# normalise
# ---------------------------------------------------------------------------

def test_ascii_text_normalises_exactly_as_before():
    rng = random.Random(7)
    alphabet = "abcXYZ019 -_./,'\"!?\t\n"
    for _ in range(5000):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        assert normalise(s) == _old_rule(s), s
    for s in (None, 12, 4.5, True, "Lazy-Regex", "  A/B  c ", "serde_json"):
        assert normalise(s) == _old_rule(s)


def test_letters_of_every_script_are_kept():
    assert normalise("東京タワー") == "東京タワー"
    assert normalise("Москва-Сити") == "москва сити"
    assert normalise("ΑΘΉΝΑ Κέντρο") == "αθηνα κεντρο"
    assert normalise("مكتبة القاهرة") == "مكتبة القاهرة"
    assert normalise("서울특별시") == "서울특별시"
    assert normalise("München Straße") == "munchen strasse"


def test_composed_and_decomposed_forms_compare_equal():
    for s in ("Café", "Ηράκλειο", "Hội An", "ガイド", "서울", "बाज़ार"):
        assert normalise(nfd(s)) == normalise(unicodedata.normalize("NFC", s)), s
    assert normalise("ＡＢＣ－１２３") == normalise("ABC-123") == "abc 123"
    assert normalise("ｶﾞｲﾄﾞ") == normalise("ガイド")


def test_combining_marks_stay_inside_their_word():
    assert normalise("ภาษาไทย") == "ภาษาไทย"
    assert normalise("हिन्दी भाषा") == "हिन्दी भाषा"
    # Hebrew niqqud is optional: dropped on both sides
    assert normalise("שָׁלוֹם") == normalise("שלום") == "שלום"


def test_invisible_characters_are_dropped_without_splitting_the_word():
    assert normalise("‏مرحبا‏") == "مرحبا"
    assert normalise("Donau­dampf") == "donaudampf"
    assert normalise("می‌خواهم") == "میخواهم"
    assert normalise("مكـتبة") == normalise("مكتبة")
    assert normalise("葛\U000E0100飾") == "葛飾"


def test_two_ideographs_are_long_enough_and_two_kana_are_not():
    assert AC.match_length("大阪") == 4 and AC.match_length("serde") == 5
    assert AC.long_enough("大阪") and AC.long_enough("서울")
    assert not AC.long_enough("から") and not AC.long_enough("ab")
    assert AC.long_enough("abc") and not AC.long_enough("東")


# ---------------------------------------------------------------------------
# whole words
# ---------------------------------------------------------------------------

def test_word_spans_match_the_ascii_word_boundary_rule():
    rng = random.Random(11)
    for _ in range(3000):
        text = normalise("".join(rng.choice("ab c-") for _ in range(rng.randint(0, 12))))
        word = "".join(rng.choice("ab") for _ in range(rng.randint(1, 3)))
        rx = re.compile(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])")
        assert AC.word_spans(text, word) == [m.span() for m in rx.finditer(text)], (text, word)


def test_whole_words_in_spaced_scripts_any_substring_in_unspaced_ones():
    cw = AC.contains_words
    assert cw(normalise("Москва Сити"), normalise("москва"))
    assert not cw(normalise("Москвариум"), normalise("москва"))
    assert cw(normalise("東京タワー"), normalise("東京"))
    assert cw(normalise("iPhoneケース"), "iphone")
    assert not cw(normalise("iPhones"), "iphone")
    assert cw(normalise("서울특별시청"), normalise("서울"))


# ---------------------------------------------------------------------------
# the proof's and the runtime contract's word checks
# ---------------------------------------------------------------------------

def test_sparse_contract_reads_non_latin_results():
    assert names_value("東京タワー", "東京")
    assert names_value("Москва Сити", "москва")
    assert not names_value("Москвариум", "москва")
    ok, why = sparse_identity(["Αθήνα Κέντρο", "Αθήνα Λιμάνι"],
                              [nfd("Αθήνα Κέντρο"), nfd("Αθήνα Λιμάνι")], "αθήνα")
    assert ok, why
    ok, _why = sparse_identity(["大阪城", "大阪駅"], ["大阪城", "大阪駅"], "東京")
    assert not ok


def test_identity_key_names_the_query_in_any_script():
    assert RC.key_names_query("東京タワー", "東京")
    assert RC.key_names_query("Казань Кремль", "казань")
    assert RC.key_names_query(nfd("Ηράκλειο Λιμάνι"), "ηράκλειο")
    assert not RC.key_names_query("Казанский вокзал", "казань")
    assert not RC.key_names_query("大阪城", "東京")
    # unchanged for ASCII: a contiguous run of whole tokens, in order
    assert RC.key_names_query("mingw-w64-boost", "boost")
    assert not RC.key_names_query("boostnote", "boost")
    assert not RC.key_names_query("turbo-libjpeg", "libjpeg turbo")


def test_a_card_heading_without_ascii_is_its_identity_key():
    from anyuse.execution.html_extract import parse, select
    root = parse("<ul><li class='r'><h3><a>東京タワー</a></h3><p>★ 4.5 · 120</p></li></ul>")
    record = select(root, "li.r")[0]
    assert RC.html_item_key(record) == "東京タワー"


def test_no_results_statement_binds_a_decomposed_query():
    phrase, why = RC.empty_evidence_text("Keine Ergebnisse für München gefunden.", nfd("München"))
    assert phrase, why


def test_row_texts_name_a_non_latin_value():
    from anyuse.execution.html_extract import _names
    assert _names("東京", ["東京タワー", "東京駅"])
    assert _names("москва", ["Москва Сити", "Казань"])
    assert not _names("москва", ["Москвариум"])


def test_landing_title_names_a_non_latin_value():
    from anyuse.execution.learn_loop import title_matches
    ok, why = title_matches("東京タワー - 観光ガイド", "東京タワー", "大阪城 - 観光ガイド", "大阪城")
    assert ok, why
    ok, _why = title_matches("Москва — Путеводитель", "Москва", "Главная — Путеводитель", "Казань")
    assert not ok


def test_a_word_is_not_found_inside_a_longer_word_of_its_script():
    from anyuse.execution import discover as D
    from anyuse.execution.agent_browser import AgentRun
    from anyuse.execution.learn_loop import title_matches
    ok, why = title_matches("Казань — Путеводитель", "Казань",
                            "Москвариум — Путеводитель", "Москва")
    assert not ok, why
    assert not D.field_carries_key("Москвариум на ВДНХ", "Москва")
    run = AgentRun(value="Москва", start_url="https://fix.test/", final_url="https://fix.test/",
                   dom="<html><body><ul><li>Москвариум</li></ul></body></html>")
    ok, why = D.typed_page_check(run)
    assert not ok, why
    phrase, why = RC.empty_evidence_text("Keine Ergebnisse für Lünchen gefunden.", "München")
    assert not phrase, why


def test_a_detail_title_in_another_script_confirms_the_value():
    from types import SimpleNamespace
    from anyuse.execution.discover import confirm_rule
    page = "<html><head><title>大阪城 - 観光</title></head><body><h1>大阪城</h1></body></html>"
    cand = SimpleNamespace(recording={"dom": page}, ok=True, error="", blocked="",
                           value="大阪城", start_url="https://fix.test/",
                           final_url="https://fix.test/item/1", title="大阪城 - 観光", preview=[])
    found, confirmed, why = confirm_rule(cand, "detail")
    assert found and confirmed, why
    cand.title = "京都御所 - 観光"
    found, confirmed, why = confirm_rule(cand, "detail")
    assert found and not confirmed, why


def test_a_two_ideograph_value_binds_inside_a_nested_layer():
    from anyuse.execution import api_slots as S

    def url(v):
        return "https://fix.test/api/search?filters=" + urllib.parse.quote(
            json.dumps({"q": v, "page": 1}, ensure_ascii=False))
    slots, why = S.discover_slots(url("大阪"), None, "大阪", "q")
    assert [s.at for s in slots] == [("query:filters", "json:/q")], why
    assert S.fill_request(url("大阪"), None, slots, {"q": "東京"})[0] == url("東京")
    for short in ("から", "ab", "東"):
        slots, why = S.discover_slots(url(short), None, short, "q")
        assert not slots and "shorter than" in why, (short, why)
    rec = _recording("大阪", LISTS["ja"]["大阪"])
    rec["network"][1]["url"] = url("大阪")
    compiled = compile_recording(rec)
    assert compiled.template is not None, compiled.reason
    assert compiled.template.slots == [{"param": "q", "at": ["query:filters", "json:/q"]}]


def test_a_two_ideograph_value_binds_as_part_of_a_path_segment():
    from anyuse.execution.param_binding import sites_for

    def parts(value):
        url = "https://fix.test/s/" + urllib.parse.quote(value + "-hotels")
        return [(s.where, s.prefix, s.suffix) for s in sites_for(url, value)]
    assert ("path_part", "", "-hotels") in parts("大阪")
    assert ("path_part", "", "-hotels") in parts("rust")
    assert not any(w == "path_part" for w, _p, _s in parts("から"))
    assert not any(w == "path_part" for w, _p, _s in parts("ab"))


def test_discovery_page_checks_read_non_latin_text():
    from anyuse.execution import discover as D
    from anyuse.execution.agent_browser import AgentRun, page_kind_check
    assert D.field_carries_key("Казань — столица Татарстана", "Казань")
    assert not D.field_carries_key("Казанский собор", "Казань")
    assert D.label_matches("大阪城", ["大阪城 - 公式サイト"])
    page = "<html><head><title>大阪城 - 観光</title></head><body><h1>大阪城</h1></body></html>"
    run = AgentRun(value="大阪城", start_url="https://fix.test/",
                   final_url="https://fix.test/item/1", dom=page)
    ok, why = page_kind_check(run)
    assert ok, why
    run = AgentRun(value="大阪城", start_url="https://fix.test/", final_url="https://fix.test/",
                   dom="<html><body><input value='x'><ul><li>大阪城</li></ul></body></html>")
    ok, why = D.typed_page_check(run)
    assert ok, why


# ---------------------------------------------------------------------------
# end to end: a search in another script compiles and proves
# ---------------------------------------------------------------------------

LISTS = {
    "ja": {"東京": ["東京タワー", "東京駅", "東京大学", "東京ドーム"],
           "大阪": ["大阪城", "大阪駅", "大阪大学", "大阪ドーム"],
           "京都": ["京都御所", "京都駅", "京都大学", "京都タワー"]},
    "ru": {"Москва": ["Москва Центр", "Москва Вокзал", "Москва Кремль", "Москва Парк"],
           "Казань": ["Казань Центр", "Казань Вокзал", "Казань Кремль", "Казань Парк"],
           "Самара": ["Самара Центр", "Самара Вокзал", "Самара Кремль", "Самара Парк"]},
    "el": {"Αθήνα": ["Αθήνα Κέντρο", "Αθήνα Λιμάνι", "Αθήνα Μουσείο", "Αθήνα Πάρκο"],
           "Πάτρα": ["Πάτρα Κέντρο", "Πάτρα Λιμάνι", "Πάτρα Μουσείο", "Πάτρα Πάρκο"],
           "Βόλος": ["Βόλος Κέντρο", "Βόλος Λιμάνι", "Βόλος Μουσείο", "Βόλος Πάρκο"]},
    "ar": {"القاهرة": ["القاهرة مكتبة", "القاهرة جامعة", "القاهرة متحف", "القاهرة مطار"],
           "دبي": ["دبي مكتبة", "دبي جامعة", "دبي متحف", "دبي مطار"],
           "تونس": ["تونس مكتبة", "تونس جامعة", "تونس متحف", "تونس مطار"]},
}


def _body(names, decompose):
    rows = [nfd(n) if decompose else n for n in names]
    return json.dumps({"results": [{"name": n, "id": i} for i, n in enumerate(rows)]},
                      ensure_ascii=False)


def _recording(value, names, decompose=False):
    q = urllib.parse.quote(value)
    return {"param": {"name": "q", "value": value}, "site": "fix.test",
            "final_url": f"https://fix.test/search?q={q}",
            "ui": {"items": list(names), "text": "\n".join(names)},
            "network": [{"url": f"https://fix.test/search?q={q}", "method": "GET", "status": 200,
                         "mime": "text/html", "body": "<html><div id=app></div></html>"},
                        {"url": f"https://fix.test/api/search?q={q}", "method": "GET",
                         "status": 200, "mime": "application/json",
                         "body": _body(names, decompose)}]}


class _Site:
    def __init__(self, lists, decompose=False):
        self.lists, self.decompose = lists, decompose

    def send(self, method, url, headers, body, timeout_s):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0]
        return ApiResponse(200, {"Content-Type": "application/json"},
                           _body(self.lists.get(q, []), self.decompose))


def _prove(lang, decompose=False):
    lists = LISTS[lang]
    values = list(lists)
    rec = _recording(values[0], lists[values[0]], decompose)
    fresh = [_recording(v, lists[v], decompose) for v in values[1:]]
    return compile_and_verify(rec, fresh, executor=ApiStepExecutor(transport=_Site(lists, decompose)),
                              preflight=lambda _c: "")


def test_non_latin_labels_are_ground_truth():
    rec = _recording("東京", LISTS["ja"]["東京"])
    assert visible_items(rec) == LISTS["ja"]["東京"]
    assert compile_recording(rec).template is not None


def test_searches_in_four_scripts_compile_and_prove():
    for lang in LISTS:
        compiled, proof = _prove(lang)
        assert compiled.template is not None, (lang, compiled.reason)
        assert compiled.template.extract == {"items": "results[*].name"}, lang
        assert proof.passed, (lang, proof.reason)


def test_a_response_in_another_normal_form_still_proves():
    for lang in ("el", "ja"):
        compiled, proof = _prove(lang, decompose=True)
        assert compiled.template is not None and proof.passed, (lang, proof.reason)


def _capitals(s):
    """``s`` as a page writes it in capitals: upper case, the accents left off."""
    return "".join(ch for ch in nfd(s).upper() if not unicodedata.combining(ch))


_ARABIC_INDIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def _prove_shown(lists, shown, decompose=False):
    """Compile and prove a site whose API returns ``lists`` and whose page shows
    each label as ``shown(label)``."""
    values = list(lists)

    def rec(v):
        r = _recording(v, lists[v], decompose)
        labels = [shown(n) for n in lists[v]]
        r["ui"] = {"items": labels, "text": "\n".join(labels)}
        return r
    return compile_and_verify(rec(values[0]), [rec(v) for v in values[1:]],
                              executor=ApiStepExecutor(transport=_Site(lists, decompose)),
                              preflight=lambda _c: "")


def test_a_page_in_capitals_without_accents_proves():
    assert _capitals("Αθήνα Κέντρο") == "ΑΘΗΝΑ ΚΕΝΤΡΟ"
    for decompose in (False, True):
        compiled, proof = _prove_shown(LISTS["el"], _capitals, decompose)
        assert compiled.template is not None, compiled.reason
        assert proof.passed, proof.reason


def test_labels_with_other_digits_prove():
    lists = {v: [f"{v} {kind} {i}" for i, kind in enumerate(("مكتبة", "جامعة", "متحف", "مطار"), 1)]
             for v in LISTS["ar"]}
    compiled, proof = _prove_shown(lists, lambda n: n.translate(_ARABIC_INDIC))
    assert compiled.template is not None, compiled.reason
    assert proof.passed, proof.reason


def test_a_response_that_ignores_the_query_does_not_prove():
    lists = LISTS["ja"]
    values = list(lists)
    deaf = {v: lists[values[0]] for v in values}
    rec = _recording(values[0], lists[values[0]])
    fresh = [_recording(v, lists[v]) for v in values[1:]]
    _compiled, proof = compile_and_verify(rec, fresh, executor=ApiStepExecutor(transport=_Site(deaf)),
                                          preflight=lambda _c: "")
    assert not proof.passed


if __name__ == "__main__":
    import inspect
    import sys
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:
            f += 1; print("FAIL", n, "::", repr(e)[:300])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
