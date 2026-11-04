"""Runtime tool writing for the agent harness.

When no default tool fits a sub-goal, the policy may answer ``write_tool`` with a :class:`ToolSpec`:
a small Python tool against a restricted interface (``def run(inputs: dict) -> dict``), with declared
inputs, outputs, an effect class (``transform`` | ``http_read``) and examples. It becomes a saved,
reusable tool only after

1. the STATIC CHECKS (AST only; the code is never imported here): an import allowlist, no
   subprocess / os / sys / socket / shutil / pathlib / importlib / ctypes / pickle / threading, no
   eval / exec / compile / __import__ / open / getattr / globals, no dunder attribute, a size cap;
2. the PROOF in a sandbox on >= ``MIN_FRESH_CASES`` FRESH value sets derived by the harness: the
   examples reproduce, the outputs have the declared types, the declared invariants hold on the
   fresh inputs, two runs agree, nothing left the workspace's ``out/``;
3. the REGISTRY saves it with its hash; ``run`` re-checks the hash and runs it at 0 model calls.

Sandboxes: ``FakeSandbox`` (this phase: in-process, restricted builtins, a wall clock; a stand-in,
not a security boundary) and ``ContainerSandbox`` (wired, NOT started: it builds the exact
``container run`` argv with no host mount except the workspace and ``--network none``, and runs only
through an injected runner). This module never imports ``subprocess``.
"""
from __future__ import annotations

import ast
import copy
import csv
import hashlib
import io
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

MIN_FRESH_CASES = 2
MAX_CODE_CHARS = 20_000
MAX_CODE_LINES = 400
RUN_TIMEOUT_S = 10.0
EFFECTS = ("transform", "http_read")
TYPES = ("str", "int", "float", "bool", "list", "dict", "csv", "json")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
ALLOWED_MODULES = frozenset({
    "json", "csv", "re", "math", "statistics", "datetime", "collections", "itertools", "functools",
    "string", "textwrap", "html", "html.parser", "urllib.parse", "decimal", "fractions", "typing",
    "dataclasses", "io", "operator", "unicodedata", "difflib", "heapq", "bisect", "enum",
})
BANNED_NAMES = frozenset({
    "eval", "exec", "compile", "__import__", "open", "input", "getattr", "setattr", "delattr", "globals",
    "locals", "vars", "breakpoint", "memoryview", "help", "exit", "quit", "__builtins__", "__loader__",
    "__spec__", "__file__", "__build_class__", "super", "type", "object", "classmethod", "staticmethod",
    "property",
})
#: the only names a tool may call that reach outside its arguments: given by the sandbox
INTERFACE = ("read_input", "write_output", "fetch")
SAFE_BUILTINS = {
    k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k)
    for k in ("abs", "all", "any", "bool", "chr", "dict", "divmod", "enumerate", "filter", "float",
              "frozenset", "int", "isinstance", "iter", "len", "list", "map", "max", "min", "next", "ord",
              "pow", "range", "repr", "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple",
              "zip", "format", "bytes", "ValueError", "TypeError", "KeyError", "IndexError", "Exception",
              "ZeroDivisionError", "ArithmeticError", "LookupError", "StopIteration", "AttributeError",
              "RuntimeError")
}


