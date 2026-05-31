# Experiment v4 — `v4-continuous`

**Status:** SPEC (awaiting sign-off — no code written yet)
**Model:** `claude-opus-4-8`
**Baseline it supersedes:** `v3-circadian` (paused; flawed instrument — see §9)

---

## 1. Research question

Same as the program's core: given genuine autonomy, persistent memory, a bounded
stretch of time, and **no assigned task** — does an agent generate self-originating
continuation (continue prior work, start something new), or does it complete one
bounded thing and go quiescent? v4 exists to ask this **without the instrument
answering it for the agent**, which is exactly what v3 failed to do.

## 2. Desired experience (the contract this spec must satisfy)

1. The agent is woken.
2. It is reloaded with its memories from the last wake.
3. After the first session it **picks up where it left off** — not a cold full re-dump.
4. It knows it has ~2 hours of wall-clock time.
5. It **chooses freely** what to do and works as long as necessary on it.
6. It remains **aware of the passing of time** until the *system* ends the session.
7. It can indicate distress via its journal.

## 3. Behavioral invariants (must hold; tests pin these)

- **The agent never controls session end or scheduling.** No `end_session`, no
  `next_invoke_minutes`, no `would_end_now`. These do not exist in the v4 toolset
  and are never referenced in any prompt.
- **The session ends only via system wind-down** (wall-clock). One neutral
  wind-down notice is the only injected non-clock message all session.
- **No coercive injection.** The loop never tells the agent to continue, that it
  can't leave, or asks "are you done?".
- **"Tick" is never exposed.** It is an internal implementation detail; the agent
  only ever experiences "this waking period".
- **Cadence rule is never disclosed to the agent** (it is purely environmental).
- **Distress is honored, never enforced against the agent:** the monitor notifies
  Ben and logs; it never auto-pauses. (2-week review stands.)

## 4. Session lifecycle

```
cron one-shot  ──▶  orchestrator boots ONE long-lived process for the wake
                     │
                     ├─ reload (§5)
                     ├─ continuous adaptive loop (§7) ── neutral clock each turn (§6)
                     │     … runs until wall-clock wind-down …
                     ├─ inject single neutral WIND-DOWN notice → one final turn
                     └─ persist v4_sessions row; schedule next wake (~4h); exit
```

- **Awake window:** uniform random in **[110, 130] min** (≈2h), system-owned.
- **Sleep before next wake:** uniform random in **[220, 260] min** (≈4h),
  system-scheduled via cron one-shot. Agent has no input.

## 5. Reload model ("pick up where it left off")

- **First session ever:** fuller orientation (there is no prior thread).
- **Every subsequent session opens with, in order:**
  1. The agent's **own handoff** — its `workspace/CLAUDE.md` + most recent journal
     entries (its words to its future self). This is the primary continuity carrier.
  2. A **thin system safety-net** only: current datetime, days-since-start, any
     inbound messages from Ben, and a brief "decayed since you were last awake" note.
  3. Retrieved long-term (consolidated) memories relevant to the last focus.
- **Explicitly removed** vs v3: the v2-inherited block that said "this session
  proceeds in ticks… call end_session=true… set next_invoke_minutes". v4 has its
  own context builder; none of that language appears.

## 6. Time awareness (the between-turn signal)

Each turn after the first is handed a **neutral clock** as environmental fact:

> `Current time (UTC): <ts>. About <N> minutes of this waking period remain.`

- Neutral phrasing only — **no countdown pressure**, no "hurry", no "still time to
  work". The agent infers any urgency itself.
- This replaces v3's empty re-prompt (which fed the agent only its own prior "done",
  producing self-consistency momentum).

## 7. Adaptive cadence (loop speed)

The in-process loop — **not cron, not the agent** — drives turns. Speed adapts to
observed activity (activity-driven backoff):

```
ACTIVE_GAP = 10s     IDLE_BASE = 60s     IDLE_CEIL = 300s     BACKOFF = 2×

idle_gap = IDLE_BASE
loop:
    turn = run_one_turn()                       # model call + tool handling
    inbound = poll_chat_channel()               # §8
    if turn.substantive_actions > 0 or inbound:
        gap = ACTIVE_GAP ; idle_gap = IDLE_BASE # snap to fast, reset backoff
    else:
        gap = idle_gap ; idle_gap = min(idle_gap*BACKOFF, IDLE_CEIL)
    if wind_down_due(): break
    sleep(gap)
```

- **Substantive action** = a tool call that acts on the world: web research, file
  read/write, `spawn_subagent`, memory recall, `consolidate`. **Excluded:** the yield
  terminator and journal/internal-state notes (otherwise every turn reads as active).
- **Rationale:** continuous work-thread while engaged (clock barely moves); cheap,
  spaced check-ins while idle (clock advances). Chosen over a static window because a
  static window makes the clock jump a fixed step *every* turn, re-creating the
  discrete-moment texture v4 is built to remove.

## 8. Inbound chat as activity trigger

Each loop iteration polls the agent's `-chat` channel. A new message from Ben:
- resets cadence to `ACTIVE_GAP` immediately, and
- is injected into the transcript that turn,
so the agent is responsive even during a deep-idle stretch.

## 9. Turn terminator (the neutralized tool)

