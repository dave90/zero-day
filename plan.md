# Plan: Remove the `notes` tool + show a clean, concise "what's going on" in the chat

## Context

The `notes` tool was meant to let the agent narrate its plan, but in practice the
model prefers real actions over writing notes, so it's effectively dead weight.
Meanwhile the chat is either verbose (full free-form `thought`) or silent — during
`dispatch_*`/`await_subagents` the orchestrator emits **no** chat line at all
(those go through `_handle_dispatch`, which never emits a `ToolProposal`), so the
user sees a thought then a long silent wait.

Goal: **remove the `notes` tool**, and replace the failed notes-narration idea with
an **automatic, deterministic concise activity line** per orchestrator coordination
step, plus a **one-line condensed thought** (full thought still goes to
`thinking.log`). Target chat look (user-approved):

```
⋯ [orchestrator · turn 3] thinking…
  uid reflects unescaped — confirming XSS with a pentester.        ← condensed thought
  ▸ pentester #3 → "XSS in uid" @ /snippets.gtl                    ← activity line (dispatch)
  ▸ waiting for subagents (1 running)                              ← activity line (await)
  ✓ pentester #3 EXPLOITED (12 turns, 84s)                         ← subagent finished
  ▸ validating → "Reflected XSS in uid"                            ← activity line (validate)
```

Scope guard: only the `notes` **tool** is removed. The unrelated **`Finding.notes`**
and **`DispatchValidatorArgs.notes`** data fields, and the prose "creds/tokens/notes
to chain" in prompts/`subagents.py`, are **kept**.

## Change 1 — Remove the `notes` tool

Source:
- Delete `src/zero/tools/notes.py`.
- `src/zero/tools/registry.py`: drop the `NotesTool` import + `reg.register(NotesTool())`.
- `src/zero/ui/render.py`: remove the `notes`/`📝 note:` special-cases in `render_event`
  (the `ToolProposal` append block + the `ToolObservation` `if ev.tool == "notes": return`)
  and the `if tool == "notes":` branch in `summarize_tool_call`.
- `src/zero/llm/echo.py`: `EchoBackend`'s scripted demo's **first action** is a `notes`
  append — change it to `web_search` (`{"query": ..., "max_results": 3}`); drop "a notes
  append," from the final-answer prose.
- Comments only (keep `burp_traffic` as the remaining no-approval local tool):
  `orchestrator.py` (~"Local, no-side-effect tools (notes, burp_traffic) bypass the gate"),
  `tools/remote.py` (~42), `tools/base.py` (~44).
- `src/zero/agents/prompts/orchestrator_system.md`: drop `notes` from the recon tool list
  and delete the whole "**Keep a scratchpad.**" paragraph.
- Docs: `README.md` tool list line; grep `architecture.md` + `CLAUDE.md` for the `notes`
  **tool** (the architecture.md tool table row + the "(notes, burp_traffic) bypass" lines)
  and remove the tool mentions only.

Tests (pick `web_search` as EchoBackend's new first tool, then propagate):
- Delete `tests/unit/test_notes_tool.py`.
- `test_render_compaction.py`: delete `test_notes_append_renders_full_text_as_note_line`
  and `test_notes_observation_is_suppressed` (they test the removed special-case).
- `test_agent_loop.py::test_echo_first_step_is_notes_action` → assert `web_search`.
- `test_host_exec_guard.py`: drop `NotesTool` from the local-tools-no-approval test and
  the `notes` call in the bypass test (keep `burp_traffic`/`web_fetch`).
- `test_remote_tool.py`: drop the `NotesTool` line (keep BurpTraffic/PythonExec).
- `test_registry.py`: remove `"notes"` from the expected-tools tuple.
- String-literal renames (functionally optional but do for consistency):
  `test_mcp_server.py`, `test_approval_reason.py`, `test_envelope.py`, `test_replay_backend.py`.
- Marker-gated e2e: `test_container_real.py` — the in-container server uses
  `default_registry()`, so `rpc.call("notes", …)` breaks; switch the bind-mount
  round-trip to `python_exec` writing a file under `.pentest/`. `test_live_claude.py` —
  ask the model to use `web_search` (or similar) instead of `notes` and assert that tool.

## Change 2 — Concise activity line + condensed thought + subagent-finish line

**New event** in `src/zero/ui/events.py`: `ActivityLine(kind="activity_line", text="", label="")`;
add to the `UIEvent` union.

**Render** in `src/zero/ui/render.py`: `render_event` yields `ev.text` verbatim for
`ActivityLine` (like `AgentTextDelta`).

**Pump** in `src/zero/ui/app.py::pump_events`:
- Route non-labelled `ActivityLine` to `_emit_chat`; `if isinstance(ev, ActivityLine) and ev.label: continue`
  (mirror the labelled-tool suppression).
- **Un-suppress `SubagentFinished`** so the `✓ [pentester #3] STATUS — target (Ns)` line
  reaches the chat. Keep `SubagentStarted` + `SubagentProgress` suppressed (the `▸ …`
  dispatch line is the "started" signal; progress is too chatty).

**Emit the activity lines from `orchestrator.py::_handle_dispatch`** (one site per dispatch
branch — the `sid`/running counts are known there, so no `peek_next_id` needed):
- `dispatch_pentester` → `  ▸ pentester {sid} → "{attack_idea}" @ {target}`
- `dispatch_validator` → `  ▸ validating {sid} → "{title}"`
- `await_subagents` → `  ▸ waiting for subagents ({running_summary})`
- `list_subagents` → `  ▸ listing subagents`
- `finalize` → `  ▸ finalizing — writing report`
Recon tools keep their existing `→ tool [risk] summary` line (no extra activity line).

**Condense the thought** in `orchestrator.py::_do_turn`: add `OrchestratorConfig.thought_display`
(`"condensed" | "full" | "off"`, default **`"condensed"`**). Gate the `AgentTextDelta(env.thought)`
emit: condensed → first sentence via the shared condenser; full → as today; off → nothing.
`_log_thinking` already writes the FULL thought to `thinking.log` unconditionally (just above),
so the full reasoning is always preserved on disk.

**Refactor:** move `_short_reason` from `src/zero/ui/app.py` into `src/zero/ui/render.py`
(a pure-text module the orchestrator already imports for `summarize_tool_call`) and re-import
it in `app.py`. Use it for the condensed thought.

## Tests (new)
- `render` test: `render_event(ActivityLine(text="  ▸ x"))` yields the text verbatim;
  labelled `ActivityLine` is suppressed in `pump_events`.
- Orchestrated e2e (`tests/e2e/test_orchestrated_flow.py`): assert a `dispatch_pentester`
  turn now puts an `ActivityLine` on the UI queue (previously dispatch emitted nothing),
  and that a `SubagentFinished` reaches the chat path.
- `_do_turn` thought test: with `thought_display="off"` no `AgentTextDelta` thought is
  emitted; with `"condensed"` only the first sentence is.

## Verification
- `uv run pytest -q` (offline suite must stay green; ~277 tests + new ones).
- `uv run ruff check .`
- Manual smoke (optional, needs claude + sandbox): `uv run zero run --goal "…"` and confirm
  the chat shows the `▸`/`✓` lines + one-line thoughts, and `.pentest/thinking.log` still
  has the full reasoning.

## Notes
- Per project preference, I'll also copy this plan to `./plan.md` once out of plan mode
  (plan mode restricts edits to this file only).
- Minor: un-suppressing `SubagentFinished` also shows validator finishes alongside the
  finding card — slight redundancy, acceptable; easy to filter to pentesters-only later.
