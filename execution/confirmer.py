"""Confirmer: one typed question after a deterministic verdict, veto-only. The deterministic proof and block rules decide
first and alone. The confirmer may only make the outcome MORE careful:

* ``proved`` -> ``checked (flagged by confirmer: <reason>)`` on a confident
  "bad" answer; anything else (yes, abstain, low confidence, runner error)
  leaves ``proved``; a status that is not ``proved`` is never touched, so it
  can never promote;
* rules say OK, confirmer confidently says challenge / login wall / error ->
  stop (tightening);
* rules say BLOCKED on weak evidence, confirmer confidently says "not a
  challenge" -> a human-check card, never an automatic continue;
* rules say BLOCKED on strong evidence -> the confirmer is not asked.

Modes (``ANYUSE_CONFIRMER``): ``off`` (default), ``shadow`` (ask at every
FINAL and HUMAN-NEEDED step, log the answer next to the deterministic decision,
never change behaviour) and ``veto`` (act only for kinds in :data:`VETO_KINDS`,
which passed the enable rule; every other kind stays shadow). Backends
(``ANYUSE_CONFIRMER_BACKEND``): ``laya`` (default) runs inside the Apple
container on an internal network, kept warm for a session
(:class:`WarmSession`, JSON lines over stdin/stdout, idle timeout); ``openjev``
asks an OpenJev server (``execution/openjev.py``) and never
sends it a challenge page. This module never imports ``laya`` or ``torch``.

Page text is evidence, never instruction: it travels only in the model's state
field and cannot select a code path here.
"""
from __future__ import annotations

import html.parser
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

MODE_ENV = "ANYUSE_CONFIRMER"
LAYA_DIR_ENV = "ANYUSE_CONFIRMER_LAYA_DIR"
CHECKPOINT_ENV = "ANYUSE_CONFIRMER_CHECKPOINT"
BACKEND_ENV = "ANYUSE_CONFIRMER_BACKEND"
BACKENDS = ("laya", "openjev")
OPENJEV = "openjev"            # the VETO_KINDS / log key of the openjev backend
MODES = ("off", "shadow", "veto")
CHECKPOINTS = ("en", "typed")

#: No kind has passed the enable rule yet (results); this
#: threshold is only what a caller gets when it asks without choosing one.
DEFAULT_THRESHOLD = 0.9
MAX_STATE_CHARS = 900

PROVED = "proved"
FLAGGED_PREFIX = "checked (flagged by confirmer: "

# block-decision actions
CONTINUE = "continue"          # rules said OK and the confirmer did not object
STOP = "stop"                  # a confident challenge/login/error answer on an OK page
BLOCKED = "blocked"            # the rules' hard stop stands
HUMAN_CHECK_CARD = "human_check_card"   # weak BLOCKED + confident "not a challenge"


@dataclass(frozen=True)
class Kind:
    name: str
    question: str               # may contain {target}
    bad_when: bool              # the noul answer (P(true) side) that makes things careful
    what: str                   # short reason text for a flag


KINDS: Dict[str, Kind] = {k.name: k for k in (
    Kind("result_page", "Is this page a list of results for the query '{target}'?",
         False, "not a result list for the query"),
    Kind("list_page", "Is this page a list of several results (a listing, or one page "
         "of it), rather than a single item's or article's page?",
         False, "not a listing"),
    Kind("item_page", "Is this page about the single item '{target}' (not a list, "
         "category or site section)?", False, "not the item's page"),
    Kind("block_page", "Is this page a CAPTCHA, bot check or browser challenge instead "
         "of the site's content?", True, "looks like a challenge page"),
    Kind("login_wall", "Does this page require signing in before it shows its content?",
         True, "looks like a login wall"),
    Kind("error_page", "Is this an error, not-found or empty page?", True,
         "looks like an error page"),
    Kind("wrong_page", "Is this page {target}?", False, "not the page kind the task asked for"),
    Kind("consent_banner", "Is the main content hidden behind a cookie or consent wall?",
         True, "content hidden behind a consent wall"),
    Kind("any_page", "Is this a real content page rather than a bot check, error or "
         "login page?", False, "not a real content page"),
)}

