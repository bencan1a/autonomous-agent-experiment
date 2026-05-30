"""Thin wrapper around the Anthropic SDK with JSON-output parsing and pricing.

Uses tool_choice-free messages; relies on the prompt asking for valid JSON.
parse_response is lenient — finds the first {...} block if there's surrounding chatter.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

# Pricing per 1M tokens. Opus 4.x pricing as of 2026-05.
# Used only for budget tracking; off by a few cents is fine.
_PRICING_USD_PER_MTOK = {
    # Opus 4.x family
    "claude-opus-4-7": {"in": 15.0, "out": 75.0},
    "claude-opus-4-6": {"in": 15.0, "out": 75.0},
    "claude-opus-4-5": {"in": 15.0, "out": 75.0},
    "claude-opus-4-1": {"in": 15.0, "out": 75.0},
    "claude-opus-4-0": {"in": 15.0, "out": 75.0},
    # Sonnet 4.x
    "claude-sonnet-4-6": {"in": 3.0, "out": 15.0},
    "claude-sonnet-4-5": {"in": 3.0, "out": 15.0},
    "claude-sonnet-4-0": {"in": 3.0, "out": 15.0},
}
_DEFAULT_PRICING = {"in": 15.0, "out": 75.0}


@dataclass
class ClaudeResult:
    text: str
    parsed: dict[str, Any] | None
    parse_error: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    model: str
    stop_reason: str | None


def _pricing_for(model: str) -> dict[str, float]:
    # strip date suffix like "-20251201" if present
    base = re.sub(r"-\d{8}$", "", model)
    return _PRICING_USD_PER_MTOK.get(base, _DEFAULT_PRICING)


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    p = _pricing_for(model)
    return (tokens_in / 1_000_000) * p["in"] + (tokens_out / 1_000_000) * p["out"]


def _extract_json(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Best-effort JSON extraction from the model's text response."""
    if not text:
        return None, "empty response"
    stripped = text.strip()
    # Direct parse first
    try:
        return json.loads(stripped), None
    except json.JSONDecodeError:
        pass
    # Strip ```json fences if present
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1)), None
        except json.JSONDecodeError as e:
            return None, f"fenced JSON parse failed: {e}"
    # Find first balanced { ... } block
    start = stripped.find("{")
    if start == -1:
        return None, "no JSON object found in response"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(stripped)):
        ch = stripped[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = stripped[start:i + 1]
                try:
                    return json.loads(candidate), None
                except json.JSONDecodeError as e:
                    return None, f"candidate JSON parse failed: {e}"
    return None, "unbalanced braces"


class ClaudeClient:
    def __init__(self, *, api_key: str, model: str, max_tokens: int = 4096):
        self.model = model
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic(api_key=api_key)

    def invoke(self, system: str, user: str) -> ClaudeResult:
        logger.info("Calling %s (in chars: sys=%d, user=%d)", self.model, len(system), len(user))
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text_parts = [
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        ]
        text = "".join(text_parts)
        parsed, err = _extract_json(text)
        tokens_in = resp.usage.input_tokens
        tokens_out = resp.usage.output_tokens
        cost = estimate_cost(self.model, tokens_in, tokens_out)
        if err:
            logger.warning("JSON parse failed (%s); raw=%r", err, text[:500])
        return ClaudeResult(
            text=text,
            parsed=parsed,
            parse_error=err,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            model=self.model,
            stop_reason=resp.stop_reason,
        )


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv("/home/claudebot/agent/.env")
    client = ClaudeClient(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7"),
    )
    r = client.invoke(
        system="You are a test. Reply with valid JSON only.",
        user='Reply with the exact JSON: {"ok": true, "n": 42}',
    )
    print("model:", r.model)
    print("text:", r.text)
    print("parsed:", r.parsed)
    print("err:", r.parse_error)
    print(f"tokens in/out: {r.tokens_in}/{r.tokens_out}  cost: ${r.cost_usd:.6f}")
