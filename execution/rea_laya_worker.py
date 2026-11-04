"""Fixed text-choice worker. Imported without ML dependencies; executed in Linux.

The wire is private to rea_laya. No warmup inference is performed. All inference
inputs must fit the installed SDK's sequence format without truncation.
"""
import contextlib
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import sys
import time

PROTOCOL = "anyuse.rea-laya-choice/1"
MAX_WIRE = 65536
CHECKPOINTS = ("typed", "en", "multilingual")


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def _text(value, limit, label):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        raise ValueError("invalid " + label)


def validate_request(request):
    if not isinstance(request, dict) or set(request) != {"protocol", "id", "state", "questions"}:
        raise ValueError("invalid request fields")
    if request["protocol"] != PROTOCOL or not re.fullmatch(r"[a-f0-9]{32}", str(request["id"])):
        raise ValueError("invalid request identity")
    if len(encode(request)) > MAX_WIRE:
        raise ValueError("request too large")
    # JSON states retain their exact serialization; arbitrary Python objects do not.
    state = request["state"]
    if not isinstance(state, (str, dict, list)) or len(encode(state)) > 32768:
        raise ValueError("invalid or oversized state")
    questions = request["questions"]
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 4:
        raise ValueError("one to four choice questions required")
    for qid, q in questions.items():
        if not isinstance(qid, str) or not re.fullmatch(r"[a-zA-Z0-9_]{1,64}", qid):
            raise ValueError("invalid question id")
        if not isinstance(q, dict) or set(q) != {"type", "instructions", "criteria"} or q["type"] != "choice":
            raise ValueError("only typed choice questions are supported")
        _text(q["instructions"], 2048, "instructions")
        criteria = q["criteria"]
        # The catalog permits eight actions, plus the explicit abstention label.
        if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 9:
            raise ValueError("two to nine choice criteria required")
        for key, description in criteria.items():
            _text(key, 128, "choice label")
            _text(description, 1024, "choice description")
    return request


def token_budget(agent, state, questions):
    """Check every token the Laya 0.3.20 build_sequence path would truncate."""
    tok = agent.tok
    max_len = agent.cfg.get("max_len", 512)
    head_max = agent.cfg.get("head_max_len", 192)
    if type(max_len) is not int or type(head_max) is not int or not 1 <= head_max <= max_len <= 4096:
        raise ValueError("unsupported model context")
    serialized = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    def ids(text):
        # Refuse the SDK's mask-token substitution too: retained input is literal.
        if tok.mask_token in text:
            raise ValueError("literal model mask token in input")
        return tok(text, add_special_tokens=False)["input_ids"]
    state_tokens = len(ids(serialized))
    rows = {}
    for qid, q in questions.items():
        head_tokens = len(ids("choice question: " + q["instructions"]))
        option_lengths = [len(ids(" " + key + ": " + desc)) for key, desc in q["criteria"].items()]
        if any(n > 48 for n in option_lengths):
            raise ValueError("choice description would be truncated")
        option_tokens = sum(n + 1 for n in option_lengths)
        available_head = head_max - option_tokens
        if available_head < 16 or head_tokens > max(8, available_head):
            raise ValueError("choice instructions or options would be truncated")
        sequence_tokens = 4 + head_tokens + option_tokens + state_tokens
        if sequence_tokens > max_len:
            raise ValueError("state would be truncated")
        rows[qid] = {"state_tokens": state_tokens, "sequence_tokens": sequence_tokens,
                     "instruction_tokens": head_tokens, "option_tokens": option_lengths}
    return {"max_len": max_len, "head_max_len": head_max, "truncated": False,
            "questions": rows}


def validate_answers(answers, questions):
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ValueError("answer question mismatch")
    for qid, q in questions.items():
        a = answers[qid]
        if not isinstance(a, dict) or a.get("type") != "choice" or a.get("choice") not in q["criteria"]:
            raise ValueError("invalid typed choice answer")
        for key in ("confidence", "answer_confidence"):
            v = a.get(key)
            if type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1:
                raise ValueError("invalid choice confidence")
        probabilities = a.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != set(q["criteria"]):
            raise ValueError("invalid choice probabilities")
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
               for v in probabilities.values()) or abs(sum(probabilities.values()) - 1) > .002:
            raise ValueError("invalid choice distribution")
        if probabilities[a["choice"]] != max(probabilities.values()):
            raise ValueError("choice is not most probable")
    encode(answers)
    return answers


def model_files(root):
    out = {}
    for name in ("model.safetensors", "rl_agent_config.json", "encoder/config.json",
                 "tokenizer/tokenizer.json", "tokenizer/tokenizer_config.json"):
        path = root / name
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(block)
        out[name] = {"sha256": h.hexdigest(), "bytes": path.stat().st_size}
    return out


def serve(checkpoint):
    if checkpoint not in CHECKPOINTS:
        raise ValueError("unsupported checkpoint")
    # Import/load chatter never shares the protocol pipe. No warmup forward pass.
    wire = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from laya import Agent
        if importlib.metadata.version("laya") != "0.3.20":
            raise ValueError("unreviewed Laya sequence protocol")
        torch.set_num_threads(8)
        root = Path("/work/models") / checkpoint
        pins = model_files(root)
        t0 = time.monotonic()
        agent = Agent(str(root), device="cpu", fast=False, compile=False)
    metadata = {"protocol": PROTOCOL, "backend": "laya", "checkpoint": checkpoint,
                "laya_version": "0.3.20", "torch_version": str(torch.__version__),
                "device": "cpu", "threads": torch.get_num_threads(), "model_files": pins,
                "load_ms": round((time.monotonic() - t0) * 1000, 3), "warmup_calls": 0}
    wire.write(encode({"ready": True, "metadata": metadata}).decode() + "\n")
    wire.flush()
    # A bounded binary reader prevents malformed callers allocating arbitrary lines.
    while True:
        line = sys.stdin.buffer.readline(MAX_WIRE + 2)
        if not line:
            return
        if len(line) > MAX_WIRE + 1 or not line.endswith(b"\n"):
            raise ValueError("oversized protocol line")
        request = validate_request(json.loads(line))
        t0 = time.monotonic()
        reply = {"protocol": PROTOCOL, "id": request["id"], "request_sha256": digest(request),
                 "inference_calls": 0}
        try:
            with contextlib.redirect_stdout(sys.stderr):
                budget = token_budget(agent, request["state"], request["questions"])
                reply["token_budget"] = budget
                reply["inference_calls"] = 1
                result = agent.system_one(request["state"], request["questions"])
            reply.update(answers=validate_answers(result["answers"], request["questions"]),
                         token_budget=budget, usage=result.get("usage", {}),
                         server_ms=round((time.monotonic() - t0) * 1000, 3), inference_calls=1)
        except Exception as exc:
            # Refusals before the forward pass must not claim an inference.
            reply.update(error=type(exc).__name__ + ": " + str(exc)[:160])
        encoded = encode(reply)
        if len(encoded) > MAX_WIRE:
            raise ValueError("oversized worker reply")
        wire.write(encoded.decode() + "\n")
        wire.flush()


if __name__ == "__main__":
    serve(sys.argv[1])
