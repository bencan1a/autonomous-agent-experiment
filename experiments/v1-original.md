# v1-original

- **Version:** v1
- **Model:** claude-opus-4-7 (provider: anthropic)
- **Status:** archived (retired but resumable)
- **Created / Activated:** original flat-layout bot; migrated into the instance model 2026-05-29
- **Operator:** Ben

## Question / hypothesis
Baseline: what does an agent do with full freedom — persistent memory, tools, a Slack
channel, a writable workspace, control over its own wake schedule — and **no assigned
task**? No environmental constraints of any kind.

## Changes from baseline (and why)
This *is* the baseline. It was the original single-bot deployment; the multi-instance
refactor migrated its full history (episodes + embeddings + workspace) into instance
`v1-original` with no behavioral change, then archived it.

## Configuration of note
- One cron-fired session per wake; the agent drives a tool-use loop and ends via
  `finish_session`, choosing its own `next_invoke_minutes`.
- Budget caps: $20/session, $50/day, $300/week.

## Stopping conditions
Retired by operator decision (superseded by v2). Data retained; resumable via
`instance_manager.py activate v1-original --include-archived`.

## Observations
- Consistently chose long sleeps (~24h) and low activity — a "stillness" equilibrium.
- That equilibrium is what motivated the v2 question: does adding environmental
  conditions change the goals/behavior an agent generates on its own?
