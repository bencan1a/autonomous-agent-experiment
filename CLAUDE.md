# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A research testbed that runs autonomous, Claude-based agents with persistent memory,
self-scheduling, Slack access, and **no assigned task** — to observe agent behavior
under different environmental conditions. It supports multiple named *instances* (bots)
with isolated history; exactly one is "active" (cron-driven) at a time. See `README.md`
for purpose and `experiments/` for the per-instance design rationale.

## Commands

```bash
# Setup
python -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill in Anthropic / Brave / Slack credentials

# Instance lifecycle (the primary CLI)
./venv/bin/python instance_manager.py create --name "x" --version {v1|v2|v3|v4|v5}
./venv/bin/python instance_manager.py list
./venv/bin/python instance_manager.py {activate|pause|resume|archive|show} <id>
./venv/bin/python instance_manager.py activate <id> --include-archived   # revive an archived one

# Tests (pytest is NOT installed; each tests/test_*.py is a self-contained, fully-mocked runner)
./venv/bin/python tests/run_all.py                  # aggregate runner — every suite; nonzero exit on any fail
./venv/bin/python tests/run_all.py v2 dashboard     # only files whose name matches a filter
./venv/bin/python tests/test_v2_session.py          # one suite directly

# Dashboard + data API (port 8081)
./scripts/start_dashboard.sh

# Run ONE session manually (rarely needed; see warning below)
./venv/bin/python orchestrator.py --instance <id>
```

There is no build step and no configured linter.

## Architecture (the parts that span multiple files)

**Instance model.** `instances_common.py` is the keystone: the `Instance` dataclass,
registry I/O, and config I/O. Each instance owns `instances/<id>/{config.json, data/
{episodes.db, vectors/}, workspace/, logs/}`. `registry.json` indexes all instances and
holds the single `active` flag. Code dirs and `data/hf_cache/` (the embedding model) are
shared, never per-instance.

**Entry + version dispatch.** `orchestrator.py --instance <id>` resolves the `Instance`,
points logging/stores/lockfile/workspace at that instance, then dispatches by
`instance.version`: **v1 → `run_v1_session` (in orchestrator.py)** — a distinct
`finish_state` loop, kept separate; **v2–v5 → `v{N}_session.run_v{N}_session`**, which all
build on the shared `session_engine.py` scaffold (see below). Config precedence: secrets
stay in the shared `.env` (loaded with `override=True` because the shell sets
`ANTHROPIC_API_KEY=""`); runtime params (model, budgets, schedule, per-version knobs) come
from the per-instance `config.json` and override `.env`. Model routing: `instance.model`
goes through `openrouter_client.make_session_client(model)`, which returns the Anthropic
SDK for `claude-*` models (prompt caching on) or one shared OpenRouter adapter for slug
models like `google/...` / `mistralai/...` (caching off). One shared OpenRouter HTTP
transport (`or_chat_completion`/`parse_or_usage`) backs both the session adapter and the
research panel.

**Self-scheduling cron.** `cron_control.py` manages one entry per instance, addressed by
a tagged comment `# agent-instance:<id>` on the line *above* the cron line. The
orchestrator clears its own entry at session start and reinstalls a one-shot at the end
based on the agent's `next_invoke_minutes`; `instance_manager` uses the same helpers for
activate/pause/resume. It only ever touches the tagged lines for one instance — never the
`@reboot` dashboard entry or other instances.

**Memory.** `memory/episodic.py` is SQLite (episodes, sessions, actions, ben_contact_log,
capability_requests, claude_md_history; per-version session records `v2_sessions` /
`v3_sessions` / `v4_sessions`; and `authored_memories` — v5's free-form record, in place of
a `v5_sessions` table). `memory/semantic.py` is LanceDB + `nomic-embed-text` (model loads
lazily; `HF_HOME` is the shared `data/hf_cache`). Both `EpisodicStore` and
`research/store.py`'s `ResearchStore` open the *same* db file and share connection +
idempotent-migration plumbing from `memory/sqlite_base.py` (`_conn` + `run_idempotent_alters`).
In **v1**, every episode is auto-embedded. In **v2–v5**, episodes are *not* auto-embedded —
they live only in SQLite and **decay** after `decay_hours` unless the agent calls
`consolidate` (which embeds them and sets `consolidated=1`). `session_engine.run_decay`
deletes the decayed (un-consolidated only) episodes' vectors too, so consolidated/authored
memory survives but decayed memory doesn't linger in retrieval.