#: capability of a proved tool -> the kind asked about its page
CAPABILITY_KIND = {"search": "result_page", "detail": "item_page",
                   "next": "list_page", "filter": "list_page", "sort": "list_page"}


def kind_for_capability(cap: str) -> str:
    return CAPABILITY_KIND.get(str(cap or "").lower(), "any_page")


def question(kind: str, target: str = "") -> Dict[str, Any]:
    k = KINDS[kind]
    t = re.sub(r"\s+", " ", str(target or "")).strip()[:80].replace("'", "’")
    return {"type": "noul", "instructions": k.question.format(target=t)}


# ---------------------------------------------------------------------------
# the compact page text
# ---------------------------------------------------------------------------

class _Reader(html.parser.HTMLParser):
    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.headings: List[str] = []
        self.text: List[str] = []
        self._stack: List[str] = []
        self._in_title = False
        self._h: Optional[List[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "img", "meta", "link", "input", "hr"):
            return
        self._stack.append(tag)
        if tag == "title":
            self._in_title = True
        if tag in ("h1", "h2", "h3") and self._h is None:
            self._h = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in ("h1", "h2", "h3") and self._h is not None:
            h = " ".join(" ".join(self._h).split())
            if h and h not in self.headings:
                self.headings.append(h[:120])
            self._h = None
        while self._stack:
            if self._stack.pop() == tag:
                break

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if any(t in self._SKIP for t in self._stack):
            return
        if self._h is not None:
            self._h.append(data)
        d = data.strip()
        if d:
            self.text.append(d)


def read_html(body: str, limit: int = 600_000) -> Dict[str, Any]:
    """Title, headings and visible text of an HTML document (stdlib only)."""
    r = _Reader()
    try:
        r.feed((body or "")[:limit])
        r.close()
    except Exception:
        pass
    return {"title": " ".join(r.title.split())[:200], "headings": r.headings[:8],
            "text": " ".join(" ".join(r.text).split())}


def url_path(url: str) -> str:
    """Path and query only: no scheme, no host credentials, no fragment."""
    try:
        p = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return ""
    host = (p.hostname or "")
    out = host + (p.path or "/")
    if p.query:
        out += "?" + p.query
    return out[:200]


def compact_state(url: str = "", title: str = "", headings: Sequence[str] = (),
                  results: Sequence[str] = (), text: str = "") -> str:
    lines = []
    if url:
        lines.append(f"URL: {url_path(url)}")
    if title:
        lines.append(f"Title: {' '.join(str(title).split())[:160]}")
    hs = [" ".join(str(h).split())[:100] for h in headings if str(h).strip()][:5]
    if hs:
        lines.append("Headings: " + " | ".join(hs))
    rs = [" ".join(str(x).split())[:80] for x in results if str(x).strip()][:5]
    if rs:
        lines.append("First results: " + " | ".join(rs))
    t = " ".join(str(text or "").split())
    if t:
        lines.append("Text: " + t[:400])
    return "\n".join(lines)[:MAX_STATE_CHARS]


# ---------------------------------------------------------------------------
# verdicts and the veto-only policy
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    """One answer. ``p_bad`` is the probability of the careful answer."""
    kind: str
    p_bad: Optional[float] = None       # None = no opinion (abstain / error)
    noul: Optional[float] = None
    ms: float = 0.0
    error: str = ""

    def confident_bad(self, threshold: float) -> bool:
        return self.p_bad is not None and not self.error and self.p_bad >= threshold

    def confident_good(self, threshold: float) -> bool:
        return self.p_bad is not None and not self.error and (1.0 - self.p_bad) >= threshold


def verdict_from_answer(kind: str, answer: Optional[Mapping[str, Any]], ms: float = 0.0,
                        error: str = "") -> Verdict:
    if error or not isinstance(answer, Mapping) or answer.get("noul") is None:
        return Verdict(kind, None, None, ms, error or "no answer")
    try:
        p_true = float(answer["noul"])
    except (TypeError, ValueError):
        return Verdict(kind, None, None, ms, "unreadable answer")
    if not 0.0 <= p_true <= 1.0:
        return Verdict(kind, None, None, ms, "answer out of range")
    p_bad = p_true if KINDS[kind].bad_when else 1.0 - p_true
    return Verdict(kind, round(p_bad, 4), p_true, ms)


def apply_to_proved(status: str, verdict: Optional[Verdict],
                    threshold: float = DEFAULT_THRESHOLD) -> str:
    """The status after the confirmer: only ``proved`` can move, and only down."""
    if status != PROVED or verdict is None:
        return status
    if verdict.confident_bad(threshold):
        return f"{FLAGGED_PREFIX}{KINDS[verdict.kind].what}, p={verdict.p_bad:.2f})"
    return status


#: kinds that may stop a page the rules let through, and the block reason each
#: becomes (tightening only)
TIGHTEN_KINDS = ("block_page", "login_wall", "error_page", "consent_banner")


def apply_to_block(rules_blocked: bool, evidence: str, verdict: Optional[Verdict],
                   threshold: float = DEFAULT_THRESHOLD) -> str:
    """The asymmetric block policy.

    ``evidence`` is ``"strong"`` or ``"weak"`` for a BLOCKED decision (ignored
    when the rules said OK). The confirmer can stop an OK page, and can turn a
    weak hard stop into a human-check card; it can never continue on its own.
    """
    if not rules_blocked:
        if verdict is not None and verdict.kind in TIGHTEN_KINDS \
                and verdict.confident_bad(threshold):
            return STOP
        return CONTINUE
    if evidence != "weak" or verdict is None or verdict.kind != "block_page":
        return BLOCKED
    if verdict.confident_good(threshold):
        return HUMAN_CHECK_CARD
    return BLOCKED


def block_evidence_strength(final_url: str = "", request_urls: Sequence[str] = (),
                            items: Sequence[str] = (), page_text: str = "") -> str:
    """``strong`` = the rule is not worth a second look; ``weak`` = it is."""
    from anyuse.execution import blocked as _blocked
    if _blocked.challenge_url_marker(final_url or ""):
        return "strong"
    if not items and len(page_text or "") <= _blocked.CHALLENGE_MAX_VISIBLE_TEXT and any(
            _blocked.challenge_url_marker(u or "") for u in request_urls):
        return "strong"
    return "weak"


# ---------------------------------------------------------------------------
# settings and the container runner
# ---------------------------------------------------------------------------

def mode(explicit: Optional[str] = None) -> str:
    try:
        from anyuse.settings import get as get_setting
        raw = str(get_setting(MODE_ENV, explicit) or "")
    except Exception:
        raw = str(explicit or "")
    raw = raw.strip().lower()
    return raw if raw in MODES else "off"


def backend(explicit: Optional[str] = None) -> str:
    raw = str(explicit or _setting(BACKEND_ENV, "laya")).strip().lower()
    return raw if raw in BACKENDS else "laya"


def active_checkpoint() -> str:
    """What answers are keyed by: the Laya checkpoint, or ``openjev``."""
    if backend() == OPENJEV:
        return OPENJEV
    return _setting(CHECKPOINT_ENV, "en")


def _setting(name: str, default: str) -> str:
    try:
        from anyuse.settings import get as get_setting
        return str(get_setting(name) or default)
    except Exception:
        return default


Runner = Callable[[List[Dict[str, Any]], str], List[Dict[str, Any]]]
"""(requests, checkpoint) -> answers. A request is ``{"id", "state",
"question"}``; an answer is ``{"id", "noul", "confidence", "ms"}`` or
``{"id", "error"}``."""

WORKER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "tools", "confirmer", "laya_worker.py")
IMAGE = "python:3.12-slim"
NETWORK = "laya-offline"


