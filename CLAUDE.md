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
./venv/bin/python instance_manager.py create --name "x" --version {v1|v2}
./venv/bin/python instance_manager.py list
./venv/bin/python instance_manager.py {activate|pause|resume|archive|show} <id>
./venv/bin/python instance_manager.py activate <id> --include-archived   # revive an archived one

# Tests (pytest is NOT installed; the test file is a plain runner)
./venv/bin/python tests/test_v2_session.py          # runs all scenarios, prints PASS/FAIL

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
`instance.version`: **v1 → `run_v1_session` (in orchestrator.py)**, **v2 →
`v2_session.run_v2_session`**. Config precedence: secrets stay in the shared `.env`
(loaded with `override=True` because the shell sets `ANTHROPIC_API_KEY=""`); runtime
params (model, budgets, schedule, v2 knobs) come from the per-instance `config.json` and
override `.env`.

**Self-scheduling cron.** `cron_control.py` manages one entry per instance, addressed by
a tagged comment `# agent-instance:<id>` on the line *above* the cron line. The
orchestrator clears its own entry at session start and reinstalls a one-shot at the end
based on the agent's `next_invoke_minutes`; `instance_manager` uses the same helpers for
activate/pause/resume. It only ever touches the tagged lines for one instance — never the
`@reboot` dashboard entry or other instances.

**Memory.** `memory/episodic.py` is SQLite (episodes, sessions, actions,
ben_contact_log, capability_requests, claude_md_history, and v2-only `v2_sessions`).
`memory/semantic.py` is LanceDB + `nomic-embed-text` (model loads lazily; `HF_HOME` is
set to the shared `data/hf_cache`). In **v1**, every episode is auto-embedded. In **v2**,
episodes are *not* auto-embedded — they live only in SQLite and **decay** after
`decay_hours` unless the agent calls the `consolidate` tool, which embeds them and sets
`consolidated=1`.

**The agent's tools.** `agent_tools/registry.py` defines `TOOLS_SPEC` (v1) and
`TOOLS_SPEC_V2`, plus `dispatch()` and `ToolContext`. `ToolContext` carries the stores,
clients, `workspace_dir` (file/AGENTS.md tools are sandboxed to it), and the loop's exit
state — `finish_state` for v1, `tick_state` for v2. v2's tool set drops v1's comms/finish
tools in favor of `end_tick` (which carries the per-tick schema) + `consolidate`.

**v2 session loop (`v2_session.py`).** A *session* is a continuous **tick loop**; each
tick is itself a tool-use sub-loop that ends when the agent calls `end_tick`. `end_tick`
carries `tick_focus`, `internal_state`, `journal_entry` (→ agent Slack channel),
`slack_to_ben` (→ DM), `capability_request`, `end_session`, and `next_invoke_minutes`.
The accumulating transcript across ticks is the only place real continuity exists — it is
prompt-cached (system + tools + a rolling breakpoint) and compacted when it grows large.
Decay runs at session start. The budget guard checks session/daily/weekly caps each tick.

**Dashboard + API (`dashboard/app.py`).** Instance-aware via `g.instance` (resolved from
`?instance=` in `before_request`). The `/api/*` routes carry the id in the path and call
`load_instance` themselves — they are explicitly exempted from the `before_request`
resolver. `GET /api` is self-describing (Markdown default, `?format=json`); `/llms.txt`
points an external agent at it.

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
  *is* the live experiment. Don't run it casually; prefer the mocked
  `tests/test_v2_session.py` for logic checks.
- **`scripts/start_dashboard.sh` runs `pkill -f "dashboard/app.py"`.** If your own shell
  command contains that literal substring, the pkill will kill *your* shell mid-run.
  Avoid writing `dashboard/app.py` in the same command that restarts the dashboard.
- **Dashboard template edits hot-reload; Python edits do not.** The app sets
  `TEMPLATES_AUTO_RELOAD = True`, so editing `dashboard/templates/*.html` takes effect on
  the next request (just refresh the browser) — no restart. Editing `dashboard/app.py` (or
  any `.py`) still requires `./scripts/start_dashboard.sh`, since `debug=False` (no `.py`
  watcher). A stale-looking dashboard after a *Python* change usually means it wasn't restarted.
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
- **Single model per session.** v2 uses `instance.model` throughout; `spawn_subagent`
  defaults to the same model.
- **Runtime data is gitignored** (`.env`, `data/`, `instances/`, `registry.json`,
  `logs/`, `venv/`). The repo is code-only.
- **Slack is per-instance.** Each instance owns three channels (`<id>-notes`,
  `<id>-mirror`, `<id>-chat`), provisioned by `instance_manager` (`communications/
  slack_provisioning.py`) and stored in `config.json`'s `slack` block — not in `.env`.
  The `-chat` channel is the two-way agent↔operator channel (it replaced the DM);
  `SlackClient.dm_ben`/`fetch_dms_from_ben` keep their names but target that channel.
- **`experiments/<id>.md`** documents each instance's design + model; the agent never
  reads it (it can only see its own workspace, memory, and inbound chat-channel messages).
