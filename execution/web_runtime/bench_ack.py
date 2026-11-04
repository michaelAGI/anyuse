"""A BENCHMARK convention, not a product behaviour: acknowledge public hosts in
the bench's OWN scratch registry, as a stand-in for a person's terms check.

Why it exists. The proof's pre-send gate refuses a host no
human has acknowledged (``execution/api_permission.py``). The bench runs with
a scratch ``ANYUSE_DATA_DIR`` whose registry is empty, so every candidate on
every host was refused before its proof was sent (4 conversion
candidates, 4 map capabilities). That measured the empty registry, not the
learn loop.

What it does. With ``ANYUSE_BENCH_ACK_PUBLIC_HOSTS=1`` — and ONLY when
``ANYUSE_DATA_DIR`` and the registry file both resolve under a scratch root
(the system temp directory), never under the real ``~/.anyuse`` — it writes an
acknowledgement for each host it is given, with ``acknowledged_by="bench"`` and
the reason in the entry, and prints every host it acknowledged. The hosts are
the corpus hosts: public, login-free, read-only pages whose ``robots.txt``
allows both of our tokens at the root (the caller checks that and passes the
check's result as ``robots_ok``). It never records a robots override: the
robots policy stays ``respect``.
"""
from __future__ import annotations

import json
import os
import pwd
import tempfile
from typing import Callable, Dict, Iterable, List

FLAG = "ANYUSE_BENCH_ACK_PUBLIC_HOSTS"
BY = "bench"
REASON = ("benchmark convention: public, login-free, read-only "
          "corpus host, robots.txt allows both tokens at the root; stands in for "
          "the owner's terms check; not a product acknowledgement")


class BenchAckRefused(RuntimeError):
    """The flag is set but the registry is not a scratch registry."""


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _under(path: str, root: str) -> bool:
    path, root = _real(path), _real(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def scratch_roots() -> List[str]:
    return sorted({_real(tempfile.gettempdir()), _real("/private/tmp"), _real("/tmp")})


def repo_root() -> str:
    """The checkout / package root — never a scratch data directory, even when cloned under /tmp."""
    return _real(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def real_anyuse_dir() -> str:
    """The REAL ``~/.anyuse`` — from the password database, not ``$HOME``,
    which the bench redirects to scratch."""
    return os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".anyuse")


def check_scratch_registry(env=None) -> str:
    """The registry path the flag may write, or raise :class:`BenchAckRefused`."""
    env = os.environ if env is None else env
    data_dir = (env.get("ANYUSE_DATA_DIR") or "").strip()
    if not data_dir:
        raise BenchAckRefused(f"{FLAG} needs ANYUSE_DATA_DIR set to a scratch directory")
    if _under(data_dir, real_anyuse_dir()):
        raise BenchAckRefused(f"{FLAG} refused: ANYUSE_DATA_DIR is the real ~/.anyuse")
    checkout = repo_root()
    if _under(data_dir, checkout) or _under(checkout, data_dir):
        raise BenchAckRefused(f"{FLAG} refused: ANYUSE_DATA_DIR={data_dir!r} overlaps the checkout root {checkout!r}")
    if not any(_under(data_dir, root) for root in scratch_roots()):
        raise BenchAckRefused(f"{FLAG} refused: ANYUSE_DATA_DIR={data_dir!r} is not "
                              f"under a scratch root {scratch_roots()}")
    registry = (env.get("ANYUSE_API_HOSTS_FILE") or "").strip() or os.path.join(
        data_dir, "api_hosts.json")
    if not _under(registry, data_dir) or _under(registry, real_anyuse_dir()) or _under(registry, checkout):
        raise BenchAckRefused(f"{FLAG} refused: the host registry {registry!r} is not "
                              f"inside ANYUSE_DATA_DIR")
    return registry


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get(FLAG) or "").strip() == "1"


def acknowledge_public_hosts(hosts: Iterable[str], robots_ok: Callable[[str], bool],
                             env=None, echo=print) -> Dict[str, str]:
    """Acknowledge ``hosts`` in the scratch registry. ``{host: outcome}``.

    No-op (empty result) without the flag. With the flag, refuses outside a
    scratch registry by raising, and skips (never acknowledges) a host whose
    robots check fails.
    """
    if not enabled(env):
        return {}
    registry = check_scratch_registry(env)
    from anyuse.execution.api_permission import acknowledge_host, host_entry, load_hosts
    out: Dict[str, str] = {}
    for host in sorted(set(h for h in hosts if h)):
        if not robots_ok(host):
            out[host] = "skipped: robots.txt does not allow both tokens at the root"
            echo(f"bench-ack SKIP {host}: robots")
            continue
        # two arm processes share the scratch registry; the
        # read-modify-write is locked and the write is atomic (a torn write
        # made every later site of both arms crash after its robots.txt)
        import fcntl
        with open(registry + ".lock", "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                acknowledge_host(host, note=REASON, by=BY, path=registry)
                data = load_hosts(registry)
                # the entry acknowledge_host wrote, under an existing key in another case if there was one
                entry = host_entry(data, host)
                entry.update(acknowledged_by=BY, reason=REASON)
                assert "robots_override" not in entry, "the bench never overrides robots"
                tmp = registry + f".tmp{os.getpid()}"
                with open(tmp, "w") as fh:
                    json.dump(data, fh, indent=2, sort_keys=True)
                os.replace(tmp, registry)
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)
        out[host] = "acknowledged"
        echo(f"bench-ack {host} (acknowledged_by=bench) -> {registry}")
    return out
