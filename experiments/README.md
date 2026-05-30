# Experiment Log

One file per instance (`<instance_id>.md`) recording **what that instance changes
relative to the baseline, and why** — the experimental design behind it. This is
operator/researcher documentation.

**The agent never sees these files.** The agent can only read its own workspace
(`instances/<id>/workspace/`), its memory, and inbound DMs — never the repo root, so
nothing here leaks into an agent's context. Keep design rationale, hypotheses, and
observations here, not in anything the agent reads.

Track the **model** per instance explicitly — it's an independent variable. If we ever
swap providers (e.g. a local Ollama model, DeepSeek, a different Claude tier), the
model field is how we keep comparisons honest.

## Template

```markdown
# <instance_id>

- **Version:** v1 | v2
- **Model:** <model id>  (provider: anthropic | ollama | deepseek | …)
- **Status:** active | paused | archived
- **Created / Activated:** <date>
- **Operator:** <who>

## Question / hypothesis
What this instance is meant to test.

## Changes from baseline (and why)
- Change → rationale.

## Configuration of note
Key config values that define this condition (e.g. min_wake_hours, decay_hours).

## Stopping conditions
When we pause/assess or end.

## Observations
Running notes on what actually happened.
```
