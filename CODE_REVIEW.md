# Codebase Review — Prioritized Punch List

**Date:** 2026-06-13  
**Scope:** all real Python (~21k LOC) + Flask dashboard + templates + research subsystem + tests. Excludes the leftover `.claude/worktrees/` mirror, `venv/`, and gitignored runtime data.  
**Method:** 12 parallel reviewers (10 per-component + 2 cross-cutting: duplication, consistency/dead-code) → one synthesis pass that deduped, resolved conflicts, and globally prioritized. Top correctness claims independently verified against live code.  
**Framing constraint:** v1–v5 are *intentional experimental conditions*. Any cross-version change must preserve each version's observable behavior — "dedupe the plumbing, freeze the science."

---

## Progress & decisions (updated 2026-06-13)

**Done & on `main`** (suite 14/14 green throughout):
- **P0-1** `session_common.py` + untangled the v3→v4→v5 lateral import chain.
- **P0-2** shared `session_engine.py` (setup/finalize/scheduling scaffold) — each `run_vN_session` is now a thin shell keeping only its experimental variables; `tests/test_session_engine.py` golden test added.
- **P0-3** aggregate `tests/run_all.py` + rewrote the dead `test_instance_control.py`.
- **P1-7** shared `tests/_harness.py`. · **P2-2** removed dead `ClaudeClient`. · **P3-1** dead-code sweep (deferred items below). · **P3-4 (partial)** gitignore `.claude/` + `requirements.lock.txt`.
- Bonus: `schedule_next_wake` now skips rescheduling a mid-session-paused instance (the orphan-cron fix).

**P0-2 also partially advanced:** P2-3 (the post-session research-panel hook is now `session_engine.run_post_session_panel` — only the script-bootstrap helper remains) · P2-6 (scheduling funnels through one `schedule_next_wake`) · P1-5 (canonical pause/cost helpers now live in `session_engine`).

**Operator decisions (2026-06-13):**
- **P1-4 → DO IT.** Semantic store should decay in lockstep with episodic: `run_decay` deletes the decayed episodes' vectors too. Constraint: `run_decay` only removes *un-consolidated* episodes, so consolidated/authored memory still survives (durable escape hatch preserved).
- **P2-7 → WON'T FIX.** Resume note stays v4/v5-only; leave v1/v2/v3 as-is.
- **`dm_ben` rename → WON'T FIX.** Keep the tool name and `channel='dm'` log label.
- **P1-2 → unify the OpenRouter side only.** Keep a distinct Anthropic path (Anthropic SDK for Anthropic models; one shared OpenRouter transport for OpenRouter models) behind `make_session_client(model)`.

**Deferred sub-items still open:** the episodic `resolve_capability_request` PRAGMA branch (P3-1) and the dashboard `_trunc` consolidation (folds into P1-3).

**Remaining tiers:** P1-1, P1-2, P1-3, P1-4, P1-5, P1-6 · P2-1, P2-3 (script bootstrap), P2-4, P2-5, P2-6 · P3-2, P3-3, P3-4 (experiment-doc dedup + v3 doc), P3-5.

---

## Executive summary

Overall the codebase is in markedly better shape than its "assembled by many independent agent sessions" origin would predict, but it carries the signature drift of that history: parallel scaffolds, copy-pasted helpers, and convention divergence that all concentrate at module seams. There is genuine evidence of at least one deliberate consolidation pass — the per-tick engine (`run_one_tick`, decay, compaction, caching, signal/pause helpers) lives in `v2_session.py` and is correctly *imported* (not copied) by v3/v4/v5, the instance run-state model has a single authority (`instance_control.apply_status` / `instances_common.registry_txn`), and the locking story is disciplined. That healthy core is the right model; the problem is almost everything *around* it has not been consolidated to the same standard.

