"""Explicit local CPU Laya transport for bounded REA typed choices.

ML libraries run only in an owned Apple container with readonly local assets.
No model download, remote endpoint, published port or host credential is used.
The dedicated NAT network and no-DNS are not an operating-system egress block.
"""
import atexit
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import threading
import time
import uuid

from . import openjev as O
from . import rea_laya_worker as W

DIR_ENV = "ANYUSE_REA_LAYA_DIR"
CHECKPOINT_ENV = "ANYUSE_REA_LAYA_CHECKPOINT"
CONTAINER_ENV = "ANYUSE_REA_LAYA_CONTAINER"
IMAGE_ENV = "ANYUSE_REA_LAYA_IMAGE"
NETWORK = "anyuse-net"
IMAGE = "python:3.12-slim"


def serve_command(cli, image, assets, name, checkpoint):
    source = Path(W.__file__).read_text(encoding="utf-8")
    script = ('set -e; python -m venv /tmp/v >/dev/null; '
              '/tmp/v/bin/pip install --disable-pip-version-check -q --no-index '
              '--find-links /work/wheels laya==0.3.20 >/dev/null 2>&1; '
              'exec /tmp/v/bin/python -u -c "$1" "$2"')
    return [cli, "run", "--rm", "-i", "--name", name, "--network", NETWORK,
            "--no-dns", "-c", "8", "-m", "8G", "--mount",
            "type=bind,source=" + str(assets) + ",target=/work,readonly",
            "-e", "HF_HUB_OFFLINE=1", "-e", "TRANSFORMERS_OFFLINE=1",
            "-e", "HF_HUB_DISABLE_TELEMETRY=1", "-e", "TOKENIZERS_PARALLELISM=false",
            image, "sh", "-c", script, "sh", source, checkpoint]