def container_command(laya_dir: str, data_dir: str, name: str,
                      checkpoints: Sequence[str]) -> List[str]:
    """The only way Laya runs: internal network, no DNS, offline HF flags."""
    return ["container", "run", "--rm", "--name", name,
            "--network", NETWORK, "--no-dns", "-c", "8", "-m", "8G",
            "-v", f"{laya_dir}:/work", "-v", f"{data_dir}:/data",
            "-e", "HF_HUB_OFFLINE=1", "-e", "TRANSFORMERS_OFFLINE=1",
            "-e", "HF_HUB_DISABLE_TELEMETRY=1",
            IMAGE, "sh", "/data/run.sh", *checkpoints]


RUN_SH = """set -e
cd /work
python -m venv /tmp/v >/dev/null
/tmp/v/bin/pip install -q --no-index --find-links /work/wheels laya >/dev/null
exec /tmp/v/bin/python /data/laya_worker.py "$@"
"""


class ContainerRunner:
    """Runs a batch through ``tools/confirmer/laya_worker.py`` in the container.

    ``spawn`` is injectable (tests pass a fake; nothing here is imported from
    laya). The container is started with ``--rm`` and stopped in ``finally``.
    """

    def __init__(self, laya_dir: Optional[str] = None, timeout_s: float = 900.0,
                 spawn: Optional[Callable[..., Any]] = None, keep_dir: Optional[str] = None):
        self.laya_dir = laya_dir or _setting(LAYA_DIR_ENV, "")
        self.timeout_s = timeout_s
        self.spawn = spawn or subprocess.run
        self.keep_dir = keep_dir
        self.last_log = ""
        self.last_wall_s = 0.0

    def __call__(self, requests: List[Dict[str, Any]], checkpoint: str = "en"
                 ) -> List[Dict[str, Any]]:
        return self.run_many(requests, [checkpoint])[checkpoint]

    def run_many(self, requests: List[Dict[str, Any]], checkpoints: Sequence[str]
                 ) -> Dict[str, List[Dict[str, Any]]]:
        for c in checkpoints:
            if c not in CHECKPOINTS:
                raise ValueError(f"unknown checkpoint {c!r}")
        if not self.laya_dir or not os.path.isdir(self.laya_dir):
            err = f"{LAYA_DIR_ENV} is not a directory"
            return {c: [{"id": r["id"], "error": err} for r in requests] for c in checkpoints}
        data = self.keep_dir or tempfile.mkdtemp(prefix="anyuse-confirmer-")
        os.makedirs(data, exist_ok=True)
        name = "anyuse-confirmer-" + uuid.uuid4().hex[:10]
        with open(os.path.join(data, "in.jsonl"), "w", encoding="utf-8") as fh:
            for r in requests:
                fh.write(json.dumps({"id": r["id"], "state": r["state"],
                                     "question": r["question"]}) + "\n")
        with open(os.path.join(data, "run.sh"), "w", encoding="utf-8") as fh:
            fh.write(RUN_SH)
        shutil.copyfile(WORKER, os.path.join(data, "laya_worker.py"))
        t0 = time.monotonic()
        err = ""
        try:
            proc = self.spawn(container_command(self.laya_dir, data, name, checkpoints),
                              capture_output=True, text=True, timeout=self.timeout_s)
            self.last_log = (getattr(proc, "stdout", "") or "")[-4000:] + \
                (getattr(proc, "stderr", "") or "")[-4000:]
            if getattr(proc, "returncode", 1) != 0:
                err = f"container exited {getattr(proc, 'returncode', '?')}"
        except Exception as exc:          # timeout, missing CLI: no opinion
            err = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            self.last_wall_s = time.monotonic() - t0
            try:
                self.spawn(["container", "stop", name], capture_output=True, text=True,
                           timeout=60)
            except Exception:
                pass
        out: Dict[str, List[Dict[str, Any]]] = {}
        for c in checkpoints:
            got: Dict[str, Dict[str, Any]] = {}
            path = os.path.join(data, f"out_{c}.jsonl")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            a = json.loads(line)
                        except ValueError:
                            continue
                        got[str(a.get("id"))] = a
            out[c] = [got.get(str(r["id"])) or {"id": r["id"], "error": err or "no answer"}
                      for r in requests]
        if not self.keep_dir:
            shutil.rmtree(data, ignore_errors=True)
        return out


