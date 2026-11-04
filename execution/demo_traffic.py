"""The per-site traffic plan: a validation traffic share reserved per site. PURE.

The benchmark policy allows <= 300 requests and <= 30 MB per site over the
whole benchmark. One undivided budget lost 20 architecture runs to an exhausted
share. The plan splits the site's 300 into fixed phase shares in their order, and a
phase may never spend what a LATER phase has reserved: the demonstration cannot eat
the immediate wave's share, and the immediate wave cannot eat the delayed wave's
(>= 24 h later). What a phase left unused stays with the site (the margin grows), never with an earlier phase.
"""
from __future__ import annotations

from typing import Dict, Mapping, Tuple

SITE_CAP = 300
SITE_BYTES = 30 * 1024 * 1024
MB = 1024 * 1024
PHASES = ("screen", "demo", "immediate", "delayed")
SHARES = {"screen": 70, "demo": 60, "immediate": 70, "delayed": 90}
BYTES = {"screen": 7 * MB, "demo": 7 * MB, "immediate": 7 * MB, "delayed": 8 * MB}
MARGIN = 10
MARGIN_BYTES = 1 * MB
assert sum(SHARES.values()) + MARGIN == SITE_CAP and sum(BYTES.values()) + MARGIN_BYTES == SITE_BYTES

#: an invocation is not started with less than this left in its phase (requests, bytes)
MIN_LEFT = {"demo_step": (4, 256 * 1024), "tool": (8, 512 * 1024), "oracle_search": (6, 384 * 1024),
            "oracle_item": (3, 256 * 1024), "http": (1, 64 * 1024), "value_render": (8, 512 * 1024)}


def share_for(phase: str, booked: Mapping[str, Mapping[str, int]]) -> Tuple[int, int]:
    """(requests, bytes) the phase may still spend on the site, given what every phase
    has booked so far (``{phase: {"requests", "bytes"}}``, the site ledger): its own
    share minus what it already used (a resumed phase), never beyond what the site's
    300 / 30 MB leave after the margin and the shares of the later phases not yet
    booked."""
    if phase not in SHARES:
        raise ValueError(f"unknown phase {phase!r}")
    used = sum(int((v or {}).get("requests", 0)) for v in booked.values())
    used_b = sum(int((v or {}).get("bytes", 0)) for v in booked.values())
    later = [p for p in PHASES[PHASES.index(phase) + 1:] if p not in booked]
    own = booked.get(phase) or {}
    req = min(SHARES[phase] - int(own.get("requests", 0)),
              SITE_CAP - MARGIN - used - sum(SHARES[p] for p in later))
    nb = min(BYTES[phase] - int(own.get("bytes", 0)),
             SITE_BYTES - MARGIN_BYTES - used_b - sum(BYTES[p] for p in later))
    return max(0, req), max(0, nb)


def can_start(kind: str, left_requests: int, left_bytes: int) -> bool:
    need_r, need_b = MIN_LEFT[kind]
    return left_requests >= need_r and left_bytes >= need_b


def plan() -> Dict[str, Dict[str, int]]:
    return {p: {"requests": SHARES[p], "bytes": BYTES[p]} for p in PHASES}
