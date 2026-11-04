"""The central list of hosts no bench may contact again.

Every bench that chooses hosts reads it before any request. A missing file is a
refusal, never an empty list.
"""
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "NEVER_CONTACT.txt")


def never_contact() -> set:
    try:
        with open(_PATH, encoding="utf-8") as fh:
            out = {ln.strip().lower() for ln in fh if ln.strip() and not ln.startswith("#")}
    except FileNotFoundError:
        raise SystemExit("NEVER_CONTACT.txt missing: refusing to choose hosts")
    try:
        from anyuse.paths import anyuse_data_path
        user_path = str(anyuse_data_path("never_contact.txt"))
        if os.path.isfile(user_path):
            with open(user_path, encoding="utf-8") as ufh:
                out |= {ln.strip().lower() for ln in ufh if ln.strip() and not ln.startswith("#")}
    except Exception:  # noqa: BLE001
        pass
    return out


#: the status every bench records for a government host it skips
GOV_SKIP = "skipped: government host (owner rule)"


def is_government(host: str) -> str:
    """Why ``host`` is a government host by the declared list
    (``execution/gov_hosts.py``), or ``""``."""
    from anyuse.execution.gov_hosts import government_host
    return government_host(host)


def looks_governmental(host: str) -> str:
    """Fail-closed reporting: a host that looks governmental but is not on the
    list (not contacted; named for review). ``""`` otherwise."""
    from anyuse.execution.gov_hosts import looks_governmental as lg
    return lg(host)


def refusal(host: str, never=None) -> str:
    """'' or why no request may go to ``host``: NEVER_CONTACT, or a government host when
    ``ANYUSE_REFUSE_GOV_HOSTS`` is on (``gov_hosts.government_refusal``). Choosing a bench's sites
    uses :func:`is_government`, the list itself, whatever the setting."""
    from anyuse.execution.gov_hosts import government_refusal
    h = (host or "").strip().lower()
    if h in (never if never is not None else never_contact()):
        return "on the never-contact list"
    if government_refusal(h):
        return GOV_SKIP
    return ""