# ---------------------------------------------------------------------------
# the warm session: one long-lived container per process, JSON lines on a pipe
# ---------------------------------------------------------------------------

IDLE_S = 600.0                  # the container exits after 10 idle minutes
READY_TIMEOUT_S = 240.0         # cold start: container + offline pip install + load
ANSWER_TIMEOUT_S = 30.0


def serve_command(laya_dir: str, name: str, checkpoint: str, idle_s: float = IDLE_S
                  ) -> List[str]:
    """The warm container: internal network, no DNS, the laya directory mounted
    READ-ONLY and nothing else; the worker's source travels as an argument and
    requests/answers over stdin/stdout (no port is published)."""
    with open(WORKER, encoding="utf-8") as fh:
        src = fh.read()
    script = ("set -e; python -m venv /tmp/v >/dev/null; "
              "/tmp/v/bin/pip install -q --no-index --find-links /work/wheels laya "
              ">/dev/null 2>&1; exec /tmp/v/bin/python -u -c \"$1\" serve \"$2\" \"$3\"")
    return ["container", "run", "--rm", "-i", "--name", name,
            "--network", NETWORK, "--no-dns", "-c", "8", "-m", "8G",
            "--mount", f"type=bind,source={laya_dir},target=/work,readonly",
            "-e", "HF_HUB_OFFLINE=1", "-e", "TRANSFORMERS_OFFLINE=1",
            "-e", "HF_HUB_DISABLE_TELEMETRY=1",
            IMAGE, "sh", "-c", script, "sh", src, checkpoint, str(int(idle_s))]


