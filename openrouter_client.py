"""Drop-in adapter for OpenRouter — lets the agent session loop run any
model available on OpenRouter (e.g. ``google/gemini-2.5-pro``,
``mistralai/mistral-large``) without touching the session loop itself.

Exposes ``OpenRouterClient`` whose ``.messages.create()`` has the same
signature as ``anthropic.Anthropic().messages.create()``. Translates
between Anthropic message format and the OpenAI-compatible format that
OpenRouter speaks. Returns fake response objects whose attributes match
what the v2/v4 session loops and ``_usage()`` expect.

Usage::

    from openrouter_client import is_openrouter_model, OpenRouterClient

    if is_openrouter_model(instance.model):
        session_client = OpenRouterClient(api_key=os.environ["OPENROUTER_API_KEY"])
    else:
        session_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_OR_URL = "https://openrouter.ai/api/v1/chat/completions"
_OR_MODELS_URL = "https://openrouter.ai/api/v1/models"
_RETRIES = 3

# Transient-HTTP retry (429 rate-limit + 5xx) for the shared transport.
_HTTP_RETRY_STATUSES = {429, 500, 502, 503, 504}
_HTTP_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 1.0
_RETRY_CAP_S = 30.0  # cap any single backoff/Retry-After so a hostile header can't stall a session


def is_openrouter_model(model: str) -> bool:
    """True when model looks like a vendor/name slug (not a native Claude model)."""
    return "/" in model


def list_openrouter_model_ids(*, api_key: str | None = None, timeout: float = 15.0) -> set[str]:
    """Return the set of model ids OpenRouter currently serves.

    The models endpoint is public (no key required); a key is sent if given.
    Raises on network/HTTP failure so callers can distinguish "couldn't check"
    from "definitely absent".
    """
    import requests

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    resp = requests.get(_OR_MODELS_URL, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return {m.get("id") for m in (resp.json().get("data") or []) if m.get("id")}


# --------------------------------------------------------------------------- #
# Shared OpenRouter HTTP transport
# --------------------------------------------------------------------------- #
# One place owns the endpoint URL, the auth/title headers, the
# ``usage: {"include": True}`` body flag, and the usage->tokens/cost parse.
# Both the session-loop client (``_Messages.create``) and the research-panel
# provider (``research.providers.OpenRouterProvider.complete``) build their own
# request body (they differ: tools/tool_choice vs response_format) and run their
# own retry/cost loop (they differ in retry condition and cost accumulation),
# then call these helpers for the shared transport + parse.


def _retry_delay_s(resp: Any, attempt: int) -> float:
    """Backoff for a retryable response: honor ``Retry-After`` if present (capped),
    else exponential backoff. Both bounded by ``_RETRY_CAP_S``."""
    ra = resp.headers.get("Retry-After") if getattr(resp, "headers", None) else None
    if ra:
        try:
            return min(float(ra), _RETRY_CAP_S)
        except ValueError:
            pass
    return min(_BACKOFF_BASE_S * (2 ** attempt), _RETRY_CAP_S)


def or_chat_completion(
    body: dict[str, Any], *, api_key: str, x_title: str, timeout: float = 180.0
) -> dict[str, Any]:
    """POST one OpenRouter chat-completion request and return the parsed JSON.

    Injects ``usage: {"include": True}`` (so OpenRouter reports actual spend),
    sends the standard auth + ``X-Title`` headers, retries on transient HTTP
    errors (429 + 5xx) with bounded backoff, raises on other HTTP errors, and
    returns ``resp.json()``. Callers own the body and any content-level retry loop.
    """
    import time

    import requests  # already in requirements.txt

    body = dict(body)
    body["usage"] = {"include": True}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Title": x_title,
    }
    for attempt in range(_HTTP_MAX_ATTEMPTS):
        resp = requests.post(_OR_URL, json=body, headers=headers, timeout=timeout)
        if resp.status_code in _HTTP_RETRY_STATUSES and attempt < _HTTP_MAX_ATTEMPTS - 1:
            delay = _retry_delay_s(resp, attempt)
            log.warning(
                "OpenRouter HTTP %d; retrying in %.1fs (attempt %d/%d)",
                resp.status_code, delay, attempt + 1, _HTTP_MAX_ATTEMPTS,
            )
            time.sleep(delay)
            continue
        resp.raise_for_status()
        return resp.json()
    # Unreachable: the final attempt always returns or raises above.
    raise RuntimeError("or_chat_completion: retry loop exhausted without returning")


def parse_or_usage(usage_raw: dict[str, Any] | None) -> tuple[int, int, float | None]:
    """OpenRouter ``usage`` block -> ``(tokens_in, tokens_out, cost_or_none)``.

    ``cost`` is OpenRouter's reported actual spend; ``None`` when absent so the
    caller can decide on a fallback (e.g. an estimate).
    """
    usage_raw = usage_raw or {}
    cost_raw = usage_raw.get("cost")
    return (
        int(usage_raw.get("prompt_tokens") or 0),
        int(usage_raw.get("completion_tokens") or 0),
        float(cost_raw) if cost_raw is not None else None,
    )


def or_cost_or_estimate(
    model: str, tokens_in: int, tokens_out: int, reported_cost: float | None
) -> float:
    """Resolve an OpenRouter call's cost: the provider's reported spend if present,
    else an estimate — with a warning, because the estimate is a guess.

    When OpenRouter doesn't report ``usage.cost``, we fall back to
    ``estimate_cost``, which prices unknown (non-Anthropic) models at the
    Anthropic Opus-tier table. That is a DELIBERATE conservative default: it
    over-estimates cheap models, so the budget guard trips early rather than
    overspending. The warning makes the (rare) estimated row visible to the
    operator so it isn't mistaken for an actual charge.
    """
    if reported_cost is not None:
        return float(reported_cost)
    from claude_client import estimate_cost

    est = estimate_cost(model, tokens_in, tokens_out)
    log.warning(
        "OpenRouter reported no usage.cost for %s; estimating $%.4f "
        "(Opus-tier fallback — conservative, may over-estimate)", model, est,
    )
    return est


# --------------------------------------------------------------------------- #
# Fake response objects — attributes match the Anthropic SDK's interface
# --------------------------------------------------------------------------- #

@dataclass
class ORUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    # Populated from OpenRouter's usage.cost — used by session loops that
    # want actual spend rather than the Anthropic pricing table estimate.
    actual_cost_usd: float | None = None


@dataclass
class ORTextBlock:
    type: str = "text"
    text: str = ""


@dataclass
class ORToolUseBlock:
    type: str = "tool_use"
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class ORResponse:
    content: list  # list[ORTextBlock | ORToolUseBlock]
    stop_reason: str = "end_turn"
    usage: ORUsage = field(default_factory=ORUsage)


# --------------------------------------------------------------------------- #
# Format helpers
# --------------------------------------------------------------------------- #

def _block_type(block: Any) -> str:
    if isinstance(block, dict):
        return block.get("type", "")
    return getattr(block, "type", "")


def _block_text(block: Any) -> str:
    if isinstance(block, dict):
        return block.get("text", "")
    return getattr(block, "text", "")


def _system_to_str(system: Any) -> str:
    """Flatten Anthropic system param (str or list of blocks) to plain text."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for b in system:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b["text"])
            elif hasattr(b, "text"):
                parts.append(b.text)
        return "\n".join(parts)
    return str(system or "")