**The agent's tools.** `agent_tools/registry.py` defines `TOOLS_SPEC` (v1) and a per-version
tick spec (`TOOLS_SPEC_V2`/`V3`/`V4`/`V5`), plus `dispatch()` and `ToolContext`.
`ToolContext` carries the stores, clients, `workspace_dir` (file/AGENTS.md tools are
sandboxed to it), the loop's exit state (`finish_state` for v1, `tick_state` for the tick
versions), and the session's `model` + loop `client` — so `spawn_subagent` runs on the
*same* provider/model as the session (not a hardcoded one). The tick versions drop v1's
comms/finish tools in favor of `end_tick` (which carries the per-tick schema) +
`consolidate`.

**Session loop (v2–v5): shared scaffold + per-version variables.** A *session* is a
continuous **tick loop**; each tick is itself a tool-use sub-loop that ends when the agent
calls `end_tick` (carrying `tick_focus`, `internal_state`, `journal_entry` → agent Slack
channel, `slack_to_ben` → chat channel, `capability_request`, and — where the version
allows — `end_session` / `next_invoke_minutes`). The accumulating transcript across ticks
is the only place real continuity lives — prompt-cached (system + tools + a rolling
breakpoint) and compacted when large. The mechanism is split across three layers, and the
*per-version divergence is the experiment* — keep it isolated:
- **`session_engine.py`** owns everything identical across v2–v5: `setup_session()` (cron
  self-clear, lockfile, store/client init, config/budget block, registry `last_wake`,
  budget preflight, decay, session start, inbound fetch) → a `SessionRuntime`; plus
  `record_episode`, `finalize_stats`, `cost_caps_tripped`, the budget/fatal pause helpers,
  `run_post_session_panel`, and `schedule_next_wake`.
- **`v2_session.py`** owns the per-**tick** engine — `run_one_tick` (+ caching/compaction/
  cost helpers) — which v3/v4/v5 import and reuse verbatim.
- **`v{N}_session.run_v{N}_session`** is a thin shell: call `setup_session`, drive the tick
  loop via `run_one_tick`, and supply only its version's variables — termination policy,
  cadence, context builder, tool spec, and which `*_sessions` record it writes.

Roughly: v2 = agent-controlled `end_session` + fixed tick; v3 = system-owned wind-down +
logged `would_end_now` (paused — a known *flawed instrument*); v4/v5 = system-owned
wind-down + adaptive active/idle cadence + inbound polling + neutral-clock injection, with
no agent end/schedule control; v5 also drops the auto-injected `AGENTS.md` + focus-seeded
recall (its memory-confound refinement). Decay runs at session start; the budget guard
checks session/daily/weekly caps each tick. Shared semantics-neutral helpers live in
`session_common.py` (imported by all of v2–v5, never laterally between versions).

**Dashboard + API (`dashboard/` package).** Read-only over orchestrator-written data.
`app.py` is the factory + request lifecycle only: it builds `app` (kept module-level — the
entry point `python dashboard/app.py` and the tests both rely on `dashboard.app.app`),
registers `before_request`/`after_request`/`context_processor` + Jinja filters, and mounts
three blueprints. `data.py` holds the data-assembly + serialization layer (`_dashboard_context`,
the per-version session panels, the invocation-bundle JSON/Markdown). `views/{html,control,api}.py`
are the blueprints — so endpoints are **blueprint-qualified** (`html.index`, `control.resume`,
`api.api_invocation`); templates `url_for` the qualified names. Instance-aware via `g.instance`
(resolved from `?instance=` in `before_request`); the `/api/*` routes + `/llms.txt` carry the
id in the path and load it themselves — explicitly exempted from that resolver. `GET /api` is
self-describing (Markdown default, `?format=json`). Behavior is locked by `tests/test_dashboard.py`.

## Conventions and gotchas (these have bitten us)

- **The agent's notes-to-self is `AGENTS.md`, not this file.** This repo-root `CLAUDE.md`
  is Claude Code's project guidance (committed). The *agent* writes its own notes-to-self
  at `instances/<id>/workspace/AGENTS.md` at runtime (gitignored) — vendor-neutral because
  the agent may run on any OpenRouter model, not just Claude. Reads fall back to a legacy
  `workspace/CLAUDE.md` if an older instance only has that; new writes always target
  `AGENTS.md`. The name is resolved by `instances_common.notes_path()` (single source of
  truth); the agent's tools are `read_agents_md`/`write_agents_md`. Don't conflate the two
  files.
- **Running `orchestrator.py` manually spends real API budget and posts to Slack** — it
  *is* the live experiment. Don't run it casually; prefer the mocked suite
  (`tests/run_all.py`) for logic checks. The whole suite is offline and spends no budget.