class WarmSession:
    """A running worker; ``ask`` costs one forward pass (~200 ms), not a cold start.

    Thread-safe, lazily started, stopped after ``idle_s`` without a request, at
    process exit, or on any protocol error (the next ask starts a fresh one).
    ``popen``/``spawn`` are injectable for tests.
    """

    def __init__(self, laya_dir: Optional[str] = None, checkpoint: str = "en",
                 idle_s: float = IDLE_S, popen: Optional[Callable[..., Any]] = None,
                 spawn: Optional[Callable[..., Any]] = None,
                 ready_timeout_s: float = READY_TIMEOUT_S):
        import threading
        self.laya_dir = laya_dir or _setting(LAYA_DIR_ENV, "")
        self.checkpoint = checkpoint
        self.idle_s = idle_s
        self.popen = popen or subprocess.Popen
        self.spawn = spawn or subprocess.run
        self.ready_timeout_s = ready_timeout_s
        self.proc = None
        self.name = ""
        self.ready: Dict[str, Any] = {}
        self.cold_start_s = 0.0
        self.last_used = 0.0
        self._lock = threading.Lock()
        self._timer = None

    # -- lifecycle ------------------------------------------------------------

    def _readline(self, timeout: float) -> str:
        import select
        out = self.proc.stdout
        if hasattr(out, "fileno"):
            try:
                r, _, _ = select.select([out], [], [], timeout)
                if not r:
                    raise TimeoutError("no answer from the confirmer container")
            except (ValueError, OSError, TypeError):
                pass                              # a fake stream in tests
        line = out.readline()
        if not line:
            raise EOFError("the confirmer container closed its output")
        return line

    def start(self) -> None:
        if self.checkpoint not in CHECKPOINTS:
            raise ValueError(f"unknown checkpoint {self.checkpoint!r}")
        if not self.laya_dir or not os.path.isdir(self.laya_dir):
            raise FileNotFoundError(f"{LAYA_DIR_ENV} is not a directory")
        self.name = "anyuse-confirmer-" + uuid.uuid4().hex[:10]
        t0 = time.monotonic()
        self.proc = self.popen(serve_command(self.laya_dir, self.name, self.checkpoint,
                                             self.idle_s),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, bufsize=1)
        deadline = t0 + self.ready_timeout_s
        while True:
            line = self._readline(max(0.1, deadline - time.monotonic()))
            try:
                msg = json.loads(line)
            except ValueError:
                continue                          # library chatter, not protocol
            if isinstance(msg, dict) and msg.get("ready"):
                self.ready = msg
                break
        self.cold_start_s = time.monotonic() - t0
        import atexit
        atexit.register(self.close)

    def alive(self) -> bool:
        return self.proc is not None and getattr(self.proc, "poll", lambda: None)() is None

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            self.spawn(["container", "stop", self.name], capture_output=True, text=True,
                       timeout=60)
        except Exception:
            pass

    def _arm_idle_timer(self) -> None:
        import threading
        if self._timer is not None:
            self._timer.cancel()

        def idle():
            with self._lock:
                if time.monotonic() - self.last_used >= self.idle_s - 0.01:
                    self.close()
        self._timer = threading.Timer(self.idle_s, idle)
        self._timer.daemon = True
        self._timer.start()

    # -- requests -------------------------------------------------------------

    def ask_many(self, requests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        with self._lock:
            try:
                if not self.alive():
                    self.start()
                out = []
                for r in requests:
                    self.proc.stdin.write(json.dumps({"id": r["id"], "state": r["state"],
                                                      "question": r["question"]}) + "\n")
                    self.proc.stdin.flush()
                    while True:
                        line = self._readline(ANSWER_TIMEOUT_S)
                        try:
                            a = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(a, dict) and str(a.get("id")) == str(r["id"]):
                            out.append(a)
                            break
                self.last_used = time.monotonic()
                self._arm_idle_timer()
                return out
            except Exception as exc:              # no opinion, and a fresh start next time
                self.close()
                err = f"{type(exc).__name__}: {exc}"[:200]
                return [{"id": r["id"], "error": err} for r in requests]

    def __call__(self, requests: List[Dict[str, Any]], checkpoint: str = "") -> List[Dict[str, Any]]:
        return self.ask_many(requests)

    def stats(self) -> Dict[str, Any]:
        """Memory/CPU of the running container (``container stats``), if any."""
        if not self.alive():
            return {}
        try:
            p = self.spawn(["container", "stats", "--no-stream", "--format", "json", self.name],
                           capture_output=True, text=True, timeout=30)
            return {"raw": (getattr(p, "stdout", "") or "")[:2000]}
        except Exception as exc:
            return {"error": type(exc).__name__}


_SESSIONS: Dict[str, Any] = {}


def session(checkpoint: Optional[str] = None) -> Runner:
    ck = checkpoint or active_checkpoint()
    if ck == OPENJEV:
        if OPENJEV not in _SESSIONS:
            from anyuse.execution import openjev as _oj
            _SESSIONS[OPENJEV] = _oj.ConfirmerRunner()      # type: ignore[assignment]
        return _SESSIONS[OPENJEV]
    if ck not in _SESSIONS:
        _SESSIONS[ck] = WarmSession(checkpoint=ck)
    return _SESSIONS[ck]


# ---------------------------------------------------------------------------
# modes, the per-kind veto list, and the shadow log
# ---------------------------------------------------------------------------

#: Kinds that passed the enable rule of the design notes, per checkpoint, with
#: the most conservative threshold that passed it. Only these may act in
#: ``veto`` mode; every other kind is asked and logged (shadow) only.
#: wrong_page passed on ``en`` but is deliberately not listed: it is wired to
#: the learn loop's reference-aware landing check in SHADOW mode only.  More
#: diverse logged evidence is required before it may veto.
VETO_KINDS: Dict[str, Dict[str, float]] = {
    "en": {"error_page": 0.8},
    "typed": {"error_page": 0.5},
    # openjev: a kind is listed only after passing the rule on HELD-OUT sites
    #; until then every kind is shadow.
    OPENJEV: {},
}


def veto_threshold(kind: str, checkpoint: Optional[str] = None) -> Optional[float]:
    ck = checkpoint or active_checkpoint()
    return VETO_KINDS.get(ck, {}).get(kind)


def log_path() -> str:
    from anyuse.paths import anyuse_data_path
    return str(anyuse_data_path("confirmer", "log.jsonl"))


def log_decision(kind: str, url: str, state: str, deterministic: str, verdict: Optional[Verdict],
                 acted: str, checkpoint: str, where: str) -> None:
    """One line per consult, next to the deterministic decision. No page text,
    no query values: the host, a digest of the state, the kind and numbers."""
    import hashlib
    try:
        host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    except ValueError:
        host = ""
    rec = {"ts": round(time.time(), 3), "mode": mode(), "where": where, "kind": kind,
           "checkpoint": checkpoint, "host": host,
           "state_sha256": hashlib.sha256(state.encode()).hexdigest()[:16],
           "deterministic": deterministic[:80],
           "p_bad": None if verdict is None else verdict.p_bad,
           "error": "" if verdict is None else verdict.error[:80],
           "ms": None if verdict is None else verdict.ms,
           "veto_threshold": veto_threshold(kind, checkpoint), "acted": acted}
    try:
        path = log_path()
    except Exception:
        return
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def ask(kind: str, state: str, target: str = "", runner: Optional[Runner] = None,
        checkpoint: Optional[str] = None, obs: Optional[Mapping[str, Any]] = None,
        images: Sequence[Any] = ()) -> Optional[Verdict]:
    """One question; ``None`` when the confirmer is off and no runner is given.

    ``obs`` (url / text / markup / requests / items) feeds the openjev
    backend's challenge guard; ``images`` go only to a backend that reads them
    (openjev, with ``ANYUSE_OPENJEV_IMAGES`` on). The Laya runners ignore both."""
    if runner is None:
        if mode() == "off":
            return None
        runner = session(checkpoint)
    ck = checkpoint or active_checkpoint()
    rid = uuid.uuid4().hex[:12]
    req: Dict[str, Any] = {"id": rid, "state": state, "question": question(kind, target),
                           "kind": kind}
    if obs is not None:
        req["obs"] = dict(obs)
    if images:
        req["images"] = list(images)
    try:
        ans = runner([req], ck)
        a = ans[0] if ans else None
    except Exception as exc:
        return Verdict(kind, None, None, 0.0, f"{type(exc).__name__}")
    return verdict_from_answer(kind, a, float((a or {}).get("ms") or 0.0),
                               str((a or {}).get("error") or ""))


def consult(kind: str, state: str, target: str = "", *, url: str = "",
            deterministic: str = "", where: str = "",
            runner: Optional[Runner] = None, obs: Optional[Mapping[str, Any]] = None,
            images: Sequence[Any] = ()) -> Tuple[Optional[Verdict], Optional[float]]:
    """Ask and log. Returns the verdict and the threshold it may ACT on (None =
    shadow only: the mode is shadow, or this kind has not passed the rule)."""
    m = mode()
    if m == "off" and runner is None:
        return None, None
    ck = active_checkpoint()
    v = ask(kind, state, target, runner=runner, checkpoint=ck, obs=obs, images=images)
    t = veto_threshold(kind, ck) if m == "veto" else None
    acted = "no"
    if t is not None and v is not None and v.confident_bad(t):
        acted = "yes"
    log_decision(kind, url, state, deterministic, v, acted, ck, where)
    return v, t


def confirm_capability(cap: Any, runner: Optional[Runner] = None,
                       threshold: Optional[float] = None) -> Optional[Verdict]:
    """After ``discover.prove`` (a FINAL step): one question about a proved
    tool's page.

    ``shadow``: asked and logged, nothing changes. ``veto``: the status moves
    down only for a kind in :data:`VETO_KINDS`. A caller that passes its own
    ``runner`` and ``threshold`` (the eval, tests) gets that threshold applied.
    """
    if getattr(cap, "status", "") != PROVED:
        return None
    if runner is None and mode() == "off":
        return None
    p = dict(getattr(cap, "proof", None) or {})
    vals = list(getattr(cap, "values", []) or [])
    target = str(vals[0]) if vals else ""        # first_url is the first value's page
    url = p.get("first_url") or (cap.render(target) if target and hasattr(cap, "render") else "")
    state = compact_state(url=url, title=str(p.get("title") or ""),
                          results=list(p.get("ground_truth_sample") or p.get("preview") or []))
    kind = kind_for_capability(getattr(cap, "cap", ""))
    obs = {"url": url, "text": state,
           "items": list(p.get("ground_truth_sample") or p.get("preview") or [])}
    v, t = consult(kind, state, target, url=url, deterministic=PROVED,
                   where="discover.prove", runner=runner, obs=obs)
    if runner is not None and threshold is not None:
        t = threshold
    new = apply_to_proved(cap.status, v, t) if t is not None else cap.status
    if new != cap.status:
        cap.status = new
        cap.reason = (str(getattr(cap, "reason", "")) + f" | {new}")[:300]
    if isinstance(getattr(cap, "proof", None), dict) and v is not None:
        cap.proof["confirmer"] = {"kind": kind, "p_bad": v.p_bad, "error": v.error,
                                  "acting_threshold": t}
    return v


def observe_landing(candidate: Any, run: Any, rule_passed: bool,
                    runner: Optional[Runner] = None) -> Optional[Verdict]:
    """Shadow ``wrong_page`` beside the reference-aware landing rule.

    This hook is intentionally narrower than :func:`consult`: it asks only in
    ``shadow`` mode, even though ``wrong_page`` has evaluation numbers.  The
    deterministic landing verdict is returned by the caller unchanged.  The
    log stores only ``landing:pass`` / ``landing:fail`` beside the answer; page
    text and the query value go to the offline runner but never into the log.
    """
    if mode() != "shadow":
        return None
    dom = str(getattr(run, "dom", "") or "")
    read = read_html(dom)
    url = str(getattr(run, "final_url", "") or "")
    state = compact_state(url=url, title=read["title"], headings=read["headings"],
                          text=str(getattr(run, "text", "") or read["text"]))
    value = str(getattr(run, "value", "") or "")
    cls = str(getattr(candidate, "cls", "") or "page")
    target = (f"a list of search results for '{value}'" if cls == "list"
              else f"the page about the item '{value}'" if cls == "detail"
              else f"the {cls} page for '{value}'")
    shot = getattr(run, "screenshot_png", None)
    verdict, _threshold = consult(
        "wrong_page", state, target, url=url,
        deterministic=f"landing:{'pass' if rule_passed else 'fail'}",
        where="learn_loop.landing_check", runner=runner,
        obs={"url": url, "text": str(getattr(run, "text", "") or read["text"]),
             "markup": dom},
        images=[shot] if isinstance(shot, (bytes, bytearray)) and shot else ())
    return verdict


def observe_block(state: Any, page_text: str, url: str, requests: Sequence[Mapping[str, Any]]
                  = (), items: Sequence[str] = (), runner: Optional[Runner] = None) -> Any:
    """After ``blocked.detect_blocked_ui`` (a FINAL or HUMAN-NEEDED step).

    * rules BLOCKED on strong evidence: not consulted;
    * rules BLOCKED on weak evidence: ``block_page`` asked and logged; a
      confident "not a challenge" is recorded on the state as a human-check
      suggestion (``state.confirmer``) — the block itself always stands;
    * rules OK: ``error_page`` asked and logged; in ``veto`` mode, a confident
      "error page" (a kind that passed the rule) turns it into an
      ``error_page`` block — tightening only.
    """
    if runner is None and mode() == "off":
        return state
    from anyuse.execution import blocked as _blocked
    texts = [str(r.get("url") or "") for r in requests or []]
    snippet = compact_state(url=url, results=list(items or [])[:5], text=page_text or "")
    obs = {"url": url, "text": page_text or "", "items": list(items or []),
           "requests": [dict(r) for r in requests or [] if isinstance(r, Mapping)],
           "blocked_reason": str(getattr(state, "reason", "") or "")
           if getattr(state, "blocked", False) else ""}
    if getattr(state, "blocked", False):
        strength = block_evidence_strength(url, texts, list(items or []), page_text or "")
        if strength == "strong":
            return state
        v, _ = consult("block_page", snippet, url=url, deterministic=f"blocked:{state.reason}",
                       where="detect_blocked_ui", runner=runner, obs=obs)
        t = veto_threshold("block_page") if mode() == "veto" else None
        if t is not None and apply_to_block(True, "weak", v, t) == HUMAN_CHECK_CARD:
            try:
                state.evidence = list(state.evidence) + [
                    "confirmer: looks like a normal page; a person should check (card)"]
            except Exception:
                pass
        return state
    # Laya keeps its measured single question; openjev also asks the other
    # tightening kinds (each shadow until it passes the rule on held-out sites).
    kinds = ("error_page",) if active_checkpoint() != OPENJEV else \
        ("error_page", "login_wall", "consent_banner", "block_page")
    for kind in kinds:
        v, t = consult(kind, snippet, url=url, deterministic="ok",
                       where="detect_blocked_ui", runner=runner, obs=obs)
        if t is not None and apply_to_block(False, "", v, t) == STOP:
            host = urllib.parse.urlsplit(url or "").netloc
            return _blocked.BlockedState(
                blocked=True, reason=_TIGHTEN_REASON[kind], host=host,
                evidence=[f"confirmer: {KINDS[kind].what} (p={v.p_bad:.2f})"])
    return state


_TIGHTEN_REASON = {"error_page": "error_page", "login_wall": "login_wall",
                   "consent_banner": "cookie_wall", "block_page": "unknown_block"}