class LayaClient:
    """Lazily owned runtime; call close(), or use the context manager.

    available checks local configuration only. It does not start a container or
    assert model quality. metadata and last_receipt are copies suitable for the
    caller's retained inference record. Failures return an empty O.Answer.
    """
    def __init__(self, laya_dir=None, *, checkpoint=None, container=None, image=None,
                 startup_timeout_s=240, timeout_s=30, popen=None, runner=None,
                 line_reader=None, writer=None):
        configured_dir = laya_dir if laya_dir is not None else O._setting(DIR_ENV, "")
        self.assets = Path(configured_dir or "").expanduser()
        self.checkpoint = checkpoint or O._setting(CHECKPOINT_ENV, "typed")
        self.cli = container or O._setting(CONTAINER_ENV, "") or shutil.which("container") or "container"
        self.image = image or O._setting(IMAGE_ENV, IMAGE)
        self.startup_timeout_s = min(600, max(1, float(startup_timeout_s)))
        self.timeout_s = min(120, max(.1, float(timeout_s)))
        self._popen = popen or subprocess.Popen
        self._runner = runner or subprocess.run
        self._reader = line_reader
        self._writer = writer
        self.proc = None
        self.name = ""
        self._buffer = b""
        self._metadata = {}
        self.last_receipt = {}
        self.last_call = {"model_calls": 0, "inference_confirmed": False}
        self.cleanup = {"attempted": False, "confirmed": False}
        self._lock = threading.RLock()
        self._configured = bool(configured_dir)
        atexit.register(self.close)

    @property
    def available(self):
        root = self.assets / "models" / self.checkpoint
        required = ("model.safetensors", "rl_agent_config.json", "encoder/config.json",
                    "tokenizer/tokenizer.json", "tokenizer/tokenizer_config.json")
        return (self._configured and self.checkpoint in W.CHECKPOINTS and
                (self.assets / "wheels").is_dir() and all((root / f).is_file() for f in required))

    @property
    def metadata(self):
        return copy.deepcopy(self._metadata)

    def _read(self, timeout):
        if self._reader:
            data = self._reader(self.proc, timeout, W.MAX_WIRE)
            if not isinstance(data, bytes) or len(data) > W.MAX_WIRE:
                raise ValueError("invalid protocol bytes")
        else:
            deadline = time.monotonic() + timeout
            while b"\n" not in self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.proc.stdout], [], [], remaining)[0]:
                    raise TimeoutError("local Laya response timeout")
                chunk = os.read(self.proc.stdout.fileno(), min(4096, W.MAX_WIRE + 2 - len(self._buffer)))
                if not chunk:
                    raise EOFError("local Laya closed output")
                self._buffer += chunk
                if len(self._buffer) > W.MAX_WIRE + 1:
                    raise ValueError("oversized local Laya reply")
            data, self._buffer = self._buffer.split(b"\n", 1)
            if len(data) > W.MAX_WIRE:
                raise ValueError("oversized local Laya reply")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("invalid local Laya message")
        return result

    def _write(self, data):
        if self._writer:
            self._writer(self.proc, data, self.timeout_s)
            return
        fd = self.proc.stdin.fileno()
        os.set_blocking(fd, False)
        deadline = time.monotonic() + self.timeout_s
        offset = 0
        while offset < len(data):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                raise TimeoutError("local Laya request timeout")
            try:
                sent = os.write(fd, data[offset:offset + 4096])
            except BlockingIOError:
                continue
            if sent <= 0:
                raise EOFError("local Laya closed input")
            offset += sent

    def start(self):
        with self._lock:
            if self.proc is not None:
                return
            if not self.available:
                raise ValueError(DIR_ENV + " must contain the offline wheels and checkpoint")
            # image inspect never downloads. Pin the inspected cached identity, rather
            # than allowing container run to fetch a missing tag.
            inspected = self._runner([self.cli, "image", "inspect", self.image],
                                     capture_output=True, timeout=30, check=True)
            images = json.loads(inspected.stdout)
            if not isinstance(images, list) or len(images) != 1:
                raise ValueError("local runtime image not found")
            config = images[0]["configuration"]
            image_digest = config["descriptor"]["digest"]
            if not isinstance(image_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
                raise ValueError("invalid local runtime image digest")
            image = config["name"].split("@", 1)[0].rsplit(":", 1)[0] + "@" + image_digest
            self.name = "anyuse-rea-laya-" + uuid.uuid4().hex[:12]
            try:
                self.proc = self._popen(serve_command(self.cli, image, self.assets.resolve(), self.name,
                                                    self.checkpoint), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
                ready = self._read(self.startup_timeout_s)
                meta = ready.get("metadata", {})
                if (set(ready) != {"ready", "metadata"} or ready["ready"] is not True or
                        meta.get("protocol") != W.PROTOCOL or meta.get("checkpoint") != self.checkpoint or
                        meta.get("laya_version") != "0.3.20" or meta.get("warmup_calls") != 0 or
                        meta.get("device") != "cpu" or not isinstance(meta.get("model_files"), dict)):
                    raise ValueError("invalid local Laya readiness")
                self._metadata = dict(meta, image_digest=image_digest, network=NETWORK, dns=False,
                                      worker_sha256=hashlib.sha256(Path(W.__file__).read_bytes()).hexdigest())
            except Exception:
                self.close()
                raise

    def decide(self, state, questions, *, images=(), steps=None, guard_obs=None):
        t0 = time.monotonic()
        request = {"protocol": W.PROTOCOL, "id": uuid.uuid4().hex,
                   "state": state, "questions": questions}
        with self._lock:
            self.last_receipt = {"backend": "laya", "request_id": request["id"]}
            self.last_call = {"model_calls": 0, "inference_confirmed": False}
            try:
                if images or (steps is not None and (type(steps) is not int or steps != 1)) or guard_obs is not None:
                    raise ValueError("local Laya supports text choice reads only")
                W.validate_request(request)
                request_hash = W.digest(request)
                self.last_receipt["request_sha256"] = request_hash
                self.start()
                self.last_call = {"model_calls": 1, "inference_confirmed": False}
                self._write(W.encode(request) + b"\n")
                # A timed-out dispatch cannot be claimed as zero inference. The
                # worker's bound response can subsequently prove preflight zero.
                result = self._read(self.timeout_s)
                if (result.get("protocol") != W.PROTOCOL or result.get("id") != request["id"] or
                        result.get("request_sha256") != request_hash):
                    raise ValueError("local Laya response identity mismatch")
                self.last_receipt.update(metadata=self.metadata, response=copy.deepcopy(result))
                if type(result.get("inference_calls")) is int and result["inference_calls"] in (0, 1):
                    self.last_call = {"model_calls": result["inference_calls"], "inference_confirmed": True}
                if result.get("error"):
                    raise ValueError(str(result["error"])[:200])
                answers = W.validate_answers(result.get("answers"), questions)
                budget = result.get("token_budget", {})
                if budget.get("truncated") is not False or result.get("inference_calls") != 1:
                    raise ValueError("local Laya lacks complete-input inference receipt")
                usage = result.get("usage", {})
                answer = O.Answer(answers=answers, server_ms=result.get("server_ms"),
                                  input_tokens=usage.get("input_tokens"))
            except Exception as exc:
                self.close()
                error = type(exc).__name__ + ": " + str(exc)[:200]
                self.last_receipt["error"] = error
                answer = O.Answer(error=error)
            answer.ms = (time.monotonic() - t0) * 1000
            if self.last_call["model_calls"]:
                from .model_adapter import note_external_model_call
                note_external_model_call("laya-local", self.checkpoint, feature="rea_picker", cost_usd=0)
            self.last_receipt["wall_ms"] = answer.ms
            self.last_receipt["call"] = copy.deepcopy(self.last_call)
            return answer

    def close(self):
        with self._lock:
            proc, self.proc = self.proc, None
            if proc is None:
                return
            cleanup = {"attempted": True, "container": self.name,
                       "stop_returncode": None, "process_returncode": None,
                       "process_exited": False, "kill_attempted": False, "errors": []}
            try:
                proc.stdin.close()
            except Exception as exc:
                cleanup["errors"].append("stdin: " + type(exc).__name__)
            try:
                stopped = self._runner([self.cli, "stop", self.name], capture_output=True, timeout=15)
                cleanup["stop_returncode"] = getattr(stopped, "returncode", None)
            except Exception as exc:
                cleanup["errors"].append("stop: " + type(exc).__name__)
            try:
                cleanup["process_returncode"] = proc.wait(timeout=5)
                cleanup["process_exited"] = True
            except Exception as exc:
                cleanup["errors"].append("wait: " + type(exc).__name__)
                try:
                    cleanup["kill_attempted"] = True
                    proc.kill()
                    cleanup["process_returncode"] = proc.wait(timeout=5)
                    cleanup["process_exited"] = True
                except Exception as exc:
                    cleanup["errors"].append("kill/wait: " + type(exc).__name__)
            cleanup["confirmed"] = cleanup["stop_returncode"] == 0 and cleanup["process_exited"]
            self.cleanup = cleanup
            self.last_receipt["cleanup"] = copy.deepcopy(cleanup)
            self._buffer = b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