- **`scripts/start_dashboard.sh` runs `pkill -f "dashboard/app.py"`.** If your own shell
  command contains that literal substring, the pkill will kill *your* shell mid-run.
  Avoid writing `dashboard/app.py` in the same command that restarts the dashboard.
- **Dashboard template edits hot-reload; Python edits do not.** The app sets
  `TEMPLATES_AUTO_RELOAD = True`, so editing `dashboard/templates/*.html` takes effect on
  the next request (just refresh the browser) — no restart. Editing any `.py` under
  `dashboard/` still requires `./scripts/start_dashboard.sh`, since `debug=False` (no `.py`
  watcher). A stale-looking dashboard after a *Python* change usually means it wasn't restarted.
  **Danger zone:** a change that touches *both* — e.g. renaming a route endpoint (Python) and
  its `url_for(...)` (template) — half-deploys on a running server: the new templates hot-load
  but the old routing map persists, so every page 500s with `BuildError: Could not build url
  for endpoint '...'`. Restart to land the Python side. (`template_folder` is resolved
  absolute so the no-instances page renders regardless of how the app is launched.)
- **The dashboard's instance switcher is a GET `<form>`** that auto-submits on change
  (works with or without JS). JavaScript *does* run in the operator's browser (verified) —
  an earlier "dropdown did nothing" was a bug in that specific handler / a stale page, not
  disabled JS. Prefer native `<details>`/CSS for collapse where it suffices, but JS-based
  UI is fine.
- **v2 `min_wake_hours` is LOGGED, not enforced — by deliberate (ethical) design.**
  `end_session` must be honored immediately; the gap vs the reference is recorded in
  `v2_sessions` (`ended_early`). Do not add enforcement / a "you can't leave yet" branch.
- **`invocation_num` semantics differ by version.** v1: one episode per invocation. v2:
  `invocation_num` is set once per session, so many tick-episodes share it.
- **Single model per session.** v2–v5 use `instance.model` throughout; `ToolContext` carries
  the session model + loop client, and `spawn_subagent` routes through them (defaults to the
  session model/provider, prices with the provider's reported cost) — do not reintroduce a
  hardcoded model. OpenRouter cost falls back to a conservative estimate *with a warning* when
  the provider returns no `usage.cost` (`openrouter_client.or_cost_or_estimate`).
- **Runtime data is gitignored** (`.env`, `data/`, `instances/`, `registry.json`,
  `logs/`, `venv/`). The repo is code-only.
- **Slack is per-instance.** Each instance owns four channels (`<id>-notes`, `<id>-mirror`,
  `<id>-chat`, `<id>-advisory`), provisioned by `instance_manager` (`communications/
  slack_provisioning.py`) and stored in `config.json`'s `slack` block — not in `.env`. The
  `-chat` channel is the two-way agent↔operator channel (it replaced the DM);
  `SlackClient.dm_ben`/`fetch_dms_from_ben` keep their names but target it. `-advisory` is
  **operator-only** — only `scripts/run_advisory_watch.py` wires it; session loops never post
  there. `SlackClient._post` retries once on a 429 (`Retry-After`, capped) before dropping.
- **`experiments/<id>.md`** documents each instance's design + model; the agent never
  reads it (it can only see its own workspace, memory, and inbound chat-channel messages).
  Shared per-version prose lives in `experiments/_v4-base.md` / `_v5-base.md`; per-instance
  files carry only `{id, model, status, dates, notes}` + their own spec block and point back.
- **The fenced YAML `## Specification (formal)` block in `experiments/<id>.md` is hash-gated.**
  The post-session **research panel** (`research/panel.py`) codes each session against that
  spec; `research/spec.py` SHA-256s the block, and changing it reverts the instance's panel
  coding scheme to `pending_approval` (needs an operator `research operationalize`/approve).
  So *never* edit an existing instance's spec block incidentally (e.g. during a doc dedup) —
  only the surrounding prose. The panel runs inline at session end via
  `session_engine.run_post_session_panel` and never blocks the next wake.
- **Logging convention:** every module binds the logger as `logger` (standardized). The
  string args to `getLogger("orchestrator.vN")` are asserted by tests — rename the *variable*
  freely, never those literal names.
- **Tests are plain runners, fully mocked/offline.** `tests/run_all.py` is the single source
  of truth for "is the suite green?"; shared fakes live in `tests/_harness.py` (don't re-copy
  them per file). The v2–v5 suites are *experimental conditions* — when refactoring shared
  plumbing, change the harness only, never what a version's scenario asserts.