def _tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic tool spec → OpenAI function spec."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        schema = dict(t.get("input_schema") or {})
        schema.pop("cache_control", None)
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": schema,
            },
        })
    return out


def _messages_to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic-format conversation history to OpenAI format.

    Key differences handled here:
    - Anthropic tool_result content (one user message with N results) →
      N separate ``{"role": "tool", ...}`` messages (OpenAI requirement).
    - Anthropic assistant tool_use content blocks →
      ``tool_calls`` array on the assistant message.
    - ``cache_control`` keys on any block are silently ignored.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content")

        if role == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})

            elif isinstance(content, list):
                tool_results = [b for b in content if _block_type(b) == "tool_result"]
                if tool_results:
                    # Explode into individual tool-role messages
                    for b in tool_results:
                        if isinstance(b, dict):
                            tid = b.get("tool_use_id", "")
                            rc = b.get("content", "")
                        else:
                            tid = getattr(b, "tool_use_id", "")
                            rc = getattr(b, "content", "")
                        out.append({
                            "role": "tool",
                            "tool_call_id": tid,
                            "content": str(rc),
                        })
                else:
                    # Regular user content blocks → join as text
                    text = " ".join(
                        _block_text(b) for b in content if _block_type(b) == "text"
                    )
                    out.append({"role": "user", "content": text or " "})
            else:
                out.append({"role": "user", "content": str(content or "")})

        elif role == "assistant":
            if isinstance(content, str):
                out.append({"role": "assistant", "content": content})

            elif isinstance(content, list):
                text_parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                for b in content:
                    btype = _block_type(b)
                    if btype == "text":
                        t = _block_text(b)
                        if t:
                            text_parts.append(t)
                    elif btype == "tool_use":
                        if isinstance(b, dict):
                            bid = b.get("id", "")
                            bname = b.get("name", "")
                            binput = b.get("input", {})
                        else:
                            bid = getattr(b, "id", "")
                            bname = getattr(b, "name", "")
                            binput = getattr(b, "input", {})
                        tool_calls.append({
                            "id": bid,
                            "type": "function",
                            "function": {
                                "name": bname,
                                "arguments": json.dumps(binput or {}),
                            },
                        })

                oai_msg: dict[str, Any] = {"role": "assistant"}
                oai_msg["content"] = " ".join(text_parts) or None
                if tool_calls:
                    oai_msg["tool_calls"] = tool_calls
                out.append(oai_msg)

    return out


