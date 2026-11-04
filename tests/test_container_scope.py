"""context/container_scope.py: a document-less window's container identity.

Pure checks over a tiny fake tree: what is read per app (Finder: AXDocument, AXURL, a POSIX title,
a URL-bearing element outside the lists, else the title; Notes: the selected folder row with its
account and parents, else the folder named in the title), what is content (a row of a list other
than the sidebar), the salted storage key, and the three verdicts of ``same_scope``. The same
functions on the product tree over a fake AX layer. No desktop, no model.

Run from the anyuse package dir:
    cd anyuse && python3 test_container_scope.py
"""
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.context import container_scope as CS  # noqa: E402

SALT = b"s" * 32
OTHER_SALT = b"o" * 32


class E:
    """A fake element: role, attributes, children (parents linked)."""

    def __init__(self, role, *kids, **attrs):
        self.a = {"AXRole": role, **{k: v for k, v in attrs.items() if v is not None}}
        self.kids = list(kids)
        for k in kids:
            k.a["AXParent"] = self


class T:
    def __init__(self, app):
        self.app = app

    def attr(self, el, name):
        return el.a.get(name) if el is not None else None

    def children(self, el):
        return list(el.kids) if el is not None else []

    def bundle_id(self):
        return self.app


def _row(label, **attrs):
    return E("AXRow", E("AXCell", E("AXStaticText", AXValue=label)), **attrs)


def _finder_window(title="Kistenpreise Herbst", files=("Preise-Gross-B.txt",), **wattrs):
    side = E("AXOutline", _row("Schreibtisch"), _row("Dokumente"), AXDescription="Seitenleiste")
    lst = E("AXOutline", *[_row(f) for f in files], AXDescription="Listendarstellung", AXIdentifier="ListView")
    win = E("AXWindow", E("AXSplitGroup", E("AXScrollArea", side), E("AXScrollArea", lst)),
            E("AXButton", AXDescription="Zurück"), AXTitle=title, **wattrs)
    return win, side, lst


def test_finder_reads_the_window_title_only_when_nothing_names_the_path():
    win, _s, _l = _finder_window()
    sc = CS.container_scope(T(CS.FINDER), win)
    assert sc == {"kind": "finder_folder", "strength": "title", "key": "", "title": "Kistenpreise Herbst",
                  "source": "title"}, sc


def test_finder_reads_the_full_path_from_document_url_title_or_a_proxy_element():
    win, _s, _l = _finder_window(AXDocument="file:///Users/m/A/Kistenpreise%20Herbst/")
    assert CS.container_scope(T(CS.FINDER), win)["key"] == "/Users/m/A/Kistenpreise Herbst"
    win, _s, _l = _finder_window(AXURL="file:///Users/m/B/Kistenpreise%20Herbst/")
    sc = CS.container_scope(T(CS.FINDER), win)
    assert (sc["strength"], sc["key"], sc["title"], sc["source"]) == (
        "path", "/Users/m/B/Kistenpreise Herbst", "Kistenpreise Herbst", "AXURL"), sc
    win, _s, _l = _finder_window(title="/Users/m/C/Kistenpreise Herbst")
    assert CS.container_scope(T(CS.FINDER), win)["source"] == "title_path"
    win, _s, _l = _finder_window()
    win.kids.append(E("AXImage", AXURL="file:///Users/m/D/Kistenpreise%20Herbst/"))
    sc = CS.container_scope(T(CS.FINDER), win)
    assert (sc["key"], sc["source"]) == ("/Users/m/D/Kistenpreise Herbst", "url_element"), sc


def test_a_file_row_named_like_the_folder_never_becomes_the_folders_path():
    # a row's URL (a file inside the folder) is not read: lists are not entered
    win, _s, lst = _finder_window(title="Archiv")
    lst.kids.append(E("AXRow", E("AXCell", E("AXStaticText", AXValue="Archiv")), AXURL="file:///Users/m/Archiv/Archiv"))
    assert CS.container_scope(T(CS.FINDER), win)["strength"] == "title"


def _notes_window(title="Arzt – 3 Notizen", selected="Arzt", levels=True, by_row=False):
    rows = []
    spec = [("iCloud", 0), ("Notizen", 1), ("Arzt", 1), ("Auf meinem Mac", 0), ("Arzt", 1)]
    for i, (lab, lv) in enumerate(spec):
        rows.append(E("AXRow", E("AXCell", E("AXStaticText", AXValue=lab)),
                      AXDisclosureLevel=lv if levels else None, AXSelected=(i == 2 and lab == selected) or None))
    if by_row:
        for r in rows:
            r.a.pop("AXDisclosureLevel", None)
        rows[1].a["AXDisclosedByRow"] = rows[0]
        rows[2].a["AXDisclosedByRow"] = rows[0]
        rows[4].a["AXDisclosedByRow"] = rows[3]
    folders = E("AXOutline", *rows, AXDescription="Ordner")
    notes = E("AXTable", _row("Zahnarzt Kontrolle"), AXIdentifier="ICMNoteListTableView_asList")
    win = E("AXWindow", E("AXSplitGroup", E("AXScrollArea", folders), E("AXScrollArea", notes)), AXTitle=title)
    return win, folders, notes


def test_notes_reads_the_selected_folder_with_its_account():
    win, _f, _n = _notes_window()
    sc = CS.container_scope(T(CS.NOTES), win)
    assert (sc["strength"], sc["key"], sc["title"]) == ("path", "iCloud/Arzt", "Arzt"), sc
    win, _f, _n = _notes_window(by_row=True)
    assert CS.container_scope(T(CS.NOTES), win)["key"] == "iCloud/Arzt"