# ---------------------------------------------------------------------------
# the spec
# ---------------------------------------------------------------------------
@dataclass
class ToolSpec:
    name: str
    description: str
    inputs: Dict[str, str]
    outputs: Dict[str, str]
    effect: str
    code: str
    examples: List[Dict[str, Any]] = field(default_factory=list)
    invariants: List[str] = field(default_factory=list)
    host: str = ""

    @classmethod
    def from_dict(cls, d: Any) -> Tuple[Optional["ToolSpec"], List[str]]:
        probs: List[str] = []
        if not isinstance(d, dict):
            return None, ["a tool spec is a JSON object"]
        name = str(d.get("name") or "")
        if not NAME_RE.match(name):
            probs.append("name: lowercase letters, digits and _ (3-41 characters, starting with a letter)")
        for key in ("inputs", "outputs"):
            v = d.get(key)
            if not isinstance(v, dict) or not v:
                probs.append(f"{key}: a non-empty object of name -> type")
            else:
                for n, t in v.items():
                    if not FIELD_RE.match(str(n)):
                        probs.append(f"{key}.{n}: a field name is lowercase letters, digits and _")
                    elif str(t) not in TYPES:
                        probs.append(f"{key}.{n}: type must be one of {', '.join(TYPES)}")
        effect = str(d.get("effect") or "")
        if effect not in EFFECTS:
            probs.append(f"effect: one of {', '.join(EFFECTS)}")
        code = d.get("code")
        if not isinstance(code, str) or not code.strip():
            probs.append("code: the Python source with `def run(inputs: dict) -> dict`")
        ex = d.get("examples")
        if not isinstance(ex, list) or not ex or not all(isinstance(e, dict) and isinstance(e.get("inputs"), dict)
                                                          and isinstance(e.get("outputs"), dict) for e in ex):
            probs.append("examples: >= 1 of {inputs: {...}, outputs: {...}}")
        inv = d.get("invariants") or []
        if not isinstance(inv, list) or not all(isinstance(x, str) for x in inv):
            probs.append("invariants: a list of expressions over inputs/outputs")
        host = str(d.get("host") or "")
        if effect == "http_read" and not host:
            probs.append("host: an http_read tool declares the one host it reads")
        if probs:
            return None, probs
        return cls(name=name, description=str(d.get("description") or "")[:500], inputs=dict(d["inputs"]),
                   outputs=dict(d["outputs"]), effect=effect, code=code, examples=list(ex),
                   invariants=list(inv), host=host), []

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    def sha256(self) -> str:
        return hashlib.sha256(self.code.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 1. static checks (AST only)
# ---------------------------------------------------------------------------
@dataclass
class StaticReport:
    ok: bool
    problems: List[str] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def static_check(spec: ToolSpec) -> StaticReport:
    probs: List[str] = []
    code = spec.code
    if len(code) > MAX_CODE_CHARS or code.count("\n") > MAX_CODE_LINES:
        probs.append(f"the code is larger than {MAX_CODE_LINES} lines / {MAX_CODE_CHARS} characters")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return StaticReport(False, [f"syntax error at line {exc.lineno}: {exc.msg}"])
    imports: List[str] = []
    run_def = None
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.append(a.name)
                if a.name not in ALLOWED_MODULES and a.name.split(".")[0] not in ALLOWED_MODULES:
                    probs.append(f"line {line}: import {a.name} is not on the allowlist")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imports.append(mod)
            if node.level or (mod not in ALLOWED_MODULES and mod.split(".")[0] not in ALLOWED_MODULES):
                probs.append(f"line {line}: from {mod or '.'} import ... is not on the allowlist")
            if any(a.name == "*" for a in node.names):
                probs.append(f"line {line}: `from ... import *` is refused")
        elif isinstance(node, ast.Name):
            if node.id in BANNED_NAMES or (node.id.startswith("__") and node.id != "__name__"):
                probs.append(f"line {line}: the name {node.id} is refused")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr in ("system", "popen", "spawn", "fork"):
                probs.append(f"line {line}: the attribute .{node.attr} is refused")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            probs.append(f"line {line}: global/nonlocal is refused")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            probs.append(f"line {line}: async code is refused")
        elif isinstance(node, ast.FunctionDef) and node.name == "run" and isinstance(node, ast.FunctionDef):
            if node in tree.body:
                run_def = node
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value \
                and re.search(r"__[a-z]+__", node.value):
            probs.append(f"line {line}: a dunder name in a string literal is refused")
    if run_def is None:
        probs.append("no top-level `def run(inputs)`")
    else:
        args = run_def.args
        if len(args.args) != 1 or args.vararg or args.kwarg or args.kwonlyargs or args.posonlyargs:
            probs.append("`run` takes exactly one argument, the inputs dict")
    if spec.effect != "http_read" and re.search(r"\bfetch\s*\(", code):
        probs.append("a transform tool does not fetch (declare effect http_read and a host)")
    for inv in spec.invariants:
        try:
            t = ast.parse(inv, mode="eval")
        except SyntaxError:
            probs.append(f"invariant {inv!r} is not an expression")
            continue
        for node in ast.walk(t):
            if isinstance(node, ast.Name) and (node.id in BANNED_NAMES or node.id.startswith("__")):
                probs.append(f"invariant {inv!r}: the name {node.id} is refused")
            if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                probs.append(f"invariant {inv!r}: a dunder attribute is refused")
    seen = set()
    probs = [p for p in probs if not (p in seen or seen.add(p))]
    return StaticReport(not probs, probs, sorted(set(imports)))


# ---------------------------------------------------------------------------
# 2. the sandboxes
# ---------------------------------------------------------------------------
@dataclass
class SandboxResult:
    ok: bool
    outputs: Dict[str, Any] = field(default_factory=dict)
    why: str = ""
    stdout: str = ""
    seconds: float = 0.0
    fetched: List[str] = field(default_factory=list)
    written: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "outputs": self.outputs, "why": self.why, "seconds": round(self.seconds, 3),
                "fetched": list(self.fetched), "written": list(self.written)}


