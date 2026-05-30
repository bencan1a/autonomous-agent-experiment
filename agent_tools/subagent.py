"""Spawn a one-shot Claude sub-agent and log the call."""

from __future__ import annotations

from typing import Any

from claude_client import estimate_cost


def spawn_subagent(
    prompt: str,
    system_prompt: str | None = None,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 2048,
    *,
    ctx: Any,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system_prompt:
        kwargs["system"] = system_prompt
    resp = ctx.anthropic.messages.create(**kwargs)
    text = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    )
    tokens_in = resp.usage.input_tokens
    tokens_out = resp.usage.output_tokens
    cost = estimate_cost(model, tokens_in, tokens_out)
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
