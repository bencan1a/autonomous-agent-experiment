"""Provider abstraction for researcher seats.

The lowest-common-denominator interface (one system + one user message +
max_tokens, single-shot) so a seat can be Anthropic today and OpenAI/Gemini
later with no change anywhere else. Adding a vendor = a new class here plus a
pricing entry in ``claude_client``; ``Seat`` and the config already carry the
``provider``/``model`` fields.

Mirrors ``agent_tools/subagent.py`` for the Anthropic call + cost estimate, but
does NOT log — the panel logs every call centrally (cost + audit) so research
spend flows into the same budget tracking.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

from claude_client import estimate_cost


@dataclass
class LLMResponse:
    text: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    model: str
    provider: str


class LLMProvider(Protocol):
    name: str

    def complete(
        self, *, system: str, prompt: str, model: str, max_tokens: int
    ) -> LLMResponse: ...


class AnthropicProvider:
    """Wraps the runner's ``anthropic.Anthropic`` client."""

    name = "anthropic"

    def __init__(self, client: Any):
        self._client = client

    def complete(
        self, *, system: str, prompt: str, model: str, max_tokens: int
    ) -> LLMResponse:
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )
        tokens_in = resp.usage.input_tokens
        tokens_out = resp.usage.output_tokens
        return LLMResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=estimate_cost(model, tokens_in, tokens_out),
            model=model,
            provider=self.name,
        )


class OpenRouterProvider:
    """OpenAI-compatible aggregator → many model families via one key.

    One integration unlocks cross-vendor diversity (GPT / Gemini / DeepSeek / …)
    by model slug (e.g. ``google/gemini-2.5-pro``). Uses ``requests`` (already a
    dependency) and reads OpenRouter's *actual* returned spend so the panel's
    budget caps stay accurate for non-Anthropic models (``estimate_cost`` only
    knows Anthropic prices). A failed call raises — the panel turns that seat into
    a graceful ``failed`` and carries on.
    """

    name = "openrouter"
    _URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, *, timeout: float = 180.0):
        self._key = api_key
        self._timeout = timeout

    def complete(
        self, *, system: str, prompt: str, model: str, max_tokens: int
    ) -> LLMResponse:
        import requests

        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "usage": {"include": True},   # ask OpenRouter to report actual cost
        }
        # Force structured JSON output for models that emit invalid JSON on large
        # notes (e.g. Mistral, DeepSeek). EXCLUDE Gemini: its OpenRouter endpoint
        # returns empty content under response_format=json_object. Gemini is
        # reliable without it (given the token headroom).
        if not model.startswith("google/"):
            body["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self._key}",
            "X-Title": "agent-research-panel",
        }
        # Retry on transient EMPTY completions: reasoning models (e.g. Gemini)
        # occasionally return no content via OpenRouter; the identical request
        # succeeds on retry. Cost accrues per attempt.
        text, tokens_in, tokens_out, cost = "", 0, 0, 0.0
        for _attempt in range(3):
            resp = requests.post(self._URL, json=body, headers=headers, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            text = msg.get("content") or ""
            usage = data.get("usage") or {}
            tokens_in = int(usage.get("prompt_tokens") or 0)
            tokens_out = int(usage.get("completion_tokens") or 0)
            c = usage.get("cost")
            cost += float(c) if c is not None else estimate_cost(model, tokens_in, tokens_out)
            if text.strip():
                break
        return LLMResponse(
            text=text, tokens_in=tokens_in, tokens_out=tokens_out,
            cost_usd=float(cost), model=model, provider=self.name,
        )


def get_provider(name: str, *, anthropic_client: Any) -> LLMProvider:
    """Resolve a provider by name.

    ``openrouter`` reads ``OPENROUTER_API_KEY`` from the environment (loaded from
    the shared ``.env`` by every runner / script), so heterogeneous panels need
    no extra plumbing — a seat just sets ``provider: "openrouter"``.
    """
    if name == "anthropic":
        return AnthropicProvider(anthropic_client)
    if name == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise ValueError("OPENROUTER_API_KEY is not set in the environment")
        return OpenRouterProvider(key)
    raise ValueError(f"unknown research provider: {name!r}")
