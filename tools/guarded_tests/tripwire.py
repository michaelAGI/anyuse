"""Stat-only tripwire proving tests did not alter the user's protected data."""
import json
import os
import sys
import tempfile
import time

# The user's real AnyUse data dir; extra paths via ANYUSE_TRIPWIRE_DIRS (os.pathsep-separated).
WATCH_DIRS = ["~/.anyuse"] + [p for p in os.environ.get("ANYUSE_TRIPWIRE_DIRS", "").split(os.pathsep) if p]
WATCH_GLOBS = ["~/Library/Keychains"]


def _stat(path):
    try:
        st = os.lstat(path)
        return [st.st_size, st.st_mtime_ns]
    except OSError:
        return None


def snapshot(dirs=WATCH_DIRS, globs=WATCH_GLOBS):
    out = {}
    for directory in dirs:
        root = os.path.expanduser(directory)
        if not os.path.isdir(root):
            out[root] = None
            continue
        for base, subdirs, names in os.walk(root):
            subdirs[:] = [name for name in subdirs if name != ".git"]
            for name in names:
                path = os.path.join(base, name)
                out[path] = _stat(path)
    for directory in globs:
        root = os.path.expanduser(directory)
        if os.path.isdir(root):
            for name in os.listdir(root):
                if name.endswith(".keychain-db"):
                    path = os.path.join(root, name)
                    out[path] = _stat(path)
    return out


def diff(before, after):
    changes = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old != new:
            kind = "added" if old is None else ("removed" if new is None else "changed")
            changes.append(f"{kind}: {path}")
    return changes


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "tripwire-probe")
        with open(path, "w") as handle:
            handle.write("a")
        before = snapshot(dirs=[tmp], globs=[])
        assert not diff(before, snapshot(dirs=[tmp], globs=[]))
        time.sleep(0.01)
        with open(path, "w") as handle:
            handle.write("bb")
        assert diff(before, snapshot(dirs=[tmp], globs=[]))
    real = snapshot()
    print(f"tripwire selftest ok; watching {sum(v is not None for v in real.values())} real path(s)")
    return 0


def main(argv):
    if not argv or argv[0] == "--selftest":
        return selftest()
    if len(argv) != 2 or argv[0] not in {"snapshot", "check"}:
        return 2
    command, path = argv
    if command == "snapshot":
        with open(path, "w") as handle:
            json.dump(snapshot(), handle)
        return 0
    with open(path) as handle:
        before = json.load(handle)
    changes = diff(before, snapshot())
    if changes:
        print("TRIPWIRE: protected data changed during the run:")
        for change in changes[:40]:
            print("  " + change)
        return 1
    print(f"tripwire clean: {sum(v is not None for v in before.values())} real path(s) unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