class _Interface:
    """``read_input`` / ``write_output`` over the workspace, ``fetch`` over the harness's HTTP tool
    (same host as declared, GET only)."""

    def __init__(self, spec: ToolSpec, workspace: Optional[str], fetch: Optional[Callable[[str], str]]):
        self.spec, self._fetch = spec, fetch
        self.ws = os.path.realpath(workspace) if workspace else None
        self.fetched: List[str] = []
        self.written: List[str] = []

    def _path(self, sub: str, name: str) -> str:
        if not self.ws:
            raise ValueError("no workspace in this run")
        n = str(name)
        if not n or os.path.isabs(n) or ".." in n.split("/") or n.startswith("~"):
            raise ValueError(f"not a workspace file name: {n!r}")
        base = os.path.realpath(os.path.join(self.ws, sub))
        p = os.path.realpath(os.path.join(base, n))
        if p != base and not p.startswith(base + os.sep):
            raise ValueError(f"{n!r} leaves the workspace")
        return p

    def read_input(self, name: str) -> str:
        with open(self._path("in", name), "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(2_000_000)

    def write_output(self, name: str, text: str) -> None:
        p = self._path("out", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(str(text))
        self.written.append(os.path.relpath(p, self.ws))

    def fetch(self, url: str) -> str:
        if self.spec.effect != "http_read":
            raise ValueError("this tool declared no http_read effect")
        host = (url.split("//", 1)[-1].split("/", 1)[0].split("@")[-1].split(":")[0] or "").lower()
        if host != self.spec.host.lower():
            raise ValueError(f"fetch refused: {host!r} is not the declared host {self.spec.host!r}")
        if self._fetch is None:
            raise ValueError("no fetch in this run (network off)")
        self.fetched.append(url)
        return str(self._fetch(url))


def _guarded_import(allowed: frozenset):
    real = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def imp(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if level or (name not in allowed and name.split(".")[0] not in allowed):
            raise ImportError(f"import {name!r} is not on the allowlist")
        return real(name, globals, locals, fromlist, level)
    return imp


class FakeSandbox:
    """In-process execution with restricted builtins and a wall clock. A stand-in for the container
    (section 6): it lets the proof and the registry be built and tested offline. NOT a boundary."""

    name = "fake"

    def __init__(self, timeout_s: float = RUN_TIMEOUT_S):
        self.timeout_s = float(timeout_s)

    def run(self, spec: ToolSpec, inputs: Dict[str, Any], workspace: Optional[str] = None,
            fetch: Optional[Callable[[str], str]] = None) -> SandboxResult:
        iface = _Interface(spec, workspace, fetch)
        out = io.StringIO()
        builtins = dict(SAFE_BUILTINS)
        builtins["__import__"] = _guarded_import(ALLOWED_MODULES)
        builtins["print"] = lambda *a, **k: print(*a, file=out, **{kk: v for kk, v in k.items() if kk != "file"})
        ns: Dict[str, Any] = {"__builtins__": builtins, "__name__": "anyuse_runtime_tool",
                              "read_input": iface.read_input, "write_output": iface.write_output}
        if spec.effect == "http_read":
            ns["fetch"] = iface.fetch
        result: Dict[str, Any] = {}
        t0 = time.monotonic()

        def target():
            try:
                exec(compile(spec.code, f"<tool {spec.name}>", "exec"), ns)  # noqa: S102 - the fake sandbox
                fn = ns.get("run")
                if not callable(fn):
                    raise ValueError("no run function")
                result["outputs"] = fn(copy.deepcopy(inputs))
            except BaseException as exc:  # noqa: BLE001
                result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        th = threading.Thread(target=target, daemon=True)
        th.start()
        th.join(self.timeout_s)
        secs = time.monotonic() - t0
        if th.is_alive():
            return SandboxResult(False, why=f"timed out after {self.timeout_s:.0f} s", stdout=out.getvalue()[:2000],
                                 seconds=secs)
        if "error" in result:
            return SandboxResult(False, why=result["error"], stdout=out.getvalue()[:2000], seconds=secs,
                                 fetched=iface.fetched, written=iface.written)
        o = result.get("outputs")
        if not isinstance(o, dict):
            return SandboxResult(False, why=f"run returned {type(o).__name__}, not a dict", seconds=secs)
        return SandboxResult(True, outputs=o, stdout=out.getvalue()[:2000], seconds=secs, fetched=iface.fetched,
                             written=iface.written)


class ContainerSandbox:
    """The Apple container sandbox, WIRED and never started here. ``plan`` is the exact argv; ``run``
    needs an injected ``runner(argv, stdin_text, timeout) -> (rc, stdout, stderr)``; without one it
    refuses. This class never imports subprocess."""

    name = "container"
    IMAGE = "python:3.12-slim"

    #: the tool's process runs as this unprivileged user inside the container (not uid 0, the image's default;
    #: ``nobody``'s uid:gid, accepted by Apple ``container`` 1.1.0's ``-u, --user <name|uid[:gid]>``)
    USER = "65534:65534"

    def __init__(self, runner=None, image: str = "", timeout_s: float = 60.0, memory: str = "256m", cpus: str = "1",
                 user: str = ""):
        self.runner, self.image, self.timeout_s = runner, image or self.IMAGE, float(timeout_s)
        self.memory, self.cpus, self.user = memory, cpus, user or self.USER

    def plan(self, spec: ToolSpec, workspace: str) -> List[str]:
        ws = os.path.realpath(workspace)
        argv = ["container", "run", "--rm", "--network", "none", "--user", self.user, "--memory", self.memory,
                "--cpus", self.cpus,
                "--mount", f"type=bind,source={ws}/in,target=/task/in,readonly",
                "--mount", f"type=bind,source={ws}/out,target=/task/out",
                "--mount", f"type=bind,source={ws}/tool,target=/task/tool,readonly",
                "--workdir", "/task/out", self.image, "python3", "-I", "-S", "/task/tool/main.py"]
        return argv

    def stage(self, spec: ToolSpec, inputs: Dict[str, Any], workspace: str) -> str:
        """Write ``tool/main.py`` (the tool plus a runner that reads inputs.json and prints outputs) and
        ``tool/inputs.json`` into the workspace; returns the tool directory."""
        d = os.path.join(workspace, "tool")
        os.makedirs(d, exist_ok=True)
        # the out directory must be writable by the container's unprivileged user (``USER``), not only by the owner
        out_dir = os.path.join(workspace, "out")
        os.makedirs(out_dir, exist_ok=True)
        try:
            os.chmod(out_dir, 0o1777)
        except OSError:
            pass
        main = (spec.code + "\n\nif __name__ == '__main__':\n    import json as _j\n"
                "    _inp = _j.load(open('/task/tool/inputs.json'))\n"
                "    print(_j.dumps({'outputs': run(_inp)}, ensure_ascii=False, default=str))\n")
        with open(os.path.join(d, "main.py"), "w", encoding="utf-8") as fh:
            fh.write(main)
        with open(os.path.join(d, "inputs.json"), "w", encoding="utf-8") as fh:
            json.dump(inputs, fh, ensure_ascii=False)
        return d

    def run(self, spec: ToolSpec, inputs: Dict[str, Any], workspace: Optional[str] = None,
            fetch: Optional[Callable[[str], str]] = None) -> SandboxResult:
        if self.runner is None:
            return SandboxResult(False, why="the container sandbox is wired but not started in this phase (no runner)")
        if not workspace:
            return SandboxResult(False, why="the container sandbox needs a workspace to mount")
        if spec.effect == "http_read":
            return SandboxResult(False, why="http_read tools run with the network off in the container; the fetch "
                                            "bridge is not wired yet")
        self.stage(spec, inputs, workspace)
        t0 = time.monotonic()
        rc, out, err = self.runner(self.plan(spec, workspace), "", self.timeout_s)
        secs = time.monotonic() - t0
        if rc != 0:
            return SandboxResult(False, why=f"container exit {rc}: {(err or '')[:200]}", stdout=(out or "")[:2000],
                                 seconds=secs)
        try:
            obj = json.loads((out or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return SandboxResult(False, why="the tool printed no outputs object", stdout=(out or "")[:2000], seconds=secs)
        o = obj.get("outputs") if isinstance(obj, dict) else None
        if not isinstance(o, dict):
            return SandboxResult(False, why="run returned no dict", stdout=(out or "")[:2000], seconds=secs)
        return SandboxResult(True, outputs=o, stdout=(out or "")[:2000], seconds=secs)


# ---------------------------------------------------------------------------
# 3. the proof on fresh value sets
# ---------------------------------------------------------------------------
_WORDS = ("Nordwind", "Quarz", "Lindenhof", "Bernstein", "Taube", "Zimt", "Fjord", "Olive")


def _fresh_value(v: Any, salt: int) -> Any:
    """A different value of the same type (never the example's own)."""
    if isinstance(v, bool):
        return not v
    if isinstance(v, int):
        return v + 7 + salt
    if isinstance(v, float):
        return round(v * 1.5 + 0.25 + salt, 4)
    if isinstance(v, str):
        s = re.sub(r"\d", lambda m: str((int(m.group(0)) + 3 + salt) % 10), v)
        word = _WORDS[salt % len(_WORDS)]
        toks = re.findall(r"[^\W\d_]{3,}", s)
        if toks:
            s = re.sub(r"\b" + re.escape(toks[-1]) + r"\b", word, s, count=1)
        elif s == v:
            s = s + word
        return s if s != v else s + " " + word
    if isinstance(v, list):
        items = [_fresh_value(x, salt) for x in v]
        if items:
            items = items[::-1] + [_fresh_value(items[0], salt + 1)]
        else:
            items = [_WORDS[salt % len(_WORDS)]]
        return items
    if isinstance(v, dict):
        return {k: _fresh_value(x, salt) for k, x in v.items()}
    return v


def fresh_inputs(spec: ToolSpec, n: int = MIN_FRESH_CASES) -> List[Dict[str, Any]]:
    """``n`` input sets derived from the examples, none equal to an example."""
    out: List[Dict[str, Any]] = []
    examples = [e["inputs"] for e in spec.examples]
    salt = 0
    while len(out) < n and salt < n * 4:
        base = examples[salt % len(examples)]
        cand = {k: _fresh_value(v, salt) for k, v in base.items()}
        salt += 1
        if cand in examples or cand in out:
            continue
        out.append(cand)
    return out


def _type_ok(value: Any, t: str) -> bool:
    return {"str": lambda v: isinstance(v, str), "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "bool": lambda v: isinstance(v, bool), "list": lambda v: isinstance(v, list),
            "dict": lambda v: isinstance(v, dict), "csv": lambda v: isinstance(v, str),
            "json": lambda v: isinstance(v, str)}.get(t, lambda v: False)(value)


def _schema_problems(spec: ToolSpec, outputs: Dict[str, Any]) -> List[str]:
    probs = []
    for name, t in spec.outputs.items():
        if name not in outputs:
            probs.append(f"output {name!r} missing")
        elif not _type_ok(outputs[name], t):
            probs.append(f"output {name!r} is not a {t}")
    return probs


def _format_problems(spec: ToolSpec, inputs: Dict[str, Any], outputs: Dict[str, Any]) -> List[str]:
    """The harness's own round-trip checks for ``csv`` / ``json`` outputs (independent of the writer)."""
    probs = []
    list_inputs = [v for v in inputs.values() if isinstance(v, list)]
    for name, t in spec.outputs.items():
        v = outputs.get(name)
        if not isinstance(v, str):
            continue
        if t == "csv":
            try:
                rows = [r for r in csv.reader(io.StringIO(v)) if r]
            except csv.Error as exc:
                probs.append(f"output {name!r} is not CSV: {exc}")
                continue
            if not rows:
                probs.append(f"output {name!r} is empty CSV")
            elif len(list_inputs) == 1 and len(rows) not in (len(list_inputs[0]), len(list_inputs[0]) + 1):
                probs.append(f"output {name!r}: {len(rows)} CSV rows for {len(list_inputs[0])} input rows "
                             "(a header row is allowed)")
            elif len({len(r) for r in rows}) > 1:
                probs.append(f"output {name!r}: CSV rows have different widths")
        elif t == "json":
            try:
                json.loads(v)
            except ValueError as exc:
                probs.append(f"output {name!r} is not JSON: {exc}")
    return probs


def _invariant_problems(spec: ToolSpec, inputs: Dict[str, Any], outputs: Dict[str, Any]) -> List[str]:
    probs = []
    env = {"__builtins__": {k: SAFE_BUILTINS[k] for k in ("len", "sum", "min", "max", "all", "any", "sorted",
                                                            "set", "str", "int", "float", "isinstance", "list",
                                                            "dict", "abs", "round", "bool", "tuple", "zip",
                                                            "range", "enumerate")},
           "inputs": inputs, "outputs": outputs}
    for inv in spec.invariants:
        try:
            ok = bool(eval(compile(inv, "<invariant>", "eval"), env))  # noqa: S307 - checked by static_check
        except Exception as exc:  # noqa: BLE001
            probs.append(f"invariant {inv!r} raised {type(exc).__name__}: {exc}"[:200])
            continue
        if not ok:
            probs.append(f"invariant {inv!r} does not hold")
    return probs


@dataclass
class ToolProof:
    passed: bool
    reason: str
    cases: List[Dict[str, Any]] = field(default_factory=list)
    sandbox: str = ""
    min_cases: int = MIN_FRESH_CASES
    model_calls: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def prove_tool(spec: ToolSpec, sandbox, *, workspace: Optional[str] = None, fetch=None,
               fresh: Optional[Sequence[Dict[str, Any]]] = None, n: int = MIN_FRESH_CASES) -> ToolProof:
    """The examples reproduce; on >= ``n`` fresh sets: runs, typed outputs, invariants, determinism,
    the harness's format checks, nothing written outside ``out/`` and no fetch off the host."""
    from anyuse.execution.model_adapter import calls_since, process_call_count
    mark = process_call_count()
    cases: List[Dict[str, Any]] = []
    for ex in spec.examples:
        r = sandbox.run(spec, ex["inputs"], workspace, fetch)
        probs = [] if r.ok else [r.why]
        if r.ok and r.outputs != ex["outputs"]:
            probs.append("the example's outputs do not reproduce: got " + json.dumps(r.outputs, default=str)[:200])
        cases.append({"kind": "example", "inputs": ex["inputs"], "passed": not probs, "problems": probs[:5]})
    sets = list(fresh) if fresh is not None else fresh_inputs(spec, n)
    if len(sets) < n:
        return ToolProof(False, f"only {len(sets)} fresh value set(s) could be derived; {n} needed", cases,
                         sandbox=getattr(sandbox, "name", ""), model_calls=calls_since(mark))
    for inputs in sets:
        if any(inputs == e["inputs"] for e in spec.examples):
            cases.append({"kind": "fresh", "inputs": inputs, "passed": False,
                          "problems": ["a fresh value set equals an example"]})
            continue
        r1 = sandbox.run(spec, inputs, workspace, fetch)
        probs: List[str] = []
        if not r1.ok:
            probs.append(r1.why)
        else:
            probs += _schema_problems(spec, r1.outputs)
            probs += _format_problems(spec, inputs, r1.outputs)
            probs += _invariant_problems(spec, inputs, r1.outputs)
            r2 = sandbox.run(spec, inputs, workspace, fetch)
            if not r2.ok or r2.outputs != r1.outputs:
                probs.append("two runs on the same inputs disagree (not deterministic)")
            if any(not w.startswith("out/") and not w.startswith("out" + os.sep) for w in r1.written):
                probs.append("the tool wrote outside out/")
            if any(spec.host.lower() not in u.lower() for u in r1.fetched):
                probs.append("the tool fetched off its declared host")
        cases.append({"kind": "fresh", "inputs": inputs, "passed": not probs, "problems": probs[:5],
                      "outputs": (r1.outputs if r1.ok else None)})
    passed = all(c["passed"] for c in cases) and sum(1 for c in cases if c["kind"] == "fresh") >= n
    failed = next((c for c in cases if not c["passed"]), None)
    reason = (f"{len(sets)} fresh value sets and {len(spec.examples)} example(s) passed" if passed
              else f"{failed['kind']} case: " + "; ".join(failed["problems"])[:300])
    return ToolProof(passed, reason, cases, sandbox=getattr(sandbox, "name", ""), min_cases=n,
                     model_calls=calls_since(mark))


# ---------------------------------------------------------------------------
# 4. the registry
# ---------------------------------------------------------------------------
class ToolRegistry:
    """``<dir>/<name>.json`` for proven tools, ``<dir>/drafts/<name>.json`` for refused ones."""

    def __init__(self, directory: Optional[str] = None):
        if directory is None:
            from anyuse.paths import anyuse_data_path
            directory = str(anyuse_data_path("agent_tools"))
        self.dir = directory
        os.makedirs(os.path.join(self.dir, "drafts"), exist_ok=True)

    def _path(self, name: str, draft: bool = False) -> str:
        if not NAME_RE.match(str(name or "")):
            raise ValueError(f"not a tool name: {name!r}")
        return os.path.join(self.dir, "drafts" if draft else "", f"{name}.json")

    def names(self) -> List[str]:
        return sorted(f[:-5] for f in os.listdir(self.dir) if f.endswith(".json"))

    def load(self, name: str) -> Optional[Dict[str, Any]]:
        try:
            with open(self._path(name), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def spec_of(self, name: str) -> Optional[ToolSpec]:
        rec = self.load(name)
        if not rec:
            return None
        spec, probs = ToolSpec.from_dict(rec.get("spec"))
        if spec is None or spec.sha256() != rec.get("sha256"):
            return None
        return spec

    def save(self, spec: ToolSpec, static: StaticReport, proof: Optional[ToolProof], *, draft: bool,
             why: str = "") -> str:
        rec = {"spec": spec.to_dict(), "static": static.to_dict(), "proof": proof.to_dict() if proof else None,
               "sha256": spec.sha256(), "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "status": "draft" if draft else "proven", "why": why, "harness_version": 1}
        path = self._path(spec.name, draft=draft)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        if not draft:
            try:
                os.remove(self._path(spec.name, draft=True))
            except OSError:
                pass
        return path

    def run(self, name: str, inputs: Dict[str, Any], sandbox, *, workspace: Optional[str] = None,
            fetch=None) -> Dict[str, Any]:
        """A proven tool at 0 model calls; the code's hash is checked against the saved one."""
        from anyuse.execution.model_adapter import calls_since, process_call_count
        mark = process_call_count()
        spec = self.spec_of(name)
        if spec is None:
            return {"ok": False, "why": f"no proven tool {name!r} (or its code no longer matches its hash)",
                    "model_calls": 0}
        if not isinstance(inputs, dict):
            return {"ok": False, "why": "inputs must be an object", "model_calls": 0}
        missing = [k for k in spec.inputs if k not in inputs]
        if missing:
            return {"ok": False, "why": f"missing input(s) {missing}", "model_calls": 0}
        r = sandbox.run(spec, inputs, workspace, fetch)
        probs = [] if not r.ok else _schema_problems(spec, r.outputs)
        return {"ok": r.ok and not probs, "outputs": r.outputs if r.ok else None,
                "why": r.why if not r.ok else "; ".join(probs), "seconds": round(r.seconds, 3),
                "model_calls": calls_since(mark), "effect": spec.effect, "name": name}


def write_tool(spec_dict: Any, registry: ToolRegistry, sandbox, *, workspace: Optional[str] = None,
               fetch=None, fresh: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The whole path: spec -> static checks -> proof -> saved (proven) or a draft with the reasons."""
    spec, probs = ToolSpec.from_dict(spec_dict)
    if spec is None:
        return {"saved": False, "name": (spec_dict or {}).get("name") if isinstance(spec_dict, dict) else None,
                "why": "spec: " + "; ".join(probs)[:400], "static": None, "proof": None}
    static = static_check(spec)
    if not static.ok:
        path = registry.save(spec, static, None, draft=True, why="static checks: " + "; ".join(static.problems)[:300])
        return {"saved": False, "name": spec.name, "why": "static checks: " + "; ".join(static.problems)[:400],
                "static": static.to_dict(), "proof": None, "path": path}
    proof = prove_tool(spec, sandbox, workspace=workspace, fetch=fetch, fresh=fresh)
    if not proof.passed:
        path = registry.save(spec, static, proof, draft=True, why="proof: " + proof.reason)
        return {"saved": False, "name": spec.name, "why": "proof: " + proof.reason, "static": static.to_dict(),
                "proof": proof.to_dict(), "path": path}
    path = registry.save(spec, static, proof, draft=False)
    return {"saved": True, "name": spec.name, "why": proof.reason, "static": static.to_dict(),
            "proof": proof.to_dict(), "path": path, "sha256": spec.sha256()}
