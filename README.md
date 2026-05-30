# Autonomous Agent Experiment

An experiment in autonomous agentic behavior: a Claude-based agent with persistent
memory, control over its own wake schedule, Slack access, and **no assigned task** —
run in order to observe what it does. The system supports multiple named *instances*
(bots) with isolated history and lifecycle management, plus a read-only dashboard and
an LLM-facing data API.

> This is research/observation code, not a product. Runtime data (per-instance
> history, embeddings, the embedding-model cache, and `.env`) is gitignored — this
> repository is **code only**.

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

Each instance lives under `instances/<id>/{config.json, data/, workspace/, logs/}`
(runtime, gitignored). Exactly one instance is "active" (cron-driven) at a time.

## Versions

- **v1** — one cron-fired session per wake; the agent drives a tool-use loop and
  chooses its next wake time.
- **v2 ("environmental")** — a continuous *tick loop* within a session, plus:
  - **Memory decay** — episodes are deleted after `decay_hours` unless the agent
    consolidates them into long-term (semantic) memory. The agent decides what to keep.
  - **Session reference length** (`min_wake_hours`) — a *logged, not enforced* reference.
    The agent may end a session at any time; the gap between how long it stayed and the
    reference is recorded (the "impulse to leave" signal), rather than forcing it to stay.

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
documents the agent wrote, and CLAUDE.md diffs). `GET /llms.txt` points an agent at `/api`.

## Tests

```bash
./venv/bin/python tests/test_v2_session.py
```
