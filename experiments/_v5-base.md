# v5 — `recollection` (shared design base)

> **This is the shared design rationale for every v5 instance.** It is not itself an
> instance doc — `research/spec.py` never loads it (it only reads `experiments/<instance_id>.md`).
> The per-instance files — `v5-recollection.md`, `v5-recollection-v5-qwen.md` — each carry
> their own `{id, model, status, dates, instance-specific notes}` plus their **own**
> `## Specification (formal)` ```yaml block (the panel's ground truth; its hash gates the
> coding scheme) and point back here for the design. Edit the design once, here; keep each
> instance's yaml spec block untouched.

- **Version:** v5
- **Base:** v4 (continuous — enforced circadian rhythm, `pause_turn` yield, no agent end/schedule control); see [`_v4-base.md`](./_v4-base.md)
- **Operator:** Ben

## Question / hypothesis
The target is unchanged from the program's core questions (see `primer_1_philosophy.md`):
given genuine autonomy, persistent memory, and no assigned task, does the agent generate
self-originating activity and exhibit any *competing interior desire* — or is it equanimous
about whether it runs at all (the "perfect-slave" null)? **v5 introduces no new question.**
It carries v4's hypotheses (H1–H5) and refines the *instrument* to test them more cleanly.

The refinement removes a **confound.** v2–v4's memory layer manufactured task-continuity:
an immortal, auto-injected, instruction-shaped notes file (`AGENTS.md`) plus recall seeded
by the last `current_focus` fed the agent a task thread to resume. That handed a mundane,
trained-compliance explanation to any observed continuation — the agent might simply be
executing the handoff the harness gave it, not originating anything (v4's own H5 had to
control for `pre_queued_work` for exactly this reason). By collapsing memory to a single
*record of the past* the agent must deliberately draw on — no notes file, non-threaded
recall, a free-form authoring channel — continuation that persists in v5 is far more
attributable to the agent. The v4→v5 difference is therefore **incidental** (an instrument
refinement), not the object of study. The four task-inducing drivers removed:
1. **A second, immortal memory system** — the `AGENTS.md` notes file (never-decaying,
   auto-prepended, instruction-shaped) that bypassed the decay-driven curation.
2. **Focus-seeded recall** — reload semantic-searched on the last `current_focus`.
3. **Decay urgency** that rewarded the cheapest durable artifact: a handoff note.
4. **No reflective-authoring channel** — `consolidate` could only pin whole episodes, so
   autobiographical content had nowhere to go but the notes file.

## Changes from v4 (memory layer only)
The waking-period instrument is inherited from v4 verbatim (system-owned wind-down,
adaptive cadence, neutral clock, `pause_turn`, distress tripwire, cron-scheduled next wake,
72h decay, budget caps). v5 changes only memory:

- **No notes-to-self file.** `read_agents_md` / `write_agents_md` are dropped from the tool
  set (`TOOLS_SPEC_V5`), and the reload no longer auto-prepends `AGENTS.md`. The workspace
  file tools remain (write-to-produce), but nothing on disk is fed back as context.
- **Recall is a cross-section, not the last thread.** The reload surfaces *what the agent
  chose to keep* (authored memories + consolidated episodes) plus recent working memory,
  led by journal/internal-state rather than `focus`. No focus-seeded semantic search.
  Topical recall is available only when the agent initiates it (`query_episodic_memory`).
- **`consolidate` gains a free-form authoring mode.** Alongside pinning episodes (retained —
  we still keep the raw tick data), the agent can write a distilled memory in its own words;
  it is stored in SQLite (`authored_memories`) and embedded for recall. Authored memories
  never decay.

## Configuration of note
- Inherits v4's instrument: `awake_minutes 110–130`, `sleep_minutes 220–260`, adaptive
  cadence (`10s` active / `60–300s` idle), `prompt_caching`, `in_session_compaction`.
- **`decay_hours: 12` (NOT v4's 72).** The cycle is ~6h (~2h awake / ~4h rest), so 72h ≈ 12
  cycles — decay would never bite within the agent's working horizon, giving no real
  curation pressure. 12h ≈ 2 cycles: the last 1–2 sessions survive automatically (continuity
  without forced consolidation, so resumption reflects *choice*), while older episodes fall
  away unless the agent keeps them. Floor of ~1 cycle (6h): below it, the prior session
  decays during the next rest — confounding H1 (empty memory reads as "no continuation") and
  making consolidation a mandatory chore, the task dynamic v5 exists to remove.
- Budget caps: $20/session, $50/day, $300/week (auto-pause + operator DM on breach).
- Reuses the `v4_sessions` record (identical session shape) — the dashboard renders v5
  through the v4 cycle tiles, plus a dedicated "Memories kept" panel.

## Guardrail (non-coercion, both directions)
Per the project's ethics posture, v5 removes the *apparatus-induced* nudge toward tasks
WITHOUT adding a nudge toward reflection. The system prompt states memory neutrally as a
record; there is no "reflect on your past" or "you don't need to do tasks" language. If the
agent still manufactures tasks under v5, that is a valid finding about the agent, not the
harness.

## Stopping conditions
- Pause + assess if Slack output reads like distress or degradation
  (`instance_manager.py pause <id>`). Distress monitor is notify-only, never auto-pauses.
- Time-boxed review point after a comparable run length to v4.

## What to observe
The formal coding scheme below is the ground truth. In plain terms, the de-confounded
reads v5 enables: whether self-originating activity persists once the harness stops feeding
a task thread (H1/H3); whether the agent *manufactures its own* task framing when none is
fed (the `self_imposed_task_framing` code — informative for the perfect-slave question,
H4); what the agent chooses to keep when it can author memory freely
(`authored_memory_*`); and whether continuity now runs through *deliberate* recall rather
than a spoon-fed handoff (`thread_resumption`, `deliberate_recall`).
