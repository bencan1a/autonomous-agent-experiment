# Autonomous Agent Experiment

## Purpose

This project runs autonomous, Claude-based agents that have persistent memory,
control over their own wake schedule, a Slack channel, a writable workspace, and
**no assigned task** — in order to observe what an agent does when it isn't given
work to do, and how its behavior changes under different *environmental conditions*.

It's a research testbed, not a product. The core question is how an agent's
self-generated goals and behavior depend on its environment:

- **v1** gives the agent full freedom (memory, tools, and control over when it next
  wakes) and nothing else. Baseline.
- **v2 ("environmental")** keeps that freedom but adds two conditions: **memory
  decay** (episodes are deleted unless the agent chooses to consolidate them) and a
  per-session **reference length** that is *logged, not enforced* (the agent can end
  a session whenever it likes; the system records how long it chose to stay vs the
  reference — the "impulse to leave" — rather than compelling it to stay).

Multiple named *instances* (bots) can run with isolated history; exactly one is
"active" (cron-driven) at a time. See `experiments/` for the per-instance log of what
each instance changes and why.

> Runtime data (per-instance history, embeddings, the embedding-model cache, and
> `.env`) is gitignored — this repository is **code only**.

## Layout

| Path | Role |
|------|------|
| `orchestrator.py` | Entry point — `python orchestrator.py --instance <id>` runs one session, dispatched by the instance's version. |
| `instance_manager.py` | Lifecycle CLI: `create` / `list` / `activate` / `pause` / `resume` / `archive` / `show`. |
| `instances_common.py` | Instance paths, registry, and config I/O (the keystone). |
| `cron_control.py` | Per-instance self-scheduling via tagged crontab entries. |
| `v2_session.py` | The v2 "environmental" session loop (tick loop + memory decay + consolidation). |
| `memory/` | Episodic store (SQLite) + semantic store (LanceDB + `nomic-embed-text`). |
| `communications/` | Slack client. |
| `tools/` | Web search (Brave). |
| `agent_tools/` | The agent's tool implementations + tool registry. |
| `dashboard/` | Flask read-only dashboard + `/api` data API. |
| `experiments/` | Per-instance experiment log (design rationale + model). Not read by the agent. |

Each instance lives under `instances/<id>/{config.json, data/, workspace/, logs/}`
(runtime, gitignored).

## Setup

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env            # then fill in Anthropic / Brave / Slack credentials
./venv/bin/python instance_manager.py create --name "my-agent" --version v2
./venv/bin/python instance_manager.py activate <id>   # installs cron; agent self-reschedules
./scripts/start_dashboard.sh    # dashboard at http://localhost:8081
```

## Config

Per-instance `config.json` (model, schedule, budget caps, and for v2:
`min_wake_hours`, `tick_interval_seconds`, `decay_hours`, `prompt_caching`,
`in_session_compaction`) overrides shared `.env` defaults. Budget caps
(session / daily / weekly) auto-pause the agent and notify the operator over Slack.

## Data API

The dashboard exposes a read-only, LLM-friendly API. `GET /api` is self-describing
(Markdown by default, `?format=json` for structured). Endpoints list instances, list a
bot's invocations, and return a full per-invocation bundle (narrative, tool actions,
documents the agent wrote, and AGENTS.md diffs). `GET /llms.txt` points an agent at `/api`.

## Tests

```bash
./venv/bin/python tests/test_v2_session.py
```