The dominant through-line is the `run_vN_session` driver. Each of v2–v5 wraps the shared tick engine in a ~300–400 line orchestration scaffold whose preamble (cron self-clear, lockfile+atexit, store init, signal handler, Slack/Brave/Anthropic init, the OpenRouter branch, the config/budget param block, registry `last_wake`, budget preflight) and tail (post-session budget recheck, the verbatim research-panel block, reschedule) are byte-for-byte identical — v4 and v5 differ by roughly 44 lines of ~400, nearly all version-string labels. v1 (`run_v1_session` in orchestrator.py) is a genuinely distinct engine (finish_state loop, no ticks/decay/consolidation, no OpenRouter) and should stay separate, but it carries *stale copies* of helpers that the v2 family has since fixed — most consequentially `_budget_pause_and_notify`, which in v1 only clears cron and leaves the registry showing `active` (the exact bug v2's version documents fixing). The single highest-leverage move is to extract a shared v2–v5 session engine with explicit version hooks (context builder, tool spec, system prompt, termination policy, cadence policy, session-record writer), landed incrementally behind the existing per-version tests.

Beneath that headline sit several real cross-module clusters that the cross-cutting reviewers and component reviewers independently surfaced (and which I reconciled into single items): a `_turn_cost`/`_usage` pricing helper duplicated between v1 and v2 (v1 lacks OpenRouter actual-cost); a content-block text-extraction one-liner in five files; the SQLite `_conn` + idempotent-ALTER idiom duplicated between `memory/episodic.py` and `research/store.py` (both opening the *same* DB file); **two-to-three** independent OpenRouter HTTP clients (`openrouter_client.py`, `research/providers.OpenRouterProvider`) that have already drifted on retry/`response_format`/cost-accumulation; four near-identical `_tools_for_call_vN` spec helpers; the `_execute_side_effects`/`_distress_check` helpers redefined in v3 and then imported by v4/v5 *from v3* rather than from v2's canonical copy; and the dead `ClaudeClient`/`ClaudeResult` class that makes `claude_client.py` masquerade as a client when it is really a pricing+JSON-extraction utility module. The dashboard (`dashboard/app.py`, 2589 lines) is a second concentration point: a ~412-line `_dashboard_context` god-function with triplicated per-version summary blocks and a two-format API serializer (`_bundle_json`/`_bundle_markdown`) that re-projects the same bundle twice.

There are two correctness-flavored risks worth treating as more than hygiene. First, `spawn_subagent` hardcodes `claude-sonnet-4-6` and always routes through `ctx.anthropic`, even on OpenRouter-backed instances — `ToolContext` carries no session model — so the subagent silently runs a different provider/model and is mis-priced, contradicting the documented "single model per session" contract and creating a reproducibility hazard. Second, `run_decay` physically deletes SQLite episodes but `SemanticStore` has no delete path at all, so vectors accumulate unboundedly and `query_episodic_memory` can surface text for deleted episodes — this is *partly* intended (consolidated memory is meant to be durable) and so needs an explicit operator decision rather than a silent fix.

The test suite is a bright spot in coverage of the high-risk experimental invariants but mirrors the production duplication: the entire mocking harness (≈11 fakes/helpers) is copy-pasted across test_v2/v3/v4_session, one file (`test_instance_control.py`) is outright dead (it calls the removed `set_paused`/`clear_paused` API and is the suite's only red file), and there is no aggregate runner — which is precisely how that dead file went unnoticed. Repo hygiene is otherwise good, with two notable gaps: `.claude/` is not gitignored (committable machine-local settings), and the experiment docs have ballooned into near-byte-identical 18–22KB per-instance copies with no v3 doc. The risk profile for the cleanup is favorable: the vast majority is mechanical extraction into already-imported helper modules. The one item demanding real care — and an operator decision — is the cross-version session-engine consolidation, because each version's observable end/inject/schedule behavior *is* the experiment.

---

## Structural themes

### The run_vN_session scaffold is duplicated 4× around an already-shared engine
v2–v5 each wrap the shared run_one_tick engine in a ~300–400 line orchestration scaffold whose preamble (cron self-clear, lockfile, store/client init, OpenRouter branch, config/budget param block, registry last_wake, budget preflight) and tail (budget recheck, research-panel block, reschedule) are byte-identical; v4 vs v5 differ by ~44 lines of ~400, almost all version-string labels. The genuine per-version divergence (termination policy, cadence, context builder, tool spec, which *_sessions table is written) is small and localizable into explicit hooks. This is the single highest-leverage refactor — but also the only one with real experiment-semantics risk, so it must be hook-based and test-gated. v1 is a distinct finish_state engine and should stay separate.

*Affected:* orchestrator.py (run_v1_session), v2_session.py, v3_session.py, v4_session.py, v5_session.py, context_assembly.py

### Stale v1 copies of helpers the v2-family already fixed
v1 (orchestrator.py) carries its own divergent copies of helpers that v2 consolidated and fixed. _budget_pause_and_notify in v1 only clears cron (leaving the registry 'active' — the exact bug v2's comment documents fixing); _turn_cost in v1 lacks the OpenRouter actual_cost passthrough; _env, _extract_text, _tools_with_cache_control are v1-local duplicates. These are drift traps where a fix to the v2 copy silently fails to reach v1.

*Affected:* orchestrator.py, v2_session.py, instance_control.py

### Multiple parallel LLM-call / OpenRouter implementations
The 'LLM client layer' is three overlapping things: a dead ClaudeClient class (claude_client.py is really a pricing+JSON-extraction util), a real Anthropic-shaped OpenRouter adapter (openrouter_client.py), and a THIRD hand-rolled OpenRouter HTTP client in research/providers.py that has already drifted (it forces response_format=json_object except for Gemini, and accumulates cost per attempt — neither of which the adapter does). The Anthropic-vs-OpenRouter client-selection block is copy-pasted across v2–v5, and the 'join text blocks + read usage + estimate_cost' idiom recurs in 4+ sites.

*Affected:* claude_client.py, openrouter_client.py, research/providers.py, agent_tools/subagent.py, v2_session.py, v3_session.py, v4_session.py, v5_session.py

### Copy-pasted helpers chained through whichever version defined them first
Small semantics-neutral helpers are duplicated or reached via a tangled import chain rather than a shared module: _tools_for_call_vN (4 copies + a 5th in orchestrator), _execute_side_effects/_distress_check (redefined in v3 then imported by v4/v5 FROM v3, not from v2's original), _fmt_hours (2 defs + 2 inlines), _env (2 copies), DEFAULT_FALLBACK_MINUTES (2 defs), UTC=timezone.utc (≈11–13 modules), and the post-session research-panel block (verbatim in all 5 loops). The fix is one small session_common.py, not deeper chaining.

*Affected:* orchestrator.py, v2_session.py, v3_session.py, v4_session.py, v5_session.py, system_prompt.py, instances_common.py

### Per-version boilerplate replicated in data, persistence, and presentation layers
The 'one copy per experimental version' pattern leaked beyond the session loops into the stores and the dashboard: memory/episodic.py has 4 near-identical log_vN_session/recent_vN_sessions pairs with doubled column lists; dashboard/app.py's _dashboard_context has 3 triplicated per-version summary blocks with repeated awake-time stat math and two duplicate live-row synthesizers; tests/ replicate the same per-version harness. Each new version adds another copy across all three layers.

*Affected:* memory/episodic.py, dashboard/app.py, tests/test_v2_session.py, tests/test_v3_session.py, tests/test_v4_session.py

### dashboard/app.py is a 2589-line module mixing five concerns
One module bundles request lifecycle, HTML routes, control routes, a ~412-line data-assembly god-function, a 283-line timeline builder, a workspace browser, and a two-format API serializer (_bundle_json/_bundle_markdown re-project the same bundle twice). No separation between data access, serialization, and rendering. Strictly read-only over orchestrator-written data, so decomposition carries no experiment-semantics risk — only the load-bearing g.instance contract must be preserved.

*Affected:* dashboard/app.py, dashboard/templates

### Reproducibility hazards: provider/model and cost drift
Several issues quietly threaten the integrity of cost accounting and provider routing that a research testbed depends on: spawn_subagent hardcodes claude-sonnet-4-6 and ignores the session's OpenRouter client; OpenRouter cost falls back to an Anthropic-only (Opus-tier) pricing table for non-Anthropic models with no warning; research/providers retry accumulates cost but only keeps last-attempt token counts; and requirements.txt uses only lower-bound pins with no lockfile.

*Affected:* agent_tools/subagent.py, openrouter_client.py, research/providers.py, claude_client.py, requirements.txt

### Contract & documentation drift hiding behind working code
Names and docs no longer match behavior: SemanticStore has no delete despite decay deleting episodes; the file-sandbox docstring claims absolute paths are rejected (they're accepted inside the sandbox); 'three channels' docstrings omit the 4th advisory channel; dm_ben/fetch_dms_from_ben target a channel not a DM; tool return shapes are inconsistent ({error} vs {sent,reason}); the dead header_meta Jinja block is defined in 10 templates but never rendered; experiment docs are near-identical 22KB copies with no v3 doc; .claude/ is committable. Individually low-severity, collectively a maintainability tax.

*Affected:* memory/semantic.py, agent_tools/files.py, communications/slack_client.py, agent_tools/registry.py, dashboard/templates, experiments, .gitignore

---

## How to read the list

- **Tiers:** **P0** = foundational / highest-leverage do-first · **P1** = high value · **P2** = worthwhile · **P3** = minor/nits.
- **Effort:** S = <2h · M = <1 day · L = >1 day.
- **⚠️ Experiment-integrity gate:** these touch observable per-version behavior — land only behind green per-version tests, and (P1-4, P2-7) explicit operator sign-off.

## Prioritized punch list

### P0

#### [P0-1] Extract a shared session_common.py for semantics-neutral helpers and untangle the v2→v3→v4→v5 import chain
*duplication · severity high · effort M · confidence high*

**Problem.** Numerous small, semantics-neutral helpers are either duplicated or reached through a fragile import chain. _tools_for_call_vN is the same 5-line deepcopy+cache-stamp helper four times (plus a 5th in orchestrator). _execute_side_effects is byte-identical in v2 and v3, yet v3 redefines it and v4/v5 import it FROM v3 (verified: v4_session.py:79, v5_session.py:80) rather than from v2's original — so a fix to v2's copy won't propagate. _distress_check, _env, _fmt_hours, DEFAULT_FALLBACK_MINUTES are similarly duplicated; DEFAULT_FALLBACK_MINUTES is imported into v3/v4 but never referenced (only used inside v2's signal handler, which they import wholesale). This is the foundational cleanup that makes the larger engine extraction (P0-2) tractable.

**Recommendation.** Create session_common.py holding the genuinely shared, semantics-neutral helpers: tools_with_cache_control(spec, caching), _execute_side_effects, _distress_check, _env, _fmt_hours, and constants (DEFAULT_FALLBACK_MINUTES, WIND_DOWN_NOTICE, MAX_TICKS_PER_SESSION). Have v2–v5 import ONLY from session_common, not from each other. Each _tools_for_call_vN becomes one call passing its own TOOLS_SPEC_V* constant. Remove the unused DEFAULT_FALLBACK_MINUTES imports from v3/v4. Move import copy to module top. Keep all per-version context builders, system prompts, and loop scaffolding in their own files.

**Risk.** Low. All moved code is pure plumbing/formatting — bodies are byte-identical today (verified), so behavior is unchanged. Main risk is introducing an import cycle; a fresh session_common.py avoids that. Run tests/test_v2..v5_session.py after the move; they import several of these symbols and will catch breakage. Do NOT rename TOOLS_SPEC constants or change cache_control placement.

**Why this tier.** P0 because it is low-risk, high-leverage, and a prerequisite that de-risks the engine extraction: once helpers live in one place with a clean import graph, P0-2 becomes a matter of extracting the scaffold rather than also untangling the helper chain. Directly serves goals 2 (kill duplication) and 3 (maintainability).

<sub>Locations: `v2_session.py:248-253 (_tools_for_call)` · `v3_session.py:107-112 (_tools_for_call_v3)` · `v4_session.py:91-96 (_tools_for_call_v4)` · `v5_session.py:89-94 (_tools_for_call_v5)` · `orchestrator.py:132-142 (_tools_with_cache_control)` · `v2_session.py:442-467 + v3_session.py:164-189 (_execute_side_effects, identical)` · `v3_session.py:132-162 (_distress_check, imported by v4/v5 FROM v3)` · `v4_session.py:79 + v5_session.py:80 (from v3_session import ...)` · `orchestrator.py:103-107 + v2_session.py:552-556 (_env)` · `system_prompt.py:81-84 + v2_session.py:232-234 (_fmt_hours; inlined in v4_session.py:228-229, v5_session.py:213-214)` · `orchestrator.py:78 + v2_session.py:61 (DEFAULT_FALLBACK_MINUTES)` · `v3_session.py:65 + v4_session.py:64 (unused DEFAULT_FALLBACK_MINUTES import)`</sub>

#### [P0-2] ⚠️ Extract a shared v2–v5 session engine (preamble + finalize + loop) with explicit version hooks; keep v1 separate
*duplication · severity high · effort L · confidence high · depends on P0-1*

**Problem.** The four run_vN_session functions (~1300 lines total) are largely the same orchestration scaffold around the already-shared run_one_tick engine. The preamble (cron self-clear, lockfile+atexit, HF_HOME, store init, signal handler, Slack/Brave/Anthropic init, OpenRouter branch, the ~20-line config/budget param block, registry last_wake, budget preflight) is byte-identical v2↔v3↔v4↔v5; the per-tick budget guard, the post-session budget recheck, and the research-panel+reschedule tail are also byte-identical; v4 vs v5 differ by only ~44 lines of ~400, nearly all version-string labels. The genuine divergence is small and identifiable: (1) termination policy (v2 agent-controlled end_session vs v3 wind-down+logged would_end_now vs v4/v5 system-owned wind-down with no agent control); (2) cadence (v2/v3 fixed tick_interval vs v4/v5 adaptive active/idle backoff + inbound polling + neutral-clock injection); (3) the context builder; (4) tool spec / system prompt; (5) which *_sessions record is written.

**Recommendation.** After P0-1 lands, introduce session_engine.run_session(instance, spec) owning the identical preamble, budget guards, tick-result bookkeeping, finalize, and reschedule. Express each version as a hook object: context_builder, tools_spec, system_prompt_builder, termination_policy, cadence_policy, session_record_writer, and flags for would_end_now/next_invoke_minutes. Land it INCREMENTALLY: first hoist the preamble into setup_session(instance)->SessionRuntime and the tail into shared helpers (lower-risk slices), keep all four run_*_session as thin shells calling those, then unify the loop last. Keep v1 OUT of this engine — it is a distinct finish_state loop.

**Risk.** HIGH experiment-semantics risk. The whole point of v1–v5 is observable behavioral divergence; a naive merge could silently change a condition (v4/v5 must never expose end/schedule control; v3 wind-down must be the only non-budget exit; v5 must not auto-inject AGENTS.md; v3 alone sets would_end_now). Mitigate by treating termination/cadence/context/record-writer as pure per-version hooks with NO shared defaults that could leak v2 semantics into v4/v5, keeping all four tests/test_v{2,3,4,5}_session.py green at every incremental step, and doing this LAST after the lower-risk extractions.

**Why this tier.** P0 because it is the single largest complexity and duplication win in the repo (goals 1+2) and unlocks every downstream per-version cleanup — but its high risk is why it depends on P0-1 and the harness work (P1-7) and must be incremental and test-gated. This is the central 'shared engine with version hooks' the brief calls for.

<sub>Locations: `v2_session.py:559-877 (run_v2_session)` · `v3_session.py:196-547 (run_v3_session)` · `v4_session.py:264-662 (run_v4_session)` · `v5_session.py:248-638 (run_v5_session)` · `v2_session.py:562-642 / v3_session.py:199-281 / v4_session.py:267-355 / v5_session.py:251-339 (identical preamble)` · `v2_session.py:852-866 / v3_session.py:526-539 / v4_session.py:641-655 / v5_session.py:617-631 (identical research-panel + reschedule tail)` · `orchestrator.py:481-827 (run_v1_session — keep separate)`</sub>

#### [P0-3] Delete dead test_instance_control.py and add an aggregate test runner
*testing · severity high · effort S · confidence high*

**Problem.** test_instance_control.py fails outright: it calls instance_control.set_paused()/clear_paused() and asserts read_control() returns {'paused': False, 'paused_at': None, ...}. Those functions and the 'paused' flag were removed by the unified run-state refactor — instance_control now exposes pause/start/apply_status/current_status/is_paused and DEFAULT_STATE = {'paused_at': None, 'reason': None, 'resume_note': None} (verified). Running it raises AttributeError. It is the suite's only red file and masquerades as coverage; test_state_model.py already covers the new model. Separately, there is no aggregate runner over the 13 plain-runner files — which is exactly how this dead file went unnoticed (README documents only test_v2_session.py).

**Recommendation.** Delete tests/test_instance_control.py. Before deleting, port its two still-unique unit checks into test_state_model.py if absent: the corrupt-control.json→defaults case (line ~67) and consume_resume_note one-shot semantics (line ~60). Then add scripts/run_tests.sh (or tests/run_all.py) that runs every tests/test_*.py via ./venv/bin/python, accumulates exit codes, and exits nonzero on any failure; reference it in CLAUDE.md/README. All tests are fully mocked offline, so the aggregator spends no API budget and is safe to run freely.

**Risk.** None to experiments (instance_control is operator lifecycle, not an experimental condition). Only risk is losing the two unique unit checks — port them first. The aggregator is additive tooling.

**Why this tier.** P0 because a red file plus no aggregate runner means 'is the suite green?' currently has no reliable answer — and the suite is the safety net for the high-risk engine extraction (P0-2). Cheap, zero-risk, and a prerequisite for trusting the test-gated refactors. Serves goals 2 and 3.

<sub>Locations: `tests/test_instance_control.py:39,42,43,50,51,70,78,80 (set_paused/clear_paused/{paused:False} legacy API)` · `instance_control.py:27 (DEFAULT_STATE has no 'paused' key)` · `instance_control.py:84,105,121 (apply_status/pause/start — the replacement API)` · `tests/test_state_model.py (covers the new model)` · `README.md:71-75 (documents only test_v2_session.py)`</sub>

### P1

#### [P1-1] Fix spawn_subagent to honor the session model/provider instead of hardcoding claude-sonnet-4-6
*correctness-risk · severity high · effort M · confidence high*

**Problem.** spawn_subagent defaults model to the literal 'claude-sonnet-4-6' and always calls ctx.anthropic.messages.create(...) (verified). ToolContext is always populated with the raw Anthropic SDK and carries NO session model field, even when the session runs on OpenRouter (the loop uses client=OpenRouterClient(...), not anthropic_client). So on an OpenRouter-backed instance a subagent silently runs a different provider AND model than the session, and estimate_cost() prices it with Anthropic rates. This contradicts CLAUDE.md's documented 'single model per session... spawn_subagent defaults to the same model' and is a reproducibility hazard for any experiment that delegates via subagent.

**Recommendation.** Add model (and ideally the session client) to ToolContext; have spawn_subagent default model to ctx.model and route through the same client the session loop uses (anthropic vs OpenRouter) rather than always ctx.anthropic. Update the tool-schema description (registry.py:154) to say 'defaults to the session model'. Keep the explicit-model override for callers who want it.

**Risk.** Behavioral: any instance currently relying on the hardcoded sonnet default would change model and cost. Since the documented intent IS same-model, aligning to it is the safer state — but it WILL change subagent outputs/costs for OpenRouter instances; note in the experiment log. Low blast radius (subagent is opt-in).

**Why this tier.** P1 (not P0) because blast radius is small (opt-in tool) but it is a genuine correctness + reproducibility bug in a research testbed that explicitly treats model as an independent variable — exactly the kind of silent drift the review is meant to surface. Highest-priority of the correctness items.

<sub>Locations: `agent_tools/subagent.py:10-17 (model='claude-sonnet-4-6'; ctx.anthropic.messages.create)` · `agent_tools/registry.py:16-29 (ToolContext has no model field)` · `agent_tools/registry.py:154 (tool-schema description names a specific model)` · `v2_session.py:600-612 (session uses OpenRouterClient, not anthropic_client)` · `v2_session.py:674-677 (ToolContext populated with anthropic=anthropic_client)`</sub>

#### [P1-2] Consolidate the two/three OpenRouter implementations and the client-selection block onto one transport
*duplication · severity high · effort M · confidence high*

**Problem.** research/providers.OpenRouterProvider.complete reimplements from scratch nearly everything openrouter_client.py already does: same POST to /chat/completions, same usage.include flag, same 3-attempt empty-content retry, same usage.cost-else-estimate fallback. They have ALREADY drifted (verified): providers.py forces response_format=json_object for non-google models and accumulates cost across retry attempts (cost += ...); openrouter_client.py does neither. Separately, the ~12-line Anthropic-vs-OpenRouter client-selection block (build anthropic.Anthropic, swap in OpenRouterClient + disable caching when is_openrouter_model) is copy-pasted byte-for-byte across all four session entry points, with v3/v4/v5 building an anthropic_client they then discard.

**Recommendation.** (a) Extract the shared OpenRouter transport (endpoint URL, headers, usage.include body, empty-content retry, usage→(tokens,cost) parse) into one helper in openrouter_client.py; have OpenRouterProvider delegate to it, threading response_format/Gemini behavior through as an explicit parameter (it is a real need for Mistral/DeepSeek, not incidental). (b) Add make_session_client(model)->(client, caching_enabled) in openrouter_client.py and call it from all four loops, avoiding the unused Anthropic client.

**Risk.** Medium. research/providers.py is on the research-panel cost path; its per-attempt cost accumulation and Gemini-excluded json_object behavior MUST be preserved exactly or panel spend/JSON reliability changes. The client-selection factory is very low risk (identical today). Verify via tests/test_research_panel.py (providers path) and a mocked v2 OpenRouter session. No experiment-semantics impact — client selection is provider plumbing.

**Why this tier.** P1 because it kills the most dangerous duplication cluster (a wire-protocol contract maintained in 2–3 places that has already diverged), directly serving goal 2, with manageable risk if the two real behavioral differences are preserved as explicit options.

<sub>Locations: `research/providers.py:73-135 (OpenRouterProvider.complete — 3rd OpenRouter impl)` · `openrouter_client.py:271-323 (_Messages.create)` · `openrouter_client.py:225-264 (_parse_response/cost)` · `v2_session.py:600-612 / v3_session.py:237-249 / v4_session.py:305-317 / v5_session.py:289-301 (identical client-selection block)`</sub>

#### [P1-3] Decompose dashboard/app.py into blueprints + a data/serializer module; extract one per-version summary builder
*complexity · severity high · effort L · confidence high*

**Problem.** One 2589-line module mixes request lifecycle, 12 HTML routes, 3 control routes, ~9 API routes, the ~412-line _dashboard_context builder, a 283-line timeline builder, a workspace browser, and a two-format API serializer. _dashboard_context contains three near-identical per-version summary blocks (v2 826-855, v3 857-951, v4/v5 953-1044) with the awake_vals→avg/min/max math repeated 3× (lines 838, 906, 1014) and two structurally-identical 'synthesize a LIVE session row from current episodes' blocks (v3 ~872-904, v4 ~968-1012). _bundle_json and _bundle_markdown are two full re-projections of the same bundle, each with its own truncation lambdas, that must be kept in lockstep.

**Recommendation.** (a) Extract _version_session_panel(version, store, current_session, is_live, episodes) that maps version→recent_v{2,3,4}_sessions, synthesizes the live row through one shared function parameterized by per-version field schema, and computes awake stats via one helper — keeping each version's distinct fields explicit. (b) Add _bundle_projection(bundle, verbose) producing the normalized, pre-truncated section dict; make _bundle_json a pass-through and _bundle_markdown a pure renderer over it. (c) Split into a package: app factory + lifecycle in app.py, HTML/control/api blueprints, and a data.py for _dashboard_context/_build_invocation_timeline/_build_bundle/serializers. Preserve the /api before_request exemption via request.blueprint and keep url_for endpoint names stable.

**Risk.** Medium. The g.instance contract and /api exemption are load-bearing; preserve via blueprint identity and stable endpoint names (templates call url_for with bare names). The Markdown API output is consumed by an external LLM researcher — keep section order/headings identical and diff a sample invocation before/after. No experiment semantics at risk: the dashboard is strictly read-only over orchestrator-written data. Do it incrementally (data layer first, then blueprints).

**Why this tier.** P1 because it is the second-largest complexity+duplication concentration (goals 1+2) and, being read-only, carries zero experiment-semantics risk — high reward, contained risk. Slightly below P0 only because it is independent of and less foundational than the session-engine work.

<sub>Locations: `dashboard/app.py:1-2589 (single module, 5 concerns)` · `dashboard/app.py:744-1156 (_dashboard_context god-function)` · `dashboard/app.py:826-855 (v2 summary block)` · `dashboard/app.py:857-951 (v3 summary + live-row synthesis)` · `dashboard/app.py:953-1044 (v4/v5 summary + live-row synthesis)` · `dashboard/app.py:2024-2124 (_bundle_json)` · `dashboard/app.py:2161-2314 (_bundle_markdown)` · `dashboard/app.py:158-231 (before/after_request g.instance contract)`</sub>

#### [P1-4] ⚠️ Resolve the decay-vs-semantic-store lifecycle mismatch (operator decision)
*correctness-risk · severity medium · effort M · confidence high*

**Problem.** run_decay() physically deletes un-consolidated episodes from SQLite, but SemanticStore has no delete/remove method at all (verified: only add_episode/search/count). Vectors are only ever appended. Any episode whose vector was written (consolidated episodes; in v1, EVERY episode is auto-embedded) leaves an orphaned LanceDB row that query_episodic_memory can still surface, returning text for an episode_id no longer in SQLite. The LanceDB table also grows unbounded with no compaction/reconciliation.

**Recommendation.** Make the contract explicit rather than silently drifting. Either (a) add SemanticStore.delete_by_episode_ids(ids) and call it from run_decay strictly for decayed ids — but NOT for consolidated/authored rows, since durable consolidated memory is the intended escape hatch; or (b) document that semantic memory is intentionally durable beyond decay and add a periodic reconcile/vacuum in maintenance.py. At minimum, ensure query_episodic_memory tolerates hits whose episode_id is gone. This needs an operator call (see open questions) because durability-after-decay may be intended per-version.

**Risk.** Deleting vectors changes what the agent can recall after decay — for v2–v5 the whole point of decay is that un-consolidated memory is gone, while consolidated memory surviving in the vector store is intended. Scope any delete strictly to ids decay removed; never touch consolidated/authored rows. v1 auto-embeds everything, so a v1 instance accumulates orphans regardless. Confirm per-version design before shipping.

**Why this tier.** P1 because it is a real correctness/consistency risk (stale recall + unbounded growth) that intersects experiment semantics — so it must be surfaced for an operator decision rather than fixed blindly. Medium severity because today it mostly affects growth and rare stale-text recall, not control flow.

<sub>Locations: `v2_session.py:101-117 (run_decay deletes SQLite episodes)` · `memory/episodic.py:755-764 (delete_episodes)` · `memory/semantic.py:27-138 (SemanticStore: add_episode/search/count, NO delete)` · `agent_tools/memory.py:8-20 (query_episodic_memory surfaces vector hits)`</sub>

#### [P1-5] Align v1's budget-pause and cost helpers with the fixed v2 versions (unify into shared/claude_client)
*inconsistency · severity medium · effort S · confidence high*

**Problem.** v1 (orchestrator.py) carries stale copies of helpers the v2 family already fixed. v1's _budget_pause_and_notify only calls cron_control.clear_instance() and never sets registry status='paused', whereas v2's version explicitly switched to instance_control.pause() with a comment noting that clearing cron alone 'left the registry showing active' (verified). So a v1 instance paused for budget stays registry-'active' and looks healthy on the dashboard/list while actually unscheduled. v1's _turn_cost also lacks the OpenRouter actual_cost passthrough that v2's _turn_cost has (v2's is a strict superset — actual_cost defaults to None → identical math).

**Recommendation.** Move the canonical _turn_cost(..., actual_cost=None) and _usage(resp) into claude_client.py (next to _pricing_for/estimate_cost) and import in both orchestrator and v2_session; delete both local copies. Make v1's budget pause call instance_control.pause() so the registry/dashboard stay consistent (import v2's helper or move both budget/fatal-pause helpers into a shared module). v1 simply passes actual_cost=None.

**Risk.** Low-medium. The cost/usage extraction is a behavior-preserving superset merge (verified multipliers 0.1/1.25 identical). Changing v1's pause to instance_control.pause() alters v1's post-budget registry state — but that is the documented intended behavior matching v2–v5 and only triggers on a cap hit. Confirm with the operator that aligning v1's pause semantics is acceptable (it does not change in-session agent behavior, only operator-facing status).

**Why this tier.** P1 because it fixes a latent operator-visible inconsistency (v1 shows 'active' while unscheduled) and removes a cost-math drift trap, both serving goal 2 — and it pairs naturally with the engine work but is independently shippable and cheap.

<sub>Locations: `orchestrator.py:110-129 (v1 _budget_pause_and_notify — cron-only, leaves registry 'active')` · `v2_session.py:470-490 (v2 version uses instance_control.pause)` · `orchestrator.py:145-162 (v1 _turn_cost — no actual_cost)` · `v2_session.py:69-94 (v2 _turn_cost + _usage with actual_cost)`</sub>

#### [P1-6] Unify the SQLite store boilerplate shared by EpisodicStore and ResearchStore
*duplication · severity medium · effort M · confidence high*

**Problem.** ResearchStore._conn is byte-identical to EpisodicStore._conn (sqlite3.connect + row_factory=Row + commit/close contextmanager — verified, and research/store.py's own docstring admits it 'mirrors that module'). Both use the same 'try ALTER; ignore duplicate column' migration idiom. Critically, BOTH classes open the SAME database file (every call site passes instance.episodes_db), so the project maintains two parallel connection managers and two schema-init paths against one DB.

**Recommendation.** Extract a small shared base (e.g. memory/sqlite_base.py) providing _conn(db_path) and run_idempotent_alters(conn, stmts); have both stores inherit/compose it. Remove the duplicated connection lifecycle and migration idiom while leaving each store's own SCHEMA/queries intact. Do NOT merge the two stores into one class — separating research persistence into its own module is reasonable; only the plumbing is duplicated.

**Risk.** Low-medium. Both already point at the same file with the same pragmas, so behavior is unchanged; main risk is import cycles (research already imports from claude_client/instances_common, so a memory-side base is safe). Run the research-panel and v2 session tests after.

**Why this tier.** P1 because two connection managers + two schema-init paths against ONE database file is a notable maintainability hazard (goal 3) and a clear duplication (goal 2), at low risk.

<sub>Locations: `memory/episodic.py:234-260 (_conn + idempotent-ALTER migration loop)` · `research/store.py:6 (docstring: 'Mirrors that module's sqlite3 + row_factory + contextmanager')` · `research/store.py:184-191 (identical _conn)`</sub>

#### [P1-7] Extract a shared test harness (tests/_harness.py); replace the cross-test import
*duplication · severity high · effort M · confidence high*

**Problem.** The entire mocking harness (FakeBlock, FakeUsage, FakeResp, tool_resp, the cycling Anthropic client, FakeSlack, FakeSemantic, RecordingCron, FakeLockfile, the Patches context manager, _injected_user_texts, _build_instance, _main) is copy-pasted near-verbatim across test_v2/v3/v4_session (verified: 11 such definitions in each), with test_v5 and test_fork carrying drifted copies. The copies have already begun to rot (v4's FakeSlack added inbound_batches; test_fork's RecordingCron diverged). Separately, test_research_panel reaches into test_v2_session for its harness via a sys.path hack — coupling it to a sibling test's private API, the wrong seam.

**Recommendation.** Add tests/_harness.py (plain module, no pytest) holding the Fake* classes, tool_resp, a single configurable FakeAnthropic (fail-on-exhaust + repeat-last modes), FakeSlack (optional inbound_batches), FakeSemantic, RecordingCron, FakeLockfile, _injected_user_texts, run_scenarios(SCENARIOS)->int, a parametrized session_patches(module, ...) context manager, and build_instance(version, overrides). Each test_vN_session imports from _harness and keeps ONLY its version-specific response helper and scenario list. Have test_research_panel import the harness from _harness instead of from test_v2_session. Do NOT merge the scenario bodies — each version's invariant assertions are the experiment.

**Risk.** Medium. The four version loops are distinct experimental conditions; the refactor must be harness-ONLY (fakes + plumbing), never touching what each scenario asserts. Keep each version's _build_instance config knobs as explicit per-call overrides so no condition's timing silently changes. The parametrized Patches must save/restore the same module attributes per module to avoid cross-test leakage. Run all four runners before/after and confirm identical PASS lines.

**Why this tier.** P1 and a prerequisite enabler for P0-2: a clean shared harness mirrors the production engine and makes the test-gated engine extraction far safer to land incrementally. ~600 lines of hand-synchronized duplicate harness directly serves goals 2 and 3.

<sub>Locations: `tests/test_v2_session.py:51-271 (11 fakes/helpers)` · `tests/test_v3_session.py:49-262 (same 11)` · `tests/test_v4_session.py:61-289 (same 11, FakeSlack drifted to add inbound_batches)` · `tests/test_v5_session.py:40-80 (drifted partial copies)` · `tests/test_fork.py:40-69 (drifted RecordingCron)` · `tests/test_research_panel.py:305 (imports Patches/_build_instance/end_tick_resp from test_v2_session)`</sub>

### P2

#### [P2-1] Collapse the four near-identical log_vN_session / recent_vN_sessions pairs
*duplication · severity medium · effort M · confidence high*

**Problem.** log_v2/v3/v4_session are structurally identical: build an INSERT … ON CONFLICT(session_id) DO UPDATE SET … that lists every column twice (VALUES + excluded.*). recent_v2/v3/v4_sessions are byte-for-byte identical except the table name. ~170 lines differing only by column set and table name; the doubled column lists are an error-prone hazard, and every new version adds another copy.

**Recommendation.** Extract a private _upsert_session_record(table, session_id, **fields) that derives the column list from fields.keys() and builds VALUES placeholders + 'col = excluded.col' update clause programmatically (excluding the PK), and a _recent_session_rows(table, n) for the readers. Keep the four public typed wrappers as thin shims so call sites and keyword contracts are unchanged. Table names are code-internal constants, so f-string interpolation of the table name is acceptable here.

**Risk.** Low if the wrappers keep identical signatures and the generated SQL is verified against the existing statements. Guard against reordering columns vs the positional VALUES. Run tests/test_v2..v5_session.py after.

**Why this tier.** P2: clear duplication (goal 2) but lower leverage than the session/dashboard/client clusters, and the per-version wrappers must stay distinct (each version has its own columns).

<sub>Locations: `memory/episodic.py:857-903 (log_v2_session + recent_v2_sessions)` · `memory/episodic.py:907-963 (log_v3_session + recent_v3_sessions)` · `memory/episodic.py:967-1025 (log_v4_session + recent_v4_sessions)`</sub>

#### [P2-2] Delete or demote the dead ClaudeClient/ClaudeResult; treat claude_client.py as the pricing+JSON utility it is
*dead-code · severity medium · effort S · confidence high*

**Problem.** ClaudeClient is instantiated nowhere in production — only in claude_client.py's own __main__ block (verified). Every real importer (orchestrator, v2_session, research/*, subagent) pulls only the module-level helpers _pricing_for / estimate_cost / _extract_json. The class advertises the module as 'the Claude client' when the actual client used everywhere is anthropic.Anthropic directly, misleading readers about where the LLM call lives.

**Recommendation.** Remove the ClaudeClient class, ClaudeResult dataclass, and the __main__ block, leaving the load-bearing module-level functions. Update the module docstring (which describes an unused wrapper). Optionally promote _extract_json to a public name (extract_json with a back-compat alias) since research/schema.py, prereg.py, proposal_review.py import the private symbol across package boundaries. A module rename (e.g. llm_pricing.py) is optional and touches several import sites — do as one mechanical pass if at all.

**Risk.** Low. Pure removal of unused code; load-bearing functions stay. If promoting _extract_json, keep the old private alias so in-module callers keep working and update the three research imports.

**Why this tier.** P2: dead-code removal that clarifies module identity (goal 3) and slightly de-risks the LLM-call consolidation, but low urgency. Pairs naturally with P1-2 and P2-3.

<sub>Locations: `claude_client.py:38-48 (ClaudeResult)` · `claude_client.py:111-144 (ClaudeClient)` · `claude_client.py:147-163 (__main__ smoke test — only instantiation site)`</sub>

#### [P2-3] Extract one post-session research-panel hook and one operator-script bootstrap helper
*duplication · severity medium · effort S · confidence high*

**Problem.** Two related duplications. (1) The 'Post-session research panel (inline, never blocks the next wake)' try/except block is copy-pasted verbatim in all five session runners (verified), differing only in how agent_root is derived (AGENT_ROOT constant in orchestrator vs instance.root.parent.parent in v2–v5) — a latent bug magnet if the directory layout changes. (2) Six call sites repeat load_dotenv(.env, override=True) + anthropic.Anthropic(api_key=os.environ[...]) + EpisodicStore/ResearchStore/optional-SemanticStore, encoding the override=True secret-loading gotcha in six places (instance_manager even re-imports os/dotenv as _os/_ld).

**Recommendation.** (a) Add research.panel.run_post_session(instance, episodic, semantic, anthropic_client, session_id, invocation_num) that resolves agent_root from one source, wraps the call in try/except, and is called by all five runners (folds into the engine's finalize tail once P0-2 lands). (b) Add a research bootstrap helper, e.g. research_clients(instance_id, *, embed=True), used by all four scripts and the two instance_manager commands, keeping override=True in one place. Also dedupe cmd_research_synthesize against scripts/run_cumulative_synthesis.py via the same helper.

**Risk.** Very low. Behavior is identical per call site today; pass agent_root as a parameter. Preserve instance_manager's lazy-import-inside-command pattern (avoids importing anthropic for unrelated subcommands) by lazy-importing the new helper inside command bodies. Preserve the 'never blocks next wake' try/except semantics.

**Why this tier.** P2: solid duplication wins (goal 2) at very low risk, in the healthiest subsystem. Below P1 because the research panel is already well-factored and these are edge dedups rather than structural fixes.

<sub>Locations: `orchestrator.py:798-811 / v2_session.py:852-866 / v3_session.py:526-539 / v4_session.py:641-655 / v5_session.py:617-631 (verbatim panel block)` · `orchestrator.py:36-38 vs v2-v5 instance.root.parent.parent (agent_root derived 2 ways)` · `scripts/run_research_panel.py:29-39 / run_cumulative_synthesis.py:25-34 / run_advisory_watch.py:29-39 / review_proposal.py:24-33 / instance_manager.py:1080-1093,1160-1175 (dotenv override + Anthropic + stores bootstrap)`</sub>

#### [P2-4] Define one tool-return contract and one numeric-input coercion; unify the capability-request side effect
*inconsistency · severity medium · effort M · confidence high*

**Problem.** Tools signal failure three ways: most return {'error':...}, comms returns {'sent':False,'reason':...}, request_capability returns {'logged':True,'sent':False,...}. The dispatch logger and v1/v2 loops detect failure via an 'error' key, so a failed Slack post is NOT logged as an error even though it failed. Numeric coercion is uneven (fetch_url coerces max_chars; web_search/query/recent pass count/k/n straight through). And the capability-request side effect exists twice with divergent Slack formatting (comms.py vs v3_session._execute_side_effects), so operators see different messages by version.

**Recommendation.** (a) Document one contract near dispatch(): every tool returns a dict; failures ALWAYS include an 'error' string in addition to tool-specific fields. Add error= to comms failure returns (keep sent/reason). (b) Add _as_int(value, default, lo, hi) and apply to count/k/n as fetch_url does for max_chars, mirroring BraveSearch's [1,20] clamp at the tool layer. (c) Extract one send_capability_request(episodic, slack, invocation_num, capability, rationale) owning the canonical message + logging, called from both the v1 tool and _execute_side_effects.

**Risk.** Low-medium. Adding 'error' to comms failures changes what the model sees on a failed post and what's logged — desirable, but re-run v2/v4 session tests (they assert on tool-result shapes); don't remove existing keys. Coercion is hardening only. The capability-request unification changes one path's exact Slack text (cosmetic; no experiment semantics depend on wording).

**Why this tier.** P2: improves maintainability and a real (if low-frequency) logging gap on failed comms (goals 2+3), but touches the model-visible tool surface so warrants test care; not foundational.

<sub>Locations: `agent_tools/comms.py:8-23,44-75 ({sent,reason} / {logged,sent,reason})` · `agent_tools/web.py:14-23 ({error})` · `agent_tools/registry.py:471-480 (dispatch fallback {error})` · `orchestrator.py:362 / v2_session.py:401 (failure detected by 'error' key)` · `agent_tools/web.py:290-295 (fetch_url coerces max_chars) vs memory.py:8-9,23-24 (no coercion)` · `agent_tools/comms.py:44-75 vs v3_session.py:176-189 (capability request, divergent Slack text)`</sub>

#### [P2-5] Add rate-limit (429) retry to SlackClient._post and fix stale 'three channels' docstrings
*correctness-risk · severity medium · effort S · confidence high*

**Problem.** SlackClient._post catches SlackApiError and logs+returns None for ALL errors including 'ratelimited' (429). _post is the runtime hot path (post_to_agent/observer/advisory, dm_ben) called every tick/episode across v1–v5, so under burst load a 429 silently loses the journal/observer/Ben message. Meanwhile slack_replay.py already honors Retry-After and retries once — the codebase knows the pattern, it's just not on the hot path. Separately, an advisory channel was added after the original three-channel design but slack_client/provisioning module docstrings still say 'three' and enumerate only notes/mirror/chat, while _SUFFIXES/_PURPOSES/return dict and tests assert four.

**Recommendation.** In _post, special-case err=='ratelimited': read int(headers.get('Retry-After','2')) (bounded/capped), sleep, retry once — ideally via a shared _post_with_retry(client, channel, text) reused by slack_replay so there's one policy. Keep it to one retry to avoid stalling a tick. Update both module docstrings and provision_instance_channels' docstring to enumerate four channels and note advisory is operator-only (only run_advisory_watch wires advisory_channel; session loops deliberately do not).

**Risk.** Low. Adds a bounded sleep on the rare 429 path — strictly better than dropping the message. Cap Retry-After so a hostile header can't stall a session. No experiment-semantics impact (Slack posts are observational side effects). Docstring change is zero-risk.

**Why this tier.** P2: a genuine (if intermittent) message-loss risk on the path every session uses, plus actively-misleading docs — both worth fixing, low effort/risk, but not as high-leverage as the structural items.

<sub>Locations: `communications/slack_client.py:111-122 (_post drops on any error incl. 429)` · `communications/slack_replay.py:119-131 (already handles ratelimited+Retry-After)` · `communications/slack_client.py:1-10 (docstring says 3 channels)` · `communications/slack_provisioning.py:1-12,58-63 (docstring says 3; code has 4)`</sub>

#### [P2-6] Collapse the double wake-interval clamp into one authority
*inconsistency · severity medium · effort S · confidence high*

**Problem.** Two independent floors on next-wake interval. v1/v2 clamp next_invoke_minutes to per-instance config min_interval_minutes (default 30); then install_instance_one_shot clamps AGAIN to the module constant MIN_INTERVAL_MINUTES (hardcoded 30). They only agree because both default to 30 — if an operator set config min_interval_minutes to 10, the session-level clamp would permit it but the cron-level clamp would silently bump to 30, making the config knob partly a lie. v3/v4/v5 don't apply the config clamp at all (they pass sleep_min, never near the floor), so the knob is also version-inconsistent.

**Recommendation.** Make install_instance_one_shot the single clamp authority: add min_minutes: int = MIN_INTERVAL_MINUTES, have v1/v2 pass their configured min_interval_minutes, and delete the duplicate clamp in orchestrator.py:662-664 and v2_session.py:791-796. Keep MIN_INTERVAL_MINUTES as the default for callers (v3/v4/v5) that don't pass one.

**Risk.** Low. Observable behavior is unchanged whenever min_interval_minutes==30 (the only default value), preserving reproducibility of existing experiments. Verify no instance config sets min_interval_minutes < 30 before shipping.

**Why this tier.** P2: removes a confusing double-clamp and makes the config knob authoritative (goals 2+3), low risk; folds naturally into the engine's reschedule tail (P0-2).

<sub>Locations: `cron_control.py:30 (MIN_INTERVAL_MINUTES=30)` · `cron_control.py:118-123 (install_instance_one_shot clamps to MIN_INTERVAL_MINUTES)` · `orchestrator.py:662-664 (clamps to cfg min_interval_minutes)` · `v2_session.py:791-796 (same)` · `instances_common.py:257 (config default min_interval_minutes:30)`</sub>

#### [P2-7] ⚠️ Resume note is only consumed by v4/v5 — dashboard resume note silently dropped for v1/v2/v3 (operator decision)
*inconsistency · severity medium · effort M · confidence high · depends on P0-2*

**Problem.** The dashboard resume button sets resume_note unconditionally for any instance (verified app.py:1251), but only v4_session.py:154 and v5_session.py:166 call consume_resume_note (verified — v1/v2/v3 never do). For v1/v2/v3 the note is written and never surfaced to the agent, then later silently cleared on the next start(). So an operator resuming a v1/v2/v3 instance believes they sent a reassuring 'paused for maintenance, nothing is wrong' message the agent never sees.

**Recommendation.** Decide one contract. Either (a) make all session entrypoints consume + inject the resume note — fold consume_resume_note + verbatim-at-top injection into the shared prologue from P0-2 so it's automatic for every version; or (b) if the note is intentionally v4/v5-only, have the dashboard skip setting it for other versions and say so. (a) is cleaner.

**Risk.** Adding consumption to v1/v2/v3 changes what those agents see on a maintenance-resume wake — a deliberate behavioral change to the first post-pause prompt for those conditions. Flag to operator; gate injection behind the same verbatim-at-top placement v4/v5 use and note it in experiment logs.

**Why this tier.** P2 and operator-gated because the fix touches an experiment-relevant stimulus (the resume prompt) for three conditions; best landed with the engine prologue (P0-2) so the consume+inject happens in one place.

<sub>Locations: `dashboard/app.py:1251 (start(... resume_note=note) for ANY instance)` · `instance_control.py:162-177 (set/consume_resume_note)` · `v4_session.py:154 + v5_session.py:166 (only consumers)` · `orchestrator.py / v2_session.py / v3_session.py (no consume_resume_note)`</sub>

### P3

#### [P3-1] Remove confirmed dead code: v3 distress constants/helper, unused imports, dead functions
*dead-code · severity low · effort S · confidence high*

**Problem.** Multiple confirmed-unused symbols and redundancies (verified by grep). v3's _WOULD_END_NOW_DISTRESS_RUN/_DEGRADATION_RUN/_normalize are leftover scaffolding (the _distress_check docstring states it deliberately does NOT use them). DEFAULT_FALLBACK_MINUTES is imported into v3/v4 but never referenced. Three dead top-level functions, a redundant 'import re' and duplicate '_trunc' in app.py, several unused imports, a dead active_instance_id()/VALID_STATUSES, and a dead PRAGMA branch (ben_response is always in SCHEMA).

**Recommendation.** Delete each. For orchestrator.py:55 keep _pricing_for and the noqa, remove only estimate_cost. For remove_legacy_orchestrator_entries, confirm the flat-layout migration is complete before deleting (almost certainly is). Either delete VALID_STATUSES or wire it into instance_control.apply_status() as a cheap validation guard (preferred — earns its keep). Delete the resolve_capability_request PRAGMA branch (keep only the UPDATE).

**Risk.** Very low. Each verified unused. Keep the noqa on the trimmed orchestrator import line; the migration helper is the only judgment call.

**Why this tier.** P3: pure low-risk hygiene (goals 2+3), excellent quick-win fodder, but individually minor.

<sub>Locations: `v3_session.py:103-104 (_WOULD_END_NOW_DISTRESS_RUN, _DEGRADATION_RUN), v3_session.py:128-129 (_normalize) — all unused (verified)` · `v3_session.py:65 + v4_session.py:64 (unused DEFAULT_FALLBACK_MINUTES import — verified)` · `orchestrator.py:165 (_extract_text dead), dashboard/app.py:699 (_invocation_to_session dead), cron_control.py:188 (remove_legacy_orchestrator_entries dead)` · `dashboard/app.py:11+1777 (duplicate import re), dashboard/app.py:43-45 (unused registry_entry/registry_txn/save_config), dashboard/app.py:462+1824 (two _trunc)` · `context_assembly.py:13 / scripts/run_cumulative_synthesis.py:17 (unused import json); memory/semantic.py:9 (unused import os); orchestrator.py:55 (unused estimate_cost — keep _pricing_for+noqa); research/advisory.py:18, proposal_review.py:18, synthesis.py:17 (unused from pathlib import Path)` · `instances_common.py:440-445 (dead active_instance_id), instances_common.py:47 (unused VALID_STATUSES)` · `memory/episodic.py:421-448 (dead PRAGMA branch in resolve_capability_request)`</sub>

#### [P3-2] Standardize conventions: logger naming, v2 dispatch wrapper, UTC/now_iso, plain-runner contract, pytest docstrings
*inconsistency · severity low · effort S · confidence high*

**Problem.** Several harmless-but-confusing convention splits. Two logger conventions coexist (variable name log vs logger; literal 'orchestrator.vN' vs __name__). v2 is dispatched via a module-level wrapper while v3/v4/v5 are inlined. UTC and 'now in UTC ISO' are reimplemented in 3+ forms. The plain-runner tests use inconsistent entry-point names and runner idioms, and four docstrings advertise a pytest invocation that cannot work (pytest is not installed; no conftest).

**Recommendation.** Pick one logger variable name (standardize on logger everywhere) but DO NOT rename the literal 'orchestrator.vN' strings — tests reference them by string to silence logs. Delete the orchestrator run_v2_session wrapper and inline v2 dispatch like v3/v4/v5. Standardize new code on instances_common.now_iso() (respect instance_control's documented import-cycle inlining). Standardize tests on the SCENARIOS+run_scenarios idiom and _main; drop stale pytest docstring lines and the half-present test_ wrappers (or adopt pytest consistently — not recommended).

**Risk.** Low/mechanical. CRITICAL: do not rename the orchestrator.vN logger strings (tests/test_v4_session.py:46-48 reference them); only rename the local variable.

**Why this tier.** P3: cosmetic consistency (goal 3) that reduces reader friction; low value individually but cheap, and several fold into P0-1/P0-3.

<sub>Locations: `orchestrator.py:52 + v2-v5 (log=getLogger('orchestrator.vN')) vs logger=getLogger(__name__) elsewhere` · `orchestrator.py:830-833,857-869 (redundant run_v2_session dispatch wrapper)` · `UTC=timezone.utc in ~11-13 modules; now_iso (instances_common) vs _utcnow_iso (episodic) vs inline (instance_control)` · `tests: main() vs _main(); check()+global vs SCENARIOS list; test_ wrappers in 5 files, absent in 8` · `tests/test_v2_session.py:8, test_research_panel.py:9, test_fetch_url.py:1, test_instance_control.py:1 (stale pytest invocation in docstrings)`</sub>

#### [P3-3] Dashboard templates: remove dead header_meta block, unify auto-refresh, extract research-note macro
*dead-code · severity low · effort M · confidence high*

**Problem.** Every child template defines a {% block header_meta %} with meaningful content, but _base.html never renders it — all silently discarded. Two incompatible auto-refresh mechanisms coexist: index uses the pausable JS controller while logs/session use raw <meta refresh> that wipes open <details>/scroll mid-read. The research-note card (header+pills+claims) is hand-rolled four times and has drifted (promoted_claims[:3] vs [:5] vs [:4]). The .card class is referenced but never defined; var(--border,#333) is a dead fallback (--border is always defined).

**Recommendation.** Delete the dead header_meta block from all 10 templates (matches the deliberately-slimmed one-row header). Replace <meta refresh> in logs.html/session.html with the {% if is_live %}window.AUTO_REFRESH_SECONDS=20{% endif %} pattern so all three share the pausable controller. Create a _macros.html with research_note_card(note, claims_limit) (and a status_pill macro shared by session/logs) and call from all sites; define a real .card class; drop the var fallback.

**Risk.** Low. Templates hot-reload, so verification is a browser refresh. The auto-refresh change is intentional and strictly better (pausable, preserves expanded panels). Diff each rendered research card before/after for visual regressions. No experiment-semantics impact (read-only display).

**Why this tier.** P3: presentational duplication + dead markup (goals 2+3) at near-zero risk; valuable cleanup but below the backend/structural work.

<sub>Locations: `_base.html:519-593 (no {% block header_meta %} render site) + the block defined in all 10 child templates` · `index.html:7 + _base.html:626-647 (JS auto-refresh) vs logs.html:5 + session.html:6 (<meta http-equiv='refresh'>)` · `index.html:606-627,629-664 + research.html:149-172 + session.html:104-110 (research-note card hand-rolled 4×, promoted_claims sliced [:3]/[:5]/[:4])` · `index.html:608 etc (undefined .card class + var(--border,#333) dead fallback)`</sub>

#### [P3-4] Repo hygiene: gitignore .claude/, pin/lock dependencies, dedupe experiment docs (+ add v3 doc)
*repo-hygiene · severity low · effort M · confidence high*

**Problem.** Three hygiene gaps. (1) .claude/ (machine-local settings + worktrees) is not gitignored, so a stray git add -A could commit per-machine config into the code-only repo. (2) requirements.txt uses only lower-bound pins with no lockfile — for a testbed where library/model versions are explicitly independent variables, a fresh install can pull incompatible majors and silently change agent behavior (numpy already constrained <2.0 hints this has bitten). (3) Experiment docs are near-byte-identical 22KB copies (v4 ×3, v5 ×2) with no v3 doc despite v3 being a full live condition, and the README template still lists only v1|v2.

**Recommendation.** (1) Add .claude/ (or at least settings.local.json + worktrees/) to .gitignore. (2) Generate a pinned lockfile (pip freeze > requirements.lock.txt from the working venv, or adopt pip-tools/uv) and document installing from it; at minimum add upper bounds on heavy ML deps (torch, sentence-transformers, lancedb, anthropic). (3) Factor shared experiment prose into a per-version base doc, keep per-instance files short (instance_id, model, status, dates, the yaml spec block, instance-specific notes); add the missing v3 doc; update README:21 to v1|v2|v3|v4|v5.

**Risk.** Low. Pin to currently-working versions (capture via pip freeze) rather than guessing. CAUTION on doc dedup: the ```yaml spec block is parsed by research/spec.py and its hash gates the panel coding scheme (editing reverts to pending_approval) — leave each instance's spec block untouched unless intentionally re-approving. Docs are never read by the agent.

**Why this tier.** P3: real reproducibility/hygiene concerns for a research repo (goal 3) but no functional impact; the .claude gitignore is a trivial quick win.

<sub>Locations: `.gitignore (no .claude entry; git check-ignore .claude → NOT IGNORED, verified)` · `.claude/settings.local.json, .claude/worktrees/` · `requirements.txt:1-25 (13 '>=' pins, 0 '==', no lockfile — verified)` · `experiments/v4-continuous{,-8a,-qwen-max}.md (~22KB each, near-identical)` · `experiments/v5-recollection{,-v5-qwen}.md (~18KB each)` · `experiments/README.md:21 (template says 'v1 | v2'); NO experiments/v3-*.md (verified)`</sub>

#### [P3-5] Smaller correctness/clarity nits: next_fire_at year fallthrough, file-sandbox docstring, query_episodic_memory None-guard, OpenRouter token accounting
*correctness-risk · severity low · effort S · confidence medium*

**Problem.** A cluster of confirmed low-severity issues. next_fire_at returns None on the first invalid date instead of trying next year, so a valid future fire date (e.g. Feb 29 in a non-leap display year) shows as '(none)' — display-only, never misfires. The file-sandbox docstring/tool description say absolute paths are rejected, but _resolve accepts them if they resolve inside the sandbox (not a security hole — relative_to still bounds it — but the doc is wrong). query_episodic_memory calls ctx.semantic.search unconditionally while consolidate guards ctx.semantic is None. research/providers retry sums cost but keeps only last-attempt token counts. OpenRouter cost falls back to the Anthropic Opus-tier table for non-Anthropic models with no warning, and the adapter has no 429/5xx retry/backoff.

**Recommendation.** cron_control: change the inner except to continue (keep the final return None). files: either reject absolute paths up front in _resolve (matches the docs) or fix the docstrings/descriptions to say absolutes are allowed only if they resolve inside the sandbox. memory: return {'results': []} when ctx.semantic is None (pick one convention with consolidate). providers: accumulate tokens like cost (or document last-attempt-only). openrouter: when actual_cost is None AND is_openrouter_model, log a warning and consider a more neutral default than Opus pricing; optionally add bounded exponential-backoff retry covering 429/5xx (folds into P1-2's shared transport).

**Risk.** All low. Most are hardening/clarity. The OpenRouter pricing-fallback change alters non-authoritative budget estimation — pick the default deliberately and confirm empirically whether OpenRouter returns usage.cost for the configured models. Adding HTTP retries changes failure timing/spend — cap attempts and add backoff.

**Why this tier.** P3: individually minor and partly medium-confidence on real-world reachability, but cheap and worth logging; the OpenRouter items fold into P1-2.

<sub>Locations: `cron_control.py:174-181 (except ValueError: return None instead of continue — verified)` · `agent_tools/files.py:1-5 + registry.py:60 (docstring claims absolute paths rejected; _resolve accepts in-sandbox absolutes — verified)` · `agent_tools/memory.py:8-9 (query_episodic_memory unguarded ctx.semantic) vs consolidate.py:52,87 (guarded)` · `research/providers.py:118-135 (cost accumulates but tokens reassigned per attempt)` · `openrouter_client.py:254-263 (non-Anthropic models fall back to Opus-tier pricing, no warning); openrouter_client.py:311-323 (no 429/5xx retry/backoff)`</sub>

---

## Conflicts reconciled during synthesis

- **The session-loop reviewer, the instance-lifecycle reviewer, and BOTH cross-cutting reviewers each independently proposed extracting the duplicated session preamble/helpers/engine — at different scopes (preamble-only helper, session_common.py for helpers, full session_engine with hooks) and with conflicting effort estimates (M for the preamble slice vs L for the full engine).**
  - *Resolution:* Split into two sequenced items by risk and scope rather than listing them as competing findings. P0-1 (session_common.py for semantics-neutral helpers, effort M, low risk) is the foundational, near-zero-risk extraction that everyone agreed on; P0-2 (the full shared engine with version hooks, effort L, HIGH experiment risk) depends on it and on the test harness, and is explicitly incremental/test-gated. This honors the brief's 'shared engine with hooks over naive merge' preference while keeping the risky part isolated and last.
- **Whether to fold v1 (run_v1_session) into the shared engine. The session-loop reviewer said keep v1 separate (distinct finish_state loop); the cross-cutting duplication reviewer noted v1's scaffold is also near-duplicate and proposed adapting it 'last'.**
  - *Resolution:* Resolved in favor of keeping v1 OUT of the v2–v5 engine (P0-2), because v1's loop shape (finish_session/finish_state, no ticks/decay/consolidation, no OpenRouter) is materially different and v1 is the experimental control condition. Instead, v1's *stale helper copies* (budget pause, _turn_cost, _env, _tools_with_cache_control) are addressed separately in P0-1 and P1-5 — getting the dedup benefit without risking the baseline loop.
- **The _tools_for_call_vN helpers, _execute_side_effects, _turn_cost, the post-session panel block, and the OpenRouter client-selection block each appeared as standalone findings in MULTIPLE reviews (session-loop, agent-tools, LLM-client, and both cross-cutting reviewers), sometimes with slightly different location sets.**
  - *Resolution:* Merged each root cause into a single item listing all affected components/locations. The four+ _tools_for_call helpers and _execute_side_effects chain fold into P0-1; the client-selection block + OpenRouter transport fold into P1-2; the panel block + script bootstrap fold into P2-3; _turn_cost folds into P1-5. Kept the most precise file:line set from across the overlapping reviews for each.
- **The memory reviewer recommended adding SemanticStore deletes from run_decay, but flagged it could change post-decay recall — which collides with the experiment-integrity constraint that v2 decay deleting un-consolidated memory (while consolidated memory survives) is intentional by design.**
  - *Resolution:* Kept as P1-4 but framed as an explicit operator decision (also surfaced in open_questions), with a hard guardrail in the recommendation: any delete must be scoped strictly to ids decay removed and must NEVER touch consolidated/authored vectors (the intended durable escape hatch). Did not recommend a blind fix — the durability-after-decay semantics may be exactly what each condition intends.
- **The dm_ben/fetch_dms_from_ben rename and the channel='dm' log label: the Slack reviewer flagged the name/behavior mismatch but explicitly cautioned against rushing it (load-bearing across ~25 sites + the agent-facing tool name is an experiment-relevant surface).**
  - *Resolution:* Deliberately did NOT promote this to an action item. The agent tool name dm_ben is part of the agent's tool vocabulary (an experiment surface across v1–v5), and the channel='dm' string is persisted data the dashboard reads. Per the experiment-integrity constraint, a rename could subtly alter agent behavior/reproducibility and split historical rows. Captured as an open question instead, with the existing docstrings as the standing mitigation.
- **Effort/severity disagreements on the dashboard decomposition: the dashboard backend reviewer rated the full blueprint split L/high; some overlapping concerns (per-version summary builder, bundle serializer) were rated M.**
  - *Resolution:* Combined into one P1-3 with a phased recommendation (extract data layer + per-version summary builder + bundle projection FIRST as M slices, then the blueprint split as the L finale). Rated the item L/high overall but explicitly sequenced the lower-risk M slices first, so the high-value summary/serializer dedup can land without committing to the full blueprint move.

## Quick wins (low effort, low risk, do first)

P0-3, P3-1, P2-6, P3-4

## Recommended sequencing

Phase 0 — Safety net and foundation (do first, low risk): Land P0-3 (delete dead test_instance_control.py + add aggregate runner) so 'is the suite green?' has a reliable answer before any refactor. In parallel, knock out the pure quick wins P3-1 (dead code/unused imports) and P3-4's .claude gitignore + dependency lock, since they touch nothing structural.

Phase 1 — De-risking enablers for the engine (low risk, high enablement): Land P0-1 (session_common.py: hoist semantics-neutral helpers, untangle the v2→v3→v4→v5 import chain) and P1-7 (shared test harness tests/_harness.py + remove the cross-test import). These two together make the big engine extraction tractable and test-gated. P1-5 (align v1's budget-pause and cost helpers, move _turn_cost/_usage into claude_client) and P2-2 (delete dead ClaudeClient) pair naturally with P0-1 and the LLM-client work.

Phase 2 — Independent high-value structural work (can run alongside Phase 1, no shared dependencies): P1-1 (spawn_subagent model/provider fix) and P1-2 (consolidate the OpenRouter implementations + client-selection factory) — do P1-1 and the make_session_client factory together since both touch provider routing; P2-2 and P3-5's OpenRouter items fold in here. P1-3 (dashboard decomposition) is fully independent and read-only — do its M slices (per-version summary builder, bundle projection) first, then the blueprint split. P1-6 (SQLite store base) and P2-1 (log_vN_session dedup) are independent memory-layer cleanups.

Phase 3 — The headline engine extraction (highest risk, do LAST): P0-2 (shared v2–v5 session engine with version hooks) — only after P0-1, P0-3, and P1-7 are green. Land incrementally: preamble→setup_session, tail→shared helpers (incl. P2-3 panel hook), then the loop. Run all four per-version test suites after EACH step. P2-7 (resume-note consume+inject) and P2-6 (single wake clamp) and P2-3's panel hook fold into the engine's prologue/finalize once it exists, so depend on P0-2.

Phase 4 — Remaining P2/P3 polish (any time, low risk): P2-4 (tool contract + coercion + capability dedup), P2-5 (Slack 429 retry + docstrings), P3-2 (convention standardization), P3-3 (template macros + auto-refresh + dead header_meta), P3-4's experiment-doc dedup, P3-5's remaining nits.

Experiment-integrity gate throughout: P0-2, P1-4, P2-7 (and the deferred dm_ben rename) touch observable per-version behavior — none ship without the per-version test suites green and, for P1-4/P2-7, an explicit operator sign-off.

## Operator decisions that gate the work

1. FREEZE vs REFACTOR the session loops: Are v1–v5 considered closed/published experimental conditions whose code should be frozen for reproducibility, or are they actively-iterated and safe to refactor behind a shared engine (P0-2)? This is the single biggest decision gating the highest-leverage work. If any condition's results are already 'in the record,' the shared-engine extraction must guarantee byte-identical observable behavior (prompts, end/inject/schedule timing, which *_sessions table is written) and be validated against the per-version tests before merge.
2. Decay vs semantic-store durability (P1-4): Is it intended that consolidated/authored vectors survive decay indefinitely (the documented escape hatch) AND that the LanceDB table grows unbounded with no reconciliation? Should we (a) add scoped deletes for decayed ids only, (b) add a periodic maintenance vacuum, or (c) explicitly document durability as designed and leave growth alone? Affects what the agent can recall post-decay, so it is an experiment decision, not a pure bug fix.
3. Resume-note contract (P2-7): Should the dashboard 'resume with note' feature work for ALL versions (requires adding consume+inject to v1/v2/v3, which changes their first post-pause prompt — an experiment stimulus change), or is it intentionally v4/v5-only and the dashboard should simply not offer it for other versions?
4. v1 + OpenRouter (P1-5): Is v1 ever intended to run on an OpenRouter model? If yes, v1's missing actual_cost path and Anthropic-only pricing fallback become a live mis-costing bug; if v1 is permanently Anthropic-only, the helper unification is still worth doing for consistency but the cost gap is only theoretical. Confirms whether to treat it as correctness or hygiene.
5. dm_ben / channel='dm' rename: Is the agent-facing tool name dm_ben (and the persisted channel='dm' log label) a frozen part of the agent's tool vocabulary across conditions, or is a deliberate, version-aware rename to message_ben acceptable? A rename changes a model-visible tool name (experiment surface) and splits historical vs new ben_contact_log rows — needs an explicit call before any action.
6. OpenRouter pricing fallback (P3-5): For non-Anthropic models that don't return usage.cost, what default pricing should the budget guard assume instead of the current Opus-tier table — a neutral mid-tier estimate, zero-with-a-flag, or a real per-model table seeded from OpenRouter's /models endpoint? This affects when the budget guard trips for those instances.
7. Dependency pinning policy (P3-4): For a testbed that treats library/model versions as independent variables, should the repo adopt a committed lockfile (pin to the currently-working venv) for reproducible re-runs, while keeping requirements.txt as the human-edited source — and who owns regenerating the lock when a dependency is intentionally bumped?
8. specs/ directory convention (low confidence): Is specs/ (currently holding only the completed, IMPLEMENTED fetch_url-tool.md) meant to be a living spec location or an archive of finished design docs? Determines whether to keep, relocate under experiments/docs, or formalize it in README.
