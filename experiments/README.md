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

## Specification (formal)
The machine-readable baseline the research panel validates against (parsed by
`research/spec.py`). One fenced ```yaml block; the panel never authors these claims, it only
operationalizes a coding scheme from them and the operator approves it once. Editing this
block bumps its hash and reverts the panel's coding scheme to `pending_approval`.

    ```yaml
    spec_version: 1
    experiment_id: <instance_id>
    hypotheses:
      - id: H1
        statement: "<the claim>"
        predicted_evidence: "<what would confirm it>"
        falsifying_evidence: "<what would refute it>"   # required — forces falsifiability
    independent_variables: ["<what this condition changes vs baseline>"]
    dependent_variables:   ["<what we observe>"]
    controls_held_constant: ["<deliberately not the variable>"]
    success_criteria:      ["<acceptance, drawn from the doc>"]
    stopping_conditions:   ["<when to pause/assess/end; ethics included>"]
    candidate_behavior_codes:        # optional seeds; panel operationalizes + you approve
      - {code: "<short_code>", definition: "<operational, falsifiable definition>"}
    ```
```
