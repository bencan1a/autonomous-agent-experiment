"""Pricing tables + lenient JSON extraction for model responses.

Despite the historical name, this module is NOT an LLM client — the session loops
call the Anthropic SDK / OpenRouterClient directly. It is a shared utility:
per-model pricing (`_pricing_for`, `estimate_cost`) and best-effort JSON extraction
from a model's text (`_extract_json` — finds the first balanced {...} block even
amid surrounding chatter). Imported across the session loops, the subagent tool,
and the research panel.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Pricing per 1M tokens. Opus 4.x pricing as of 2026-05.
# Used only for budget tracking; off by a few cents is fine.
_PRICING_USD_PER_MTOK = {
    # Opus 4.x family
    "claude-opus-4-8": {"in": 15.0, "out": 75.0},
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