def test_notes_without_a_parent_relation_or_selection_falls_back_to_the_folder_in_the_title():
    win, _f, _n = _notes_window(levels=False)
    sc = CS.container_scope(T(CS.NOTES), win)
    assert (sc["strength"], sc["key"], sc["title"]) == ("title", "", "Arzt"), sc
    win, _f, _n = _notes_window(selected="none")
    assert CS.container_scope(T(CS.NOTES), win)["strength"] == "title"


def test_the_notes_title_count_is_not_part_of_the_folder_name():
    assert CS.scope_title(CS.NOTES, "Preise Werkstatt – 3 Notizen") == "Preise Werkstatt"
    assert CS.scope_title(CS.NOTES, "Arzt — 1 Note") == "Arzt"
    assert CS.scope_title(CS.NOTES, "Preise Werkstatt – 3 Notizen (alt)") == "Preise Werkstatt – 3 Notizen (alt)"
    assert CS.scope_title(CS.FINDER, "A – 3 B") == "A – 3 B"


def test_no_reader_no_window_no_scope():
    win, _s, _l = _finder_window()
    assert CS.container_scope(T("com.apple.TextEdit"), win) is None
    assert CS.container_scope(T(CS.FINDER), None) is None
    assert CS.container_scope(T(CS.FINDER), E("AXWindow")) is None


def test_content_is_a_row_of_a_list_other_than_the_sidebar():
    win, side, lst = _finder_window()
    t = T(CS.FINDER)
    file_text = lst.kids[0].kids[0].kids[0]
    assert CS.in_container_content(t, file_text) and CS.in_container_content(t, lst.kids[0])
    assert not CS.in_container_content(t, side.kids[0].kids[0].kids[0])
    assert not CS.in_container_content(t, win.kids[1])                         # a toolbar button
    nwin, folders, notes = _notes_window()
    assert CS.in_container_content(T(CS.NOTES), notes.kids[0])
    assert not CS.in_container_content(T(CS.NOTES), folders.kids[2])
    assert not CS.in_container_content(T("com.apple.TextEdit"), lst.kids[0])  # no reader: no rule


def test_same_scope_rules():
    a_path = {"kind": "finder_folder", "strength": "path", "key": "/A/X", "title": "X", "source": "AXURL"}
    b_path = dict(a_path, key="/B/X")
    x_title = {"kind": "finder_folder", "strength": "title", "key": "", "title": "X", "source": "title"}
    y_title = dict(x_title, title="X (alt)")
    assert CS.same_scope(a_path, dict(a_path)) == CS.SAME
    assert CS.same_scope(a_path, b_path) == CS.DIFFERENT                 # same name, another folder
    assert CS.same_scope(x_title, y_title) == CS.DIFFERENT               # another name: another folder
    assert CS.same_scope(x_title, dict(x_title)) == CS.UNCONFIRMED       # a name never proves the folder
    assert CS.same_scope(a_path, x_title) == CS.UNCONFIRMED
    assert CS.same_scope(a_path, y_title) == CS.DIFFERENT
    assert CS.same_scope(x_title, dict(x_title, kind="notes_folder")) == CS.DIFFERENT
    assert CS.same_scope(None, x_title) == CS.UNCONFIRMED and CS.same_scope(x_title, None) == CS.UNCONFIRMED


def test_scope_key_hides_the_path_and_compares_like_the_raw_scope():
    a_path = {"kind": "finder_folder", "strength": "path", "key": "/Users/m/A/X", "title": "X", "source": "AXURL"}
    k = CS.scope_key(a_path, salt=SALT)
    assert CS.is_scope_key(k) and "/Users" not in k and "X" not in k.split(":", 3)[-1]
    assert CS.same_scope(k, dict(a_path), salt=SALT) == CS.SAME
    assert CS.same_scope(k, dict(a_path, key="/Users/m/B/X"), salt=SALT) == CS.DIFFERENT
    t = {"kind": "finder_folder", "strength": "title", "key": "", "title": "X", "source": "title"}
    kt = CS.scope_key(t, salt=SALT)
    assert CS.same_scope(kt, dict(t), salt=SALT) == CS.UNCONFIRMED
    assert CS.same_scope(kt, dict(t, title="Y"), salt=SALT) == CS.DIFFERENT
    assert CS.same_scope(k, dict(a_path), salt=OTHER_SALT) == CS.UNCONFIRMED   # another machine's key
    assert CS.scope_key(None, salt=SALT) == "" and CS.scope_key(a_path, salt=b"short") == ""


def test_scope_strength_of_a_scope_and_of_its_key():
    a_path = {"kind": "finder_folder", "strength": "path", "key": "/A/X", "title": "X", "source": "AXURL"}
    x_title = {"kind": "finder_folder", "strength": "title", "key": "", "title": "X", "source": "title"}
    assert CS.scope_strength(a_path) == "path" and CS.scope_strength(x_title) == "title"
    assert CS.scope_strength(CS.scope_key(a_path, salt=SALT)) == "path"
    assert CS.scope_strength(CS.scope_key(x_title, salt=SALT)) == "title"
    assert CS.scope_strength(None) == "" and CS.scope_strength("nonsense") == "" and CS.scope_strength({}) == ""


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:  # noqa: BLE001
            f += 1; print("FAIL", n, "::", repr(e)[:400])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
