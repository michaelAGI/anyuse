"""One fail-closed destination policy for HTTP and browser navigations."""
from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse
from typing import Callable, Iterable, Optional, Sequence, Set

LOCAL_FIXTURE_ENV = "ANYUSE_LOCAL_FIXTURE_ORIGINS"


def origin(url: str) -> str:
    """Canonical exact origin (scheme, host and effective port), or ``""``."""
    parts = urllib.parse.urlsplit(str(url or ""))
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme.lower() not in ("http", "https") or not host:
        return ""
    try:
        port = parts.port
    except ValueError:
        return ""
    default = 443 if parts.scheme.lower() == "https" else 80
    suffix = "" if port in (None, default) else f":{port}"
    return f"{parts.scheme.lower()}://{host}{suffix}"


def _configured_fixture_origins() -> Set[str]:
    from anyuse.settings import get as get_setting
    return {origin(value.strip()) for value in
            str(get_setting(LOCAL_FIXTURE_ENV)).split(",") if origin(value.strip())}


def is_local_fixture_origin(url: str) -> bool:
    """True only for an exact loopback origin named as an offline fixture."""
    target = origin(url)
    if target not in _configured_fixture_origins():
        return False
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _nonpublic_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return not address.is_global


def destination_refusal(url: str, allowed_origins: Iterable[str], *,
                        local_fixture_origins: Optional[Iterable[str]] = None,
                        resolve: bool = False,
                        resolver: Optional[Callable[[str], Sequence[str]]] = None) -> str:
    """Why ``url`` may not be sent, or ``""``.

    Exact origins are the capability boundary. Non-public destinations are
    refused even when their origin is listed, unless that exact origin is also
    named by the explicit local-fixture exception.
    """
    target = origin(url)
    allowed = {origin(item) for item in allowed_origins if origin(item)}
    fixtures = ({origin(item) for item in local_fixture_origins if origin(item)}
                if local_fixture_origins is not None else _configured_fixture_origins())
    if not target:
        return "destination has no valid http(s) origin"
    if target not in allowed:
        return f"origin {target!r} is not in the exact-origin allowlist"

    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    named_private = (host == "localhost" or host.endswith((".local", ".internal")))
    private = named_private or _nonpublic_address(host)
    if resolve and not private:
        try:
            addresses = (list(resolver(host)) if resolver else
                         list({row[4][0] for row in socket.getaddrinfo(host, None)}))
        except Exception as exc:
            return f"destination address resolution failed closed: {type(exc).__name__}: {exc}"
        private = any(_nonpublic_address(address) for address in addresses)
    if private and target not in fixtures:
        return (f"nonpublic destination {host!r} is refused by default; for an offline "
                f"release fixture, set {LOCAL_FIXTURE_ENV} to this exact origin")
    return ""


def exact_origin_allowlist(start_url: str,
                           related_origins: Optional[Iterable[str]] = None) -> Set[str]:
    out = {origin(start_url)} if origin(start_url) else set()
    out.update(origin(item) for item in (related_origins or []) if origin(item))
    return out
