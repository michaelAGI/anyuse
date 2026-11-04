"""Host recomputation of the measured desktop discovery.

The five dumps in ``testdata/discovery/`` are the real guest dumps
(``execution/guest/discover_desktop.py dump``, German guest, Blender
5.2.2 LTS), gzipped. This test re-runs the pure ``discover_app`` on them and
requires every count to equal what the guest computed
(``guest_counts.json``), and pins the class outcomes fixed in advance (``EXPECTED_CLASSES``). Pure: no pyobjc, no
subprocess.

    python3 test_desktop_discover_real.py
"""
import gzip
import json
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

import anyuse  # noqa: E402
assert pathlib.Path(anyuse.__file__).resolve().parent == _HERE, "wrong checkout (D9)"

from anyuse.execution import desktop_discover as dd  # noqa: E402

D = _HERE / "testdata" / "discovery"
EXPECTED_CLASSES = {
    "com.apple.calculator": ["arithmetic"],
    "com.apple.TextEdit": ["file_write"],
    "com.apple.finder": ["directory_op/create"],
    "com.apple.Notes": [],
    "org.blenderfoundation.blender": ["app_script/mesh_add_move"],
}


def _dump(bid):
    with gzip.open(D / f"dump_{bid}.json.gz", "rt") as fh:
        return json.load(fh)


def test_host_recomputation_equals_the_guest():
    guest = {a["bundle_id"]: a["counts"] for a in json.load(open(D / "guest_counts.json"))["apps"]}
    assert set(guest) == set(EXPECTED_CLASSES)
    for bid, counts in guest.items():
        host = dd.discover_app(_dump(bid)).counts()
        assert host == counts, f"{bid}: host {host} != guest {counts}"
    print(f"OK  host recomputation equals the guest's counts for all {len(guest)} apps")


def test_expected_classes():
    for bid, want in EXPECTED_CLASSES.items():
        d = dd.discover_app(_dump(bid))
        assert d.counts()["classes"] == want, (bid, d.counts()["classes"])
        assert all(k.status == "mapped" for k in d.classes), "nothing is proved by discovery alone"
    print("OK  mapped classes are exactly the expected ones; all start 'mapped', none 'proved'")


def test_every_unresolved_candidate_has_a_reason_and_keys_are_unique():
    for bid in EXPECTED_CLASSES:
        d = dd.discover_app(_dump(bid))
        assert all(c.reason for c in d.candidates)
        keys = [(c.source, c.key) for c in d.candidates]
        assert len(keys) == len(set(keys)), f"{bid}: duplicate candidate keys"
    print("OK  every candidate carries a reason; (source, key) is unique per app")


def test_triggers_are_language_independent():
    for bid in EXPECTED_CLASSES:
        d = dd.discover_app(_dump(bid))
        by_key = {c.key: c for c in d.candidates}
        for k in d.classes:
            for t in k.triggers:
                assert by_key[t].key_kind in ("identifier", "shortcut", "idname"), (bid, t, by_key[t].key_kind)
    print("OK  every mapped trigger is keyed by identifier, shortcut or idname, never a localized title")


def main():
    for t in (test_host_recomputation_equals_the_guest, test_expected_classes,
              test_every_unresolved_candidate_has_a_reason_and_keys_are_unique,
              test_triggers_are_language_independent):
        t()
    print("\nDESKTOP DISCOVER REAL-DUMP TEST PASSED SUCCESSFULLY!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
