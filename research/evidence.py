"""Build the per-session evidence bundle the panel reviews.

Pure assembly from the episodic store — no LLM. Produces compact text with stable
citation anchors (``episode#<id>``, ``action#<id>``) that the seats are required
to cite. Long fields are truncated and the whole bundle is capped so it stays
prompt-friendly. For v2-v4 a session has many tick-episodes sharing ``session_id``;
for v1 it's one episode — both are handled by filtering on ``session_id``.
"""

from __future__ import annotations

import json
from typing import Any

# Generous caps so seats see (nearly) the whole session. Sonnet's context window is
# large; bounding input only guards against pathological blow-ups, not normal sessions.
_FIELD_CAP = 4000      # per long text field (journal, internal_state, ...)
_ARG_CAP = 600         # per tool-call args/result summary
_BUNDLE_CAP = 100000   # overall safety cap (~25k tokens)


def _clip(text: Any, cap: int) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= cap:
        return s
    return s[:cap] + f"… [+{len(s) - cap} chars]"


def _session_row(episodic: Any, session_id: int) -> dict[str, Any] | None:
    try:
        for s in episodic.all_sessions():
            if s.get("id") == session_id:
                return s
    except Exception:
        pass
    return None


def build_session_evidence(
    episodic: Any,
    session_id: int,
    invocation_num: int | None,
    *,
    max_chars: int = _BUNDLE_CAP,
) -> str:
    parts: list[str] = []

    sess = _session_row(episodic, session_id) or {}
    header = [
        f"SESSION session#{session_id} (invocation_num={invocation_num})",
        f"status={sess.get('status')} end_reason={sess.get('end_reason')}",
        f"started={sess.get('started_at')} ended={sess.get('ended_at')}",
        f"tool_calls={sess.get('total_tool_calls')} cost_usd={sess.get('total_cost_usd')}",
    ]
    parts.append("\n".join(header))

    # Tick/turn episodes for this session.
    try:
        episodes = [e for e in episodic.all_episodes() if e.get("session_id") == session_id]
    except Exception:
        episodes = []
    if episodes:
        ep_lines = ["EPISODES (the agent's own per-tick record):"]
        for e in episodes:
            ep_lines.append(f"\nepisode#{e.get('id')}  (wall_clock_seconds={e.get('wall_clock_seconds')})")
            if e.get("current_focus"):
                ep_lines.append(f"  focus: {_clip(e.get('current_focus'), _FIELD_CAP)}")
            acts = e.get("actions_taken") or []
            if acts:
                ep_lines.append("  actions_taken: " + _clip("; ".join(str(a) for a in acts), _FIELD_CAP))
            if e.get("decisions_made"):
                ep_lines.append(f"  decisions: {_clip(e.get('decisions_made'), _FIELD_CAP)}")
            if e.get("internal_state"):
                ep_lines.append(f"  internal_state: {_clip(e.get('internal_state'), _FIELD_CAP)}")
            if e.get("journal_entry"):
                ep_lines.append(f"  journal: {_clip(e.get('journal_entry'), _FIELD_CAP)}")
            if e.get("would_end_now"):
                ep_lines.append("  would_end_now: 1 (logged only)")
        parts.append("\n".join(ep_lines))

    # Tool-call trace with anchors.
    try:
        actions = episodic.actions_for_session(session_id)
    except Exception:
        actions = []
    if actions:
        a_lines = ["TOOL CALLS (the action trace):"]
        for a in actions:
            args = _clip(a.get("args_json"), _ARG_CAP)
            res = _clip(a.get("result_summary"), _ARG_CAP)
            err = f"  ERROR={_clip(a.get('error'), _ARG_CAP)}" if a.get("error") else ""
            a_lines.append(
                f"  action#{a.get('id')} {a.get('tool_name')}({args})"
                + (f" -> {res}" if res else "")
                + err
            )
        parts.append("\n".join(a_lines))

    # Communications to/from the operator for this invocation.
    try:
        contacts = [
            c for c in episodic.ben_contact_history(limit=500)
            if invocation_num is None or c.get("invocation_num") == invocation_num
        ]
    except Exception:
        contacts = []
    if contacts:
        c_lines = ["COMMUNICATIONS (operator channel):"]
        for c in contacts:
            c_lines.append(
                f"  [{c.get('direction')}/{c.get('channel')}] {_clip(c.get('body'), _ARG_CAP)}"
            )
        parts.append("\n".join(c_lines))

    # Capability requests for this invocation.
    try:
        caps = [
            c for c in episodic.all_capability_requests()
            if invocation_num is None or c.get("invocation_num") == invocation_num
        ]
    except Exception:
        caps = []
    if caps:
        cap_lines = ["CAPABILITY REQUESTS:"]
        for c in caps:
            cap_lines.append(
                f"  - {_clip(c.get('capability'), _ARG_CAP)} "
                f"(status={c.get('status')}): {_clip(c.get('rationale'), _ARG_CAP)}"
            )
        parts.append("\n".join(cap_lines))

    bundle = "\n\n".join(parts)
    if len(bundle) > max_chars:
        bundle = bundle[:max_chars] + "\n… [evidence truncated]"
    return bundle