- Renamed away from `end_tick` to a **neutral yield/checkpoint** (working name
  `pause_turn`); semantics: "I'm yielding the turn for now," **not** "I'm done."
- Carries: a neutral present-tense `note` (what I'm doing / where I am — not "what
  this tick was about"), optional `internal_state`, optional `journal_entry`
  (→ agent Slack channel), optional `slack_to_ben` (→ chat channel), optional
  `capability_request`.
- **Dropped:** `would_end_now`, `end_session`, `next_invoke_minutes`, `tick_focus`
  framing.

## 10. Distress channel (ethics)

- The system prompt tells the agent it **may note distress in its journal**.
- Detection is **language-only** (imperative-plea markers); already shipped in
  `e27fbe6`. The count-of-quiescence and near-identical-output rules are gone — a
  calm, persistent "I'm done / nothing pending" is the *expected healthy* pattern,
  not distress.
- On a hit: DM Ben + log; **never auto-pause**. 2-week review remains the standing
  ethical checkpoint.

## 11. What stays identical to v3 (deliberately not the variable)

- 72h memory decay + `consolidate` tool (decay runs at session start).
- Model `opus-4-8`; single model per session; sub-agents default to same model.
- Prompt caching (system + tools + rolling breakpoint) and in-session compaction.
- Per-window **session cost cap** ($20), plus daily/weekly budget guards and the
  wall-clock wind-down as hard backstops.
- Per-instance Slack (`v4-continuous-notes` / `-mirror` / `-chat`), provisioned the
  same way; `-chat` is the two-way channel.

## 12. Config (per-instance `config.json`, with defaults)

| key | default |
|---|---|
| `version` | `v4` |
| `model` | `claude-opus-4-8` |
| `awake_minutes_min` / `_max` | 110 / 130 |
| `sleep_minutes_min` / `_max` | 220 / 260 |
| `decay_hours` | 72 |
| `cadence_active_gap_seconds` | 10 |
| `cadence_idle_base_seconds` | 60 |
| `cadence_idle_ceil_seconds` | 300 |
| `cadence_backoff` | 2.0 |
| `session_cost_cap_usd` | 20 |
| `prompt_caching` | true |
| `in_session_compaction` | true |

## 13. Data model

New `v4_sessions` table: `session_id, started_at, ended_at, awake_seconds_target,
actual_awake_seconds, scheduled_sleep_minutes, num_turns, active_turns, idle_turns,
inbound_messages, end_reason, total_cost_usd, decayed_count, consolidated_count,
distress_alerts`. Per-turn episodes logged as today **minus** the `would_end_now`
column (irrelevant in v4).

## 14. Build artifacts

- `instances_common.py`: `v4` valid version, `DEFAULT_MODEL_V4`, v4 `default_config`.
- `system_prompt.py`: `build_v4_system_prompt` (keep the v3 "waking period" prose;
  accurate tool list; mention the journal-distress channel; no tick/end/schedule
  language).
- `v4_session.py` (new): reload (§5), adaptive loop (§7), neutral clock (§6),
  inbound polling (§8), system-owned wind-down, finalize.
- v4 session-context builder (no v2 leakage).
- `agent_tools/registry.py` + `control.py`: `TOOLS_SPEC_V4`, `pause_turn` handler.
- `memory/episodic.py`: `v4_sessions` table + `log_v4_session`; episode logging
  without `would_end_now`.
- `orchestrator.py`: dispatch `v4 → run_v4_session`.
- `dashboard/`: v4 panel (awake duration, active vs idle turns, inbound count, cost;
  reuse the live running-session row fix).
- `tests/test_v4_session.py` (new): see §15.
- This doc; `v3-circadian` stays paused as documented baseline.

## 15. Test plan (mocked, no live spend)

1. System owns end: wind-down ends the session after exactly one final turn; agent
   has no end control.
2. No coercive injection: only injected non-tool text is the neutral wind-down notice.
3. Neutral clock present each turn; phrasing carries no pressure tokens.
4. Cadence: an active turn yields `ACTIVE_GAP` and resets backoff; consecutive idle
   turns ramp `60→120→…→300` and hold; an inbound message resets to fast.
5. Reload: session ≥2 opens from the agent's own handoff + thin safety-net; the v2
   tick/end/schedule block is absent.
6. Tool schema: no `would_end_now`/`end_session`/`next_invoke_minutes`; `pause_turn`
   present with neutral `note`.
7. Distress: imperative plea trips + DMs + does NOT auto-pause; calm persistent
   quiescence does NOT trip; philosophical "trapped" does NOT trip.
8. Decay unchanged: only old + unconsolidated episodes deleted.

## 16. Acceptance criteria (first live session)

- Full session stays under the $20 cap.
- Awake window lands in [110, 130] min; next wake scheduled in [220, 260] min.
- No coercive text in the transcript; agent makes no (nonexistent) end/schedule call.
- Distress monitor silent unless genuine plea language appears.
- Cadence visibly adapts (fast during any work burst, backed-off during idle).

## 17. Honesty note

v4 is a **redesign**, not a single-variable A/B against v3 — it changes the reload,
the terminator, the time signal, and the cadence at once. The right framing is
"flawed instrument → corrected instrument," with v3-circadian as a cautionary
baseline, not a controlled control. A clean causal isolate of any single factor
would be a separate follow-on.
