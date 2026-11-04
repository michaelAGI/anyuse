"""An app's scripting dictionary read at runtime from its own bundle (``execution/script_dictionary.py``).

Pinned here:

* the guest dumps of Finder, Notes and TextEdit parse to the recorded counts (commands, properties)
  and the model survives its JSON round trip (the cache);
* reading a bundle is FILE READS ONLY: ``Info.plist`` -> the sdef, its ``xi:include``s only from the bundle or
  ``/System/Library/ScriptingDefinitions/``; an include elsewhere is not read; the legacy ``.scriptSuite`` /
  ``.scriptTerminology`` (old-style property lists) parse without ``plutil``; the module never runs a process (the
  ``sdef`` tool is an Xcode shim in the guest);
* the cache is per app VERSION: a hit parses nothing, a new version or a changed dictionary file is read again, the
  JSON copy reloads.
No desktop, no app, no osascript, no network.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_script_dictionary.py
"""
import gzip
import json
import os
import plistlib
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import script_dictionary as SD  # noqa: E402

DATA = os.path.join(HERE, "testdata", "discovery")
COCOA_HREF = "file://localhost/System/Library/ScriptingDefinitions/CocoaStandard.sdef"


def dump(bid):
    with gzip.open(os.path.join(DATA, f"dump_{bid}.json.gz"), "rt") as fh:
        return json.load(fh)


COCOA = dump("com.apple.Notes")["sdef"]["includes"][COCOA_HREF]


@pytest.mark.parametrize("bid,commands,props", [("com.apple.finder", 24, 167), ("com.apple.Notes", 13, 45),
                                                ("com.apple.TextEdit", 11, 19)])
def test_the_guest_dumps_parse_to_expected_counts(bid, commands, props):
    d = SD.from_dump(dump(bid), COCOA)
    s = SD.summary(d)
    assert (s["commands"], s["properties"]) == (commands, props)
    assert SD.AppDictionary.from_dict(json.loads(json.dumps(d.to_dict()))).to_dict() == d.to_dict()


def test_the_model_resolves_inheritance_elements_enumerations_and_hidden_terms():
    f = SD.from_dump(dump("com.apple.finder"), COCOA)
    assert f.class_by_code("brow").inherits == "window" and f.is_a("desktop window", "window")
    assert f.prop_owner("desktop window", "fvtg") == "Finder window"            # the most general declaring class
    assert "column" in f.elements("list view options")
    assert ("modification date column", "elsm") in f.enums["elsv"].enumerators
    assert f.enums["sort"].hidden and not f.enums["elsv"].hidden                # a hidden suite hides its terms
    n = SD.from_dump(dump("com.apple.Notes"), COCOA)
    assert n.props("note")["text"].name == "plaintext" and n.props("note")["text"].access == "r"
    assert n.props("note")["body"].writable and n.responds("note") == {"show"}
    assert n.props("application")["seln"].is_list and n.props("application")["seln"].types == ("note",)
    t = SD.from_dump(dump("com.apple.TextEdit"), COCOA)
    assert t.source == "script_suite" and set(t.props("document")) == {"pnam", "imod", "file", "ctxt"}
    assert t.command("coresave") is not None and t.application().name == "application"


def test_old_style_property_lists_parse_without_plutil():
    text = b'{ "Name" = "X"; /* c */ "Classes" = { Doc = { AppleEventCode = docu; Superclass = "NSCoreSuite.NSDocument"; }; }; // e\n List = (a, "b c"); }'
    v = SD.load_plist_bytes(text)
    assert v == {"Name": "X", "Classes": {"Doc": {"AppleEventCode": "docu", "Superclass": "NSCoreSuite.NSDocument"}},
                 "List": ["a", "b c"]}
    assert SD.load_plist_bytes(plistlib.dumps({"a": 1})) == {"a": 1}


SDEF = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE dictionary SYSTEM "file://localhost/System/Library/DTDs/sdef.dtd">
<dictionary xmlns:xi="http://www.w3.org/2003/XInclude">
  <xi:include href="file:///System/Library/ScriptingDefinitions/CocoaStandard.sdef" xpointer="xpointer(/dictionary/suite)"/>
  <xi:include href="file://localhost{bundle}/Contents/Resources/Extra.sdef"/>
  <xi:include href="file:///etc/Elsewhere.sdef"/>
  <suite name="Todo Suite" code="TODO">
    <class name="item" code="TDit" plural="items"><property name="name" code="pnam" type="text"/></class>
  </suite>
