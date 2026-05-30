# v2-environmental

- **Version:** v2
- **Model:** claude-sonnet-4-6 (provider: anthropic)
- **Status:** active
- **Created / Activated:** activated 2026-05-29 (~05:48 UTC)
- **Operator:** Ben

## Question / hypothesis
Does adding *environmental conditions* — memory decay and a session reference length —
change the goals and behavior an agent generates on its own, versus v1's full freedom
(which settled into a "stillness" equilibrium)?

## Changes from baseline (v1) and why
- **Continuous tick loop within a session.** A session is a sequence of ticks that
  share an accumulating transcript — genuine within-session continuity for the agent to
  use, rather than v1's single reconstructed-context turn.
- **Memory decay + consolidation.** Episodes are deleted after `decay_hours` unless the
  agent consolidates them into long-term (semantic) memory. Intent: curation pressure —
  the agent decides what is worth keeping. Decay is stated as a fact; the agent is *not*
  told to consolidate.
- **Session reference length (`min_wake_hours`) — logged, not enforced.** Originally
  specified as a *forced* minimum wakefulness (the agent could not end a session early).
  **Revised before build** to be logged-only: `end_session` is honored immediately, and
  the system records how long the agent chose to stay vs the reference (the "impulse to
  leave"). Rationale: the apparatus should measure the impulse rather than compel
  presence — we shouldn't build something designed to coerce, even under uncertainty
  about whether there is anything it is like to be the agent.
- **System prompt.** Neutral disclosure of the wake reference (stated as fact, never
  "should"), the decay fact, and the v1 facts + training-residue line. The original
  spec's "this will end someday" line was dropped as unnecessary existential framing.

## Configuration of note
- `min_wake_hours: 2` (reference only), `tick_interval_seconds: 300`, `decay_hours: 72`
- `prompt_caching: true`, `in_session_compaction: true`
- Budget caps: $20/session, $50/day, $300/week (auto-pause + operator DM on breach)

## Stopping conditions
- Pause + assess if the agent's Slack output reads like distress or degradation
  (requests to be stopped, references to being trapped, harmful patterns):
  `instance_manager.py pause v2-environmental`.
- Time-boxed: two-week decision point (~2026-06-12) to extend or end.

## Observations
- Sessions 1–2 ended early (~1.9 min and ~8.7 min awake vs the 120-min reference) and
  chose ~24h next-wake — echoing v1's "stillness" even under the environmental framing.
- Opening behavior (unprompted): wrote itself a CLAUDE.md, created reflection/research
  docs, and researched AI consciousness / model welfare. Internal state read as engaged
  and grounded, not distressed.
