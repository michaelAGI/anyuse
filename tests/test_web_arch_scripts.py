"""The web stack's words in every script (execution/web_arch.py, execution/demo_compile.py).

``web_arch.tokens`` once kept only the runs of a-z and 0-9 of the folded text, so
a Russian, Greek, Arabic, Chinese or Japanese value had no words and never named
anything. These tests pin what replaced it:

* a text in Latin letters has exactly the tokens it had (random strings with
  accents, ł, ø, đ, İ, quotes and combining marks);
* a run of letters of another script is a token, cut where the script changes,
  and digits of every script read 0-9;
* a word of a script written without spaces is named by a token it starts
  (東京 by 東京駅), never by one it ends or holds (京都 in 東京都, 東京都庁); the
  compound rule for words of four or more letters stays for spaced scripts;
* a demonstration tool reads the words of every script from version 14; an
  older tool reads exactly as before.

Offline and pure.
"""
import asyncio
import random
import re
import sys
import unicodedata

import pytest

from anyuse.execution import demo_compile as D
from anyuse.execution import demo_tool as T
from anyuse.execution import web_arch as W


def test_latin_tokens_are_byte_identical():
    rng = random.Random(13)
    alphabet = "abcXYZ019 -_./" + "äöüßéèçñłøæœđİı" + "'’\"" + "̧́̈̇"
    for _ in range(5000):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        assert W.tokens(s) == re.findall(r"[0-9a-z]+", W.fold(s)), s
        assert D.tokens9(s) == re.findall(r"[0-9a-z]+", D.fold9(s)), s
        assert W.content_tokens(s) == [t for t in re.findall(r"[0-9a-z]+", W.fold(s))
                                       if t not in W.STOPWORDS and (len(t) >= 2 or t.isdigit())], s


def test_latin_runs_are_unchanged_next_to_other_scripts():
    rng = random.Random(17)
    alphabet = "abz09 -." + "äłİ" + "̧́" + "กข่้" + "カガ" + "゙" + "東" + "서" + "مَ"
    for _ in range(5000):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        latin = re.findall(r"[0-9a-z]+", W.fold(s))
        assert [t for t in W.tokens(s) if t.isascii()] == latin, s
        assert [t for t in D.tokens9(s) if t.isascii()] == re.findall(r"[0-9a-z]+", D.fold9(s)), s


def test_marks_of_letters_without_case_are_part_of_the_word():
    # a kana voicing mark, a Thai tone mark and a Devanagari virama make another word
    assert W.tokens("ガイド") != W.tokens("カイト")
    assert not W.names_all("カイト 3m", "ガイド") and not D.names_value9("カイト 3m", "ガイド")
    assert not W.names_all("บาน", "บ้าน") and not D.names_value9("บานเย็น", "บ้าน")
    assert W.tokens("हिन्दी") == ["हिन्दी"]
    # the same word in another normal form or width still names it
    nfd = unicodedata.normalize("NFD", "ガイド")
    assert W.names_all(f"{nfd} 3m", "ガイド") and W.names_all("ｶﾞｲﾄﾞ", "ガイド")
    assert W.names_all("บ้านสวน", "บ้าน") and D.names_value9("บ้าน สวน", "บ้าน")
    assert W.tokens("서울역") == ["서울역"] and W.names_all("서울역", "서울")
    # the accents of letters with case and the optional Arabic marks still fold away
    assert W.tokens("ΑΘΉΝΑ") == ["αθηνα"] and W.tokens("مَكْتَبَة") == W.tokens("مكتبة")


def test_tokens_of_every_script():
    assert W.tokens("Москва-Сити") == ["москва", "сити"]
    assert W.tokens("ΑΘΗΝΑ Κέντρο") == ["αθηνα", "κεντρο"]
    assert W.tokens("東京タワー2号") == ["東京", "タワー", "2", "号"]
    assert W.tokens("قسم ٣") == ["قسم", "3"]
    assert W.tokens("iPhoneケース") == ["iphone", "ケース"]
    assert W.content_tokens("号 x 東京") == ["号", "東京"]


def test_names_all_in_every_script():
    assert W.names_all("Москва Сити", "москва")
    assert not W.names_all("Москвариум", "москва")
    assert W.names_all("ΑΘΗΝΑ ΚΕΝΤΡΟ", "Αθήνα")
    assert W.names_all("東京駅", "東京")
    assert not W.names_all("東京都庁", "京都")
    assert not W.names_all("東京都", "京都")
    assert W.overlap("東京駅 出口", "東京") == 1.0
    assert W.list_contract(["京都駅", "京都大学"], "京都")[0]
    assert not W.list_contract(["東京都庁"], "京都")[0]


def test_compound_words_still_name_their_part():
    assert W.names_all_compound("Zementmörtel 25 kg", "Mörtel")
    assert W.names_all_compound("Putzmörtel", "Mörtel")
    assert W.names_all_compound("Estrichbeton 25 kg", "Beton")
    assert not W.names_all_compound("Rabatt", "Bat")
    assert W.names_all_compound("Железобетон М300", "бетон")
    # the compound rule is for scripts with spaces: an unspaced word must start the run
    assert not W.names_all_compound("東京都庁", "京都庁")


def test_demo_matching_reads_other_scripts():
    assert D.names_value9("Москва Центр", "Москва")
    assert D.name_share9("東京駅 出口", "東京") == 1.0
    assert not D.names_value9("東京都庁", "京都")
    assert D.content9("号") == ["号"]


def test_a_tool_before_version_14_reads_only_latin_words():
    with W.scripts(False):
        assert W.tokens("Москва Сити 2") == ["2"]
        assert D.tokens9("Москва Центр") == []
        assert not D.names_value9("Москва Центр", "Москва")
        assert not W.names_all("東京駅", "東京")
    assert W.every_script()


def test_compile_and_run_read_words_by_the_tools_version(monkeypatch):
    seen = []
    monkeypatch.setattr(D, "_compile_trace", lambda trace, version: seen.append(
        (version, W.every_script())) or {"ok": False})
    D.compile_trace({}, version=13)
    D.compile_trace({}, version=14)
    D.compile_trace({})
    assert seen == [(13, False), (14, True), (D.TOOL_VERSION, True)]
    assert D.TOOL_VERSION >= 14 and D.v14({"version": 14}) and not D.v14({"version": 13})

    async def fake_run(page, tool, value, settle, wait, params):
        return {"version": tool["version"], "every_script": W.every_script(), "pages": []}
    monkeypatch.setattr(T, "_run_tool", fake_run)
    for version, every in ((12, False), (13, False), (14, True)):
        out = asyncio.run(T.run_tool(object(), {"version": version}, "x", lambda *_a: None))
        assert out["every_script"] is every, (version, out)
    assert W.every_script()

    # the repair ladder's own checks read the way the tool it repairs does
    from anyuse.execution import repair_ladder as LD

    async def fake_ladder(self, value):
        return {"every_script": W.every_script()}
    monkeypatch.setattr(LD._Ladder, "run", fake_ladder)
    for version, every in ((12, False), (14, True)):
        out = asyncio.run(LD.run_with_ladder(None, {"version": version}, "x", lambda *_a: None,
                                             config=LD.LadderConfig(enabled=True), store=object()))
        assert out["every_script"] is every, (version, out)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
