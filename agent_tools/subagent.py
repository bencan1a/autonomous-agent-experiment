"""Spawn a one-shot sub-agent (same provider/model as the session) and log the call."""

from __future__ import annotations

from typing import Any

from claude_client import estimate_cost


def spawn_subagent(
    prompt: str,
    system_prompt: str | None = None,
    model: str | None = None,
    max_tokens: int = 2048,
    *,
    ctx: Any,
) -> dict[str, Any]:
    # Default to the session's model + client so the subagent runs on the SAME
    # provider/model as the session (the documented "single model per session"
    # contract). For an OpenRouter session, ctx.client is the OpenRouter adapter;
    # both it and the Anthropic SDK expose the same messages.create() interface.
    model = model or getattr(ctx, "model", None) or "claude-sonnet-4-6"
    client = getattr(ctx, "client", None) or ctx.anthropic
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system_prompt:
        kwargs["system"] = system_prompt
    resp = client.messages.create(**kwargs)
    text = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    )
    tokens_in = resp.usage.input_tokens
    tokens_out = resp.usage.output_tokens
    # Prefer the provider's reported actual cost (OpenRouter); else estimate.
    actual = getattr(resp.usage, "actual_cost_usd", None)
    cost = actual if actual is not None else estimate_cost(model, tokens_in, tokens_out)
    try:
        ctx.episodic.log_subagent_call(
            session_id=ctx.session_id,
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
            response=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
        )
    except Exception:
        pass
    return {
        "text": text,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost,
        "model": model,
    }
