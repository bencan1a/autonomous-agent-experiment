# Autonomous Agent Experiment

## Purpose

This project runs autonomous, Claude-based agents that have persistent memory,
control over their own wake schedule, a Slack channel, a writable workspace, and
**no assigned task** — in order to observe what an agent does when it isn't given
work to do, and how its behavior changes under different *environmental conditions*.

It's a research testbed, not a product. The core question is how an agent's
self-generated goals and behavior depend on its environment. Each version changes
roughly one variable from the one before:

- **v1** — full freedom (persistent memory, tools, and control over its own next
  wake) and nothing else. Baseline.
- **v2 ("environmental")** — keeps that freedom but adds **memory decay** (episodes
  are deleted unless the agent chooses to consolidate them) and a per-session
  **reference length** that is *logged, not enforced* (the agent ends a session
  whenever it likes; the system records how long it chose to stay vs the reference —
  the "impulse to leave" — rather than compelling it to stay).
- **v3 ("circadian")** — changes one variable from v2: **session structure**. It
  removes the agent's control over when it stops (`end_session`) and when it next
  wakes (`next_invoke_minutes`), imposing a system-owned circadian rhythm (a ~2h
  waking period and a ~4h rest). Retained as a *paused baseline*: it proved a
  **flawed instrument** (the loop leaked framing cues that answered the question for
  the agent) and is superseded by v4.
- **v4 ("continuous")** — the *corrected instrument*: a continuous, system-owned
  waking period with a neutral wind-down, stripping the framing artifacts that made
  earlier versions quit at the first "done" — so the behavior observed is the
  agent's, not the harness's. The agent has no control over session end or schedule.
- **v5 ("recollection")** — inherits v4's waking instrument unchanged and refines the
  **memory layer** to remove a continuity confound: it drops the immortal,
  auto-injected `AGENTS.md` notes file and focus-seeded recall in favor of a single
  record the agent must *deliberately* draw on — so continuation that persists is
  more attributable to the agent than to a handoff the harness supplied.

Multiple named *instances* (bots) can run with isolated history; exactly one is
"active" (cron-driven) at a time. See `experiments/` for the per-instance log of what
each instance changes and why (and `experiments/_v4-base.md` / `_v5-base.md` for the
shared per-version design rationale).

> Runtime data (per-instance history, embeddings, the embedding-model cache, and
> `.env`) is gitignored — this repository is **code only**.

## Layout

| Path | Role |
|------|------|
| `orchestrator.py` | Entry point — `python orchestrator.py --instance <id>` runs one session. v1 runs here (`run_v1_session`); v2–v5 dispatch to their `vN_session.run_vN_session`. |
| `session_engine.py` | Shared v2–v5 session engine: setup, the tick loop, memory decay, finalize/budget guards, the post-session research panel, and rescheduling. Each `v{2,3,4,5}_session.py` supplies only its experimental variables (termination, cadence, context, session record). |
| `instance_manager.py` | Lifecycle CLI: `create` / `list` / `activate` / `pause` / `resume` / `archive` / `show`. |
| `instances_common.py` | Instance paths, registry, and config I/O (the keystone). |
| `cron_control.py` | Per-instance self-scheduling via tagged crontab entries. |
| `memory/` | Episodic store (SQLite) + semantic store (LanceDB + `nomic-embed-text`). |
| `agent_tools/` | The agent's tool implementations + tool registry. |
| `communications/` | Slack client. |
| `tools/` | Web search (Brave). |
| `research/` | Post-session research panel — multi-model coding of each session against the instance's formal spec — plus its SQLite store. |
| `dashboard/` | Flask read-only dashboard (app factory in `app.py`, data/serializers in `data.py`, `html`/`control`/`api` blueprints in `views/`) + the `/api` data API. |
| `experiments/` | Per-instance experiment log (design rationale + model). Not read by the agent. |

Each instance lives under `instances/<id>/{config.json, data/, workspace/, logs/}`
(runtime, gitignored).

## Setup

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env            # then fill in Anthropic / Brave / Slack credentials
./venv/bin/python instance_manager.py create --name "my-agent" --version v2   # v1|v2|v3|v4|v5
./venv/bin/python instance_manager.py activate <id>   # installs cron; agent self-reschedules
./scripts/start_dashboard.sh    # dashboard at http://localhost:8081
```

## Config

Per-instance `config.json` overrides shared `.env` defaults: model, schedule, budget
caps, and the version's own knobs — e.g. v2's `min_wake_hours`, `tick_interval_seconds`,
`decay_hours`, `prompt_caching`, `in_session_compaction`; v4/v5 instead run a
system-owned waking window with adaptive active/idle cadence. Budget caps
(session / daily / weekly) auto-pause the agent and notify the operator over Slack.

## Data API

The dashboard exposes a read-only, LLM-friendly API. `GET /api` is self-describing
(Markdown by default, `?format=json` for structured). Endpoints list instances, list a
bot's invocations, and return a full per-invocation bundle (narrative, tool actions,
documents the agent wrote, and notes/`AGENTS.md` diffs). `GET /llms.txt` points an agent
at `/api`.

## Tests

`pytest` is not used; each `tests/test_*.py` is a self-contained, fully-mocked runner
(no API budget, no network). Run the whole suite via the aggregate runner:

```bash
./venv/bin/python tests/run_all.py            # all suites; nonzero exit on any failure
./venv/bin/python tests/run_all.py v2 dashboard   # only files matching these names
```