</dictionary>"""
EXTRA = """<dictionary><suite name="Extra" code="EXTR"><class name="tag" code="TDtg"/></suite></dictionary>"""


def _bundle(tmp, version="1.0", build="7", name="Todo"):
    app = tmp / f"{name}.app"
    res = app / "Contents" / "Resources"
    res.mkdir(parents=True, exist_ok=True)
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleIdentifier": f"org.example.{name.lower()}", "CFBundleShortVersionString": version,
                       "CFBundleVersion": build, "OSAScriptingDefinition": "Todo.sdef"}, fh)
    (res / "Todo.sdef").write_text(SDEF.replace("{bundle}", str(app)), encoding="utf-8")
    (res / "Extra.sdef").write_text(EXTRA, encoding="utf-8")
    return app


def test_reading_a_bundle_follows_includes_only_from_allowed_roots(tmp_path):
    app = _bundle(tmp_path)
    dump_ = SD.read_bundle(str(app))
    assert (dump_["bundle_id"], dump_["version"], dump_["build"]) == ("org.example.todo", "1.0", "7")
    inc = dump_["sdef"]["includes"]
    assert any(h.endswith("Extra.sdef") for h in inc)                          # the bundle's own include
    assert not any("Elsewhere" in h for h in inc)                              # never outside the allowed roots
    d = SD.from_dump(dump_)
    assert d.klass("item").code == "TDit" and d.klass("tag").code == "TDtg" and d.digest
    if os.path.exists(SD.COCOA_STANDARD):                                      # the host's standard suite, a file
        assert d.command("corecrel") is not None
    assert any("not collected" in n for n in d.notes)                          # the refused include is noted


STANDARD = """<dictionary><suite name="Standard Suite" code="????">
  <command name="make" code="corecrel"><parameter name="new" code="kocl" type="type"/></command>
  <command name="save" code="coresave"><direct-parameter type="specifier"/></command>
  <command name="delete" code="coredelo"><direct-parameter type="specifier"/></command>
  <class name="application" code="capp"><element type="window"/></class>
  <class name="window" code="cwin"><property name="name" code="pnam" type="text" access="r"/></class>
</suite></dictionary>"""
HREF = "file:///System/Library/ScriptingDefinitions/CocoaStandard.sdef"


def test_an_include_inside_a_suite_brings_the_standard_nodes_minus_the_excluded_commands():
    # Calendar / Contacts / Mail: the include sits INSIDE a suite and its
    # xpointer keeps every node of the standard suites but the commands the app redefines
    app = """<dictionary xmlns:xi="http://www.w3.org/2003/XInclude"><suite name="Standard Suite" code="????">
      <xi:include href="%s" xpointer="xpointer(/dictionary/suite/node()[not(self::command and %s)])"/>
      <command name="save" code="coresave"><direct-parameter type="text"/></command>
    </suite><suite name="App" code="APPP"><class-extension extends="application"><element type="event"/>
    </class-extension><class name="event" code="wrev"/></suite></dictionary>"""
    one = SD.parse_sdef("x", app % (HREF, "@name = 'save'"), {HREF: STANDARD})
    assert one.application() is not None and one.elements("application") == ["window", "event"]
    assert one.command("coresave").direct.types == ("text",)                     # the app's own, not the standard
    assert one.command("coredelo") is not None and one.command("corecrel") is not None
    two = SD.parse_sdef("x", app % (HREF, "((@name = 'delete') or (@name = 'make'))"), {HREF: STANDARD})
    assert two.command("coredelo") is None and two.command("corecrel") is None and two.application() is not None
    assert SD.xpointer_filter("xpointer(/dictionary/suite)") == ("suites", "", frozenset())
    assert SD.xpointer_filter("xpointer(/dictionary/suite/node()[not(self::command and ((@name = 'a') or "
                              "(@name = 'b')))])") == ("nodes", "command", frozenset({"a", "b"}))
    # a command redefined hidden on top of the visible standard one stays visible, with
    # the app's own parameters (Calendar's make)
    redef = app.replace('<command name="save" code="coresave"><direct-parameter type="text"/></command>',
                        '<command name="make" code="corecrel" hidden="yes"><parameter name="new" code="kocl" '
                        'type="type"/><parameter name="with properties" code="prdt" type="record"/></command>')
    three = SD.parse_sdef("x", redef % (HREF, "@name = 'save'"), {HREF: STANDARD})
    assert three.command("corecrel") is not None and three.command("corecrel").param("prdt") is not None
    hidden_only = SD.parse_sdef("x", '<dictionary><suite name="S" code="SSSS"><command name="secret" code="abcdefgh" '
                                'hidden="yes"/></suite></dictionary>')
    assert hidden_only.command("abcdefgh") is None
    odd = SD.parse_sdef("x", '<dictionary xmlns:xi="http://www.w3.org/2003/XInclude"><xi:include href="%s" '
                        'xpointer="xpointer(//class)"/></dictionary>' % HREF, {HREF: STANDARD})
    assert odd.application() is not None and any("not understood" in n for n in odd.notes)


def test_the_cache_is_per_app_version(tmp_path):
    app = _bundle(tmp_path)
    c = SD.DictionaryCache(str(tmp_path / "cache"))
    a = c.get(str(app))
    assert c.get(str(app)) is a and c.reads == 1                               # a hit parses nothing
    c2 = SD.DictionaryCache(str(tmp_path / "cache"))
    assert c2.get(str(app)).to_dict() == a.to_dict() and c2.reads == 0          # the JSON copy reloads
    _bundle(tmp_path, version="2.0", build="8")
    assert c.get(str(app)).version == "2.0" and c.reads == 2                   # a new version is read again
    sd = app / "Contents" / "Resources" / "Todo.sdef"
    sd.write_text(sd.read_text(encoding="utf-8").replace('code="TDit"', 'code="TDiu"'), encoding="utf-8")
    os.utime(sd, ns=(1, 1))
    assert c.get(str(app)).klass("item").code == "TDiu" and c.reads == 3       # a changed dictionary file too


def test_a_bundle_without_a_dictionary_is_empty_and_the_module_runs_no_process(tmp_path):
    app = tmp_path / "Plain.app"
    (app / "Contents" / "Resources").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleIdentifier": "org.example.plain"}, fh)
    d = SD.read_app_dictionary(str(app))
    assert d.empty and d.source == "none"
    src = open(SD.__file__, encoding="utf-8").read()
    assert "subprocess" not in src and "os.system" not in src and "popen" not in src.lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