def _parse_response(data: dict[str, Any]) -> ORResponse:
    """OpenAI-format API response → Anthropic-compatible ORResponse."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish_reason = choice.get("finish_reason") or ""

    blocks: list[Any] = []
    text = msg.get("content") or ""
    if text:
        blocks.append(ORTextBlock(type="text", text=text))

    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            inputs = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, ValueError):
            inputs = {}
        blocks.append(ORToolUseBlock(
            type="tool_use",
            id=tc.get("id", ""),
            name=fn.get("name", ""),
            input=inputs,
        ))

    stop_reason = {
        "tool_calls": "tool_use",
        "length": "max_tokens",
    }.get(finish_reason, "end_turn")

    tokens_in, tokens_out, cost = parse_or_usage(data.get("usage"))
    return ORResponse(
        content=blocks,
        stop_reason=stop_reason,
        usage=ORUsage(
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            actual_cost_usd=cost,
        ),
    )


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class _Messages:
    def __init__(self, api_key: str, timeout: float = 180.0):
        self._key = api_key
        self._timeout = timeout

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: Any = None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **_kwargs,
    ) -> ORResponse:
        oai_messages: list[dict[str, Any]] = []
        if system:
            sys_text = _system_to_str(system).strip()
            if sys_text:
                oai_messages.append({"role": "system", "content": sys_text})
        oai_messages.extend(_messages_to_openai(messages))

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": oai_messages,
        }
        if tools:
            body["tools"] = _tools_to_openai(tools)
            body["tool_choice"] = "auto"

        parsed = ORResponse(content=[])
        for attempt in range(_RETRIES):
            data = or_chat_completion(
                body, api_key=self._key, x_title="agent-session", timeout=self._timeout,
            )
            parsed = _parse_response(data)
            # Retry on empty content (some models occasionally return blank)
            if parsed.content or parsed.stop_reason != "end_turn":
                return parsed
            log.warning(
                "OpenRouter returned empty content for %s (attempt %d/%d)",
                model, attempt + 1, _RETRIES,
            )
        return parsed


class OpenRouterClient:
    """Minimal drop-in for ``anthropic.Anthropic`` that routes through OpenRouter.

    Only ``.messages.create()`` is implemented — that is all the session loops
    and ``compact_session_transcript`` need.
    """

    def __init__(self, api_key: str, timeout: float = 180.0):
        self.messages = _Messages(api_key=api_key, timeout=timeout)


# --------------------------------------------------------------------------- #
# Loop-client selection (Anthropic SDK vs OpenRouter)
# --------------------------------------------------------------------------- #

def make_session_client(model: str, *, anthropic_mod: Any = None) -> tuple[Any, bool]:
    """Resolve the session loop's model client + whether prompt caching applies.

    OpenRouter models -> ``(OpenRouterClient, False)`` (prompt caching is an
    Anthropic-specific feature). Native Claude models -> a raw
    ``anthropic.Anthropic`` client with caching enabled. The DISTINCT Anthropic
    path is deliberate: Anthropic models go through the official SDK, not the
    OpenRouter adapter.

    ``anthropic_mod`` lets the caller pass the (possibly monkeypatched) ``anthropic``
    module reference it already imported, so the SDK client is built through the
    same reference tests patch; defaults to a real ``import anthropic``.

    Reads keys via ``session_common._env`` (the same source ``setup_session``
    uses), so the shared ``.env`` is the single source of credentials.
    """
    from session_common import _env

    if is_openrouter_model(model):
        client: Any = OpenRouterClient(api_key=_env("OPENROUTER_API_KEY", required=True))
        log.info("Using OpenRouter model: %s (caching disabled)", model)
        return client, False  # prompt caching is Anthropic-specific
    if anthropic_mod is None:
        import anthropic as anthropic_mod

    return anthropic_mod.Anthropic(api_key=_env("ANTHROPIC_API_KEY", required=True)), True
