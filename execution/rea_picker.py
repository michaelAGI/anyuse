"""Explicit typed chooser transports for the REA action catalog.

Contract: https://github.com/Davipar/djev-dev/blob/main/docs/api.md
Djev uses /v1/request and no request model field; OpenJev uses /v1/systemone.
"""
import hashlib
import base64
import json
import math
import os

from . import openjev as O


BACKENDS = ("auto", "djev", "openjev", "laya", "jev", "reasoning")
MAX_WIRE_BYTES = 128 * 1024
# A conservative reservation, not a claim about a provider's token price.
REQUEST_RESERVATION_USD = .01


class RecordedClient(O.Client):
    """Retain actual wire hashes and account for a typed request at send time."""
    backend = "openjev"
    protocol = "/v1/systemone"

    def __init__(self, *args, poster=None, budget=None, payload_transform=None, **kwargs):
        from .model_adapter import RunBudget
        self.budget = budget or RunBudget.from_env()
        self.last_call = {"model_calls": 0}
        post = poster or O._urllib_post

        def recorded(url, body, headers, timeout):
            from . import spend
            from .model_adapter import note_external_model_call
            if payload_transform is not None:
                body = payload_transform(body)
            if len(body) > MAX_WIRE_BYTES:
                raise ValueError("typed request exceeds byte budget")
            reservation = self.budget.reserve(1, REQUEST_RESERVATION_USD * 1_000_000, 0)
            if reservation is None:
                raise ValueError("typed chooser run budget exhausted")
            ticket, why = spend.reserve(REQUEST_RESERVATION_USD, feature="rea_picker")
            if why:
                self.budget.settle(reservation, 0)
                raise ValueError("typed chooser daily budget refused")
            note_external_model_call(self.backend, self.model, feature="rea_picker")
            self.last_call = {"model_calls": 1, "backend": self.backend, "model": self.model,
                              "protocol": self.protocol, "request_sha256": hashlib.sha256(body).hexdigest(),
                              "request_base64": base64.b64encode(body).decode(), "request": json.loads(body)}
            actual, usage = None, {}
            try:
                status, response_headers, response = post(url, body, headers, timeout)
                self.last_call.update(http_status=status, response_bytes=len(response),
                                      response_sha256=hashlib.sha256(response).hexdigest())
                if len(response) > MAX_WIRE_BYTES:
                    raise ValueError("typed response exceeds byte budget")
                self.last_call["response_base64"] = base64.b64encode(response).decode()
                try:
                    data = json.loads(response)
                    self.last_call["response"] = data
                    usage = data.get("usage", {}) if isinstance(data, dict) else {}
                    cost = usage.get("cost") if isinstance(usage, dict) else None
                    if type(cost) in {int, float} and math.isfinite(cost) and cost >= 0:
                        actual = cost
                except (ValueError, TypeError):
                    pass
                return status, response_headers, response
            finally:
                self.budget.settle(reservation, actual)
                cost = actual if actual is not None else REQUEST_RESERVATION_USD
                self.last_call.update(cost_usd=cost, cost_basis="provider" if actual is not None else "reservation")
                error = spend.book(ticket, provider=self.backend, model=self.model,
                                   prompt_tokens=usage.get("input_tokens", 0) if isinstance(usage, dict) else 0,
                                   completion_tokens=usage.get("output_tokens", 0) if isinstance(usage, dict) else 0,
                                   cost_usd=cost, cost_basis=self.last_call["cost_basis"]) if ticket is not None else ""
                if error:
                    raise ValueError("typed chooser spend booking failed")

        super().__init__(*args, poster=recorded, **kwargs)

    @property
    def metadata(self):
        return {"backend": self.backend, "model": self.model, "protocol": self.protocol}

    def decide(self, *args, **kwargs):
        self.last_call = {"model_calls": 0}
        return super().decide(*args, **kwargs)


class DjevClient(RecordedClient):
    backend = "djev"
    protocol = "/v1/request"

    def __init__(self, url=None, *, timeout_s=3, poster=None, key=None, budget=None):
        raw = url if url is not None else O._setting("ANYUSE_DJEV_URL", "") or os.environ.get("DJEV_URL", "")
        base = O.valid_base_url(raw)
        target = base if base.endswith("/v1/request") else base + "/v1/request"
        post = poster or O._urllib_post

        def transform(body):
            payload = json.loads(body)
            payload.pop("model", None)
            if payload.pop("steps", 1) != 1:
                raise ValueError("Djev structured reads use one step")
            if len(payload.get("images", [])) > 1:
                raise ValueError("Djev accepts one state image")
            payload["options"] = {"seed": 0, "samples": 1}
            return json.dumps(payload).encode()

        def wire(_url, body, headers, timeout):
            return post(target, body, dict(headers, **{"User-Agent": "anyuse-rea-djev/0.1"}), timeout)

        super().__init__(url=base, model="djev-0.1", timeout_s=timeout_s, poster=wire,
                         payload_transform=transform, key=key, budget=budget)

    def _auth(self):
        if self._key is None:
            from .agent_browser import read_api_key
            self._key = read_api_key("DJEV_API_KEY") or ""
        return self._key


class JevClient(RecordedClient):
    """Explicit hosted Jev decisions route; never selected by auto mode."""
    backend = "jev"
    protocol = "/api/alpha/decisions"

    def __init__(self, *, poster=None, key=None, timeout_s=30, budget=None):
        from .judge import JEV_MODEL, JEV_URL
        post = poster or O._urllib_post
        def wire(_url, body, headers, timeout):
            return post(JEV_URL, body, headers, timeout)
        super().__init__(url=JEV_URL, model=JEV_MODEL, timeout_s=timeout_s,
                         poster=wire, key=key, budget=budget)

    def _auth(self):
        if self._key is None:
            from .judge import _read_key
            self._key = _read_key("OPENROUTER_API_KEY") or ""
        return self._key

    @property
    def available(self):
        return bool(self._auth())


def configured_client(backend=None, *, budget=None):
    name = str(backend or O._setting("ANYUSE_REA_PICKER", "auto")).strip().lower()
    if name not in BACKENDS:
        raise ValueError("unknown REA chooser backend")
    if name == "auto":
        name = "djev" if O._setting("ANYUSE_DJEV_URL", "") or os.environ.get("DJEV_URL") else "openjev"
    if name == "djev":
        return DjevClient(budget=budget)
    if name == "laya":
        from .rea_laya import LayaClient
        return LayaClient()
    if name == "jev":
        return JevClient(budget=budget)
    if name == "reasoning":
        from .rea_reasoning_choice import ReasoningClient
        return ReasoningClient(budget=budget)
    return RecordedClient(budget=budget)
