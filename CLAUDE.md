# CLAUDE.md

Guidance for working on the **`zero`** codebase. (This is developer guidance for
the repo — *not* `pentest.md`, which is the per-engagement scope/creds file the
agents read at runtime. Don't conflate the two.)

`zero` is an autonomous web-app pentest / bug-bounty agent system — structurally
like Claude Code, but for offensive security. Deep detail lives in
[`architecture.md`](architecture.md) (as-built reference) and
[`README.md`](README.md) (user-facing). Read those before large changes.

## Commands

Everything runs through **uv** — always prefix with `uv run` (the package is not
importable otherwise; bare `python -m pytest` fails with `No module named 'zero'`).

```bash
uv sync                                   # create .venv + install deps
uv run pytest -q                          # DEFAULT: unit + docker sandbox tests (free, no tokens)
uv run pytest -m live                      # opt-in: token-spending live LLM runs (claude + gruyere)
uv run pytest tests/unit -q                # only the offline unit tests (no Docker)
uv run pytest tests/unit/test_x.py -q     # one file
uv run ruff check .                       # lint (line-length 100, target py311)
uv build                                  # wheel + sdist into dist/

# Run the app (needs `claude` CLI authenticated + the sandbox image):
uv run zero init                          # scaffold pentest.md + .pentest/
uv run zero run --goal "..."              # interactive session (mode c default)
uv run zero replay <audit.jsonl>          # re-drive a recorded run
docker build -t zero-sandbox:latest -f docker/Dockerfile .   # build sandbox (context = repo root)
```

**Test split (two buckets):**
- **`tests/unit/`** — offline & free: pure logic + the orchestrator/subagent flow
  tests driven by `ScriptedBackend`/`EchoBackend` (no network, no `claude`),
  asserting on the JSONL audit log.
- **`tests/e2e/`** — *real* tests: Docker sandbox tests (each tool actually runs
  in the `zero-sandbox` container — free, need the built image) and **live** runs
  that shell out to `claude` and **cost tokens**.

**Default `uv run pytest` = unit + docker, and never spends tokens:**
`addopts = -m 'not live'` excludes the live runs; opt in with **`-m live`** (a
command-line `-m` overrides the default filter). Docker tests skip with a
build-hint reason when the image isn't present. **Tests are async**
(`asyncio_mode = "auto"` — no `@pytest.mark.asyncio`). Every registered tool has a
sandbox test in `tests/e2e/test_tools_in_sandbox.py`; a registry-driven guard
(`test_every_registered_tool_has_sandbox_coverage`) fails if a new tool lacks one.

## Architecture in one screen

**The orchestrator and all agent loops run on the HOST. The Docker container is a
*tool sandbox* — only tool calls are proxied into it over HTTP RPC.** This is the
#1 thing to keep straight; the LLM loop is never inside the container.

- **Execution modes** (`--execution-mode`, default **`c`**):
  - `c` orchestrated — a senior-pentester **Orchestrator** recons directly and
    dispatches concurrent **pentester** + **validator** subagents.
  - `a` single Pentest agent; `b` MCP-delegated (`claude` runs its own loop).
- **Agent loop** (`agents/base.py`): one step per turn. The LLM returns a flat,
  `type`-tagged JSON **envelope** — `{"type":"action"|"ask_user"|"final", ...}`
  (`llm/envelope.py`). The caller runs the tool/answers, appends an observation,
  loops.
- **Subagents** (`orchestrator/subagents.py`, `tools/dispatch.py`): dispatched via
  `dispatch_pentester` / `dispatch_validator` / `await_subagents` / `finalize`
  (orchestrator-only tools, never in `default_registry`). Bounded by
  `--pentester-max-turns` (14), `--subagent-deadline` (1200s total wall-clock),
  `--llm-timeout` (900s per `claude -p` call — shared with the orchestrator's own
  backend, one value for all), `--max-subagents` (10).
- **Validation handoff** (`orchestrator/router.py`): only a Validator subagent
  confirms/rejects a finding; the orchestrator can't self-confirm.
- **Findings** (`findings/`): `.pentest/findings/{confirmed,rejected,pending}/<10-hex-id>.md`
  + `index.json`; verdicts are exactly `confirmed|rejected|pending`; deduped on
  `(normalized_endpoint, cwe, payload_shape, method)`.

## Code map

| Path | What |
|---|---|
| `src/zero/cli.py` | Click CLI: `run` / `init` / `reset` / `replay` and all flags. |
| `src/zero/orchestrator/orchestrator.py` | The core loop (all 3 modes), subagent runners, validation, UI event emit. Big file — start here. |
| `src/zero/orchestrator/subagents.py` | `SubagentManager` (concurrency, await, results). `SUMMARY_MAX_CHARS`. |
| `src/zero/orchestrator/{approval,ask_user_governor,rate_limit,router,context,audit,thinking}.py` | Gate, ask-user policy, per-host rate limit, validator handoff, `pentest.md` loader, logs. |
| `src/zero/agents/` | `base.py` (AgentLoop), `pentest.py` / `validator.py` / `orchestrator_agent.py` builders + `prompts/*.md`. |
| `src/zero/llm/` | `ClaudeCodeBackend` (Mode A), `ClaudeCodeBackendModeB` (MCP), `echo`/`replay` fixtures, `mcp_*`. |
| `src/zero/tools/` | One file per tool + `registry.py` (`default_registry`), `base.py` (`Tool`, `RiskLevel`), `remote.py` (RPC proxy), `dispatch.py`. |
| `src/zero/container/` | `manager.py` (docker run/stop), `rpc.py` (host RPC client). Server is baked into the image (`docker/`). |
| `src/zero/traffic/` | Burp XML parser + `TrafficStore` (file export only). |
| `src/zero/ui/` | Inline prompt_toolkit shell (`app.py`), event rendering, slash commands. |
| `tests/unit` | Offline & free (default run). Orchestrator/subagent flow tests via scripted backends in `tests/unit/test_orchestrated_flow.py`. |
| `tests/e2e` | Real tests: Docker sandbox (`test_container_real.py`, `test_tools_in_sandbox.py`) + live token-spending runs (`test_live_*.py`, `-m live`). |

## Conventions

- **Tools** are async, subclass `Tool` with a Pydantic `Args`, a `RiskLevel`
  (`safe`/`active`/`exploit`/`destructive`), and optional `runs_on_host` /
  `requires_approval`. Add one → drop a file in `tools/`, register in
  `default_registry`. Nothing else.
- **Match the surrounding style** (this file's tone, naming, comment density). The
  codebase favors dense, explanatory comments on *why*, not *what*.
- **Never let a background task / subagent crash the session** — catch broadly in
  loop bodies and surface the error to the UI/audit (see the `turn_error` and
  validator-crash handling for the pattern).
- **Regression tests for real failures:** reproduce a production-run bug with an
  offline test using scripted backends — see `tests/unit/test_orchestrated_flow.py`.
  Tool/sandbox bugs (missing/wrong binary, missing dep) need a `tests/e2e` docker
  test — unit tests can't see them.

## Gotchas

- **`--yolo` is NOT "approve everything."** It auto-approves only `safe`+`active`;
  `exploit` still needs `--auto-approve exploit --i-understand`, `destructive`
  always prompts, host-exec needs `--allow-host-exec`.
- **Scope is advisory, not a hard gate.** There is no `violates_scope → BLOCK`;
  `--scope-host` only blocklists hosts for `web_fetch`. (Roadmap in architecture.md §9.)
- **Backend is claude-only** (`--backend` is `Choice(["claude"])`); `echo`/`replay`
  are internal test fixtures, not user-selectable.
- **Container is ON by default** and falls back to host execution if Docker is
  missing (then host-exec tools prompt every call). `--no-container` forces host.
- **The UI is inline** (no alternate screen, no overlays); approvals/asks render in
  the stream. Single-line input (`Enter` submits).
- Don't hand-edit `uv.lock` — regenerate via `uv lock` / `uv sync`. It IS committed
  (this is an application).
