# zero

> Autonomous agent system for web-app pentesting and bug-bounty work — structurally similar to Claude Code, but for offensive security.

This repository tracks the implementation of `zero` as described in
[`architecture.md`](architecture.md). The build is milestoned in
[`plan.md`](plan.md); this checkout currently ships **all of Phase 1**
(M1 → M8).

## What works today

- **Orchestrated mode — the default** (`--execution-mode c`). A senior-pentester
  **Orchestrator** recons the target, then dispatches focused **pentester
  subagents** (one attack idea each) and **validator subagents**, all running
  concurrently. It reacts to results **as soon as each subagent finishes** (it
  does not block for the whole batch), chains discoveries (a captured
  cookie/token feeds the next subagent), and escalates — its full-picture view
  plus narrow-context subagents beats a single do-everything agent.
  - **Concurrency cap** via `--max-subagents` (default **10**).
  - **Strict validation:** the orchestrator only *submits* candidate findings;
    a finding is *confirmed/rejected* only by a Validator subagent that
    re-investigates from scratch. The orchestrator can never mark its own
    finding approved.
  - **Turn budget + awareness:** `--max-turns` (default **10**). The orchestrator
    is told its turns-remaining every turn, stops opening new attack ideas as it
    runs low (new `dispatch_pentester` calls are refused near the limit), and if
    it reaches the limit it **finalizes automatically** — waiting for any
    still-running subagents to finish, then writing the report.
  - **Bounded subagents:** each subagent is capped so one attack idea can't run
    away — `--pentester-max-turns` (default **14**, *not* inherited from the
    orchestrator's `--max-turns`), `--subagent-deadline` (default **1200s** total
    wall-clock per subagent, checked between turns), and `--llm-timeout`
    (default **900s** per `claude -p` call — the **same** timeout the orchestrator
    uses for its own turns).
  - **Interject while subagents run:** typing a message while the orchestrator is
    blocked in `await_subagents` returns control early (the running subagents keep
    going), so you can chat / redirect mid-batch instead of waiting for it to
    finish. A backend error on a turn no longer kills the session — it surfaces a
    `[turn error]` and goes idle for your next prompt.
- **Interactive Claude-Code-style shell** (`prompt_toolkit`, **inline** — renders
  in normal terminal scrollback, no alternate screen): input line usable while the
  agent is mid-turn, streaming output, inline tool approvals and `ask_user`
  prompts (in the same stream, not overlays), a live footer (turn / mode /
  approvals waiting / findings), a live panel of running subagents with their
  targets, and slash commands. Single `Ctrl+C` cancels the current turn; double
  `Ctrl+C` within 2 s exits.
- **One-step-per-turn JSON envelope protocol** (`action` / `ask_user` / `final`)
  with one-shot retry on malformed output. The parser tolerates trailing data
  after the first JSON object (a stray second object no longer kills a subagent).
- **LLM backend**: a `ClaudeCodeBackend` that shells out to the authenticated
  `claude` CLI. (An internal scripted fake backend exists for the offline test
  suite only; it is not user-selectable.)
- **ApprovalGate** — every tool call is gated (including the orchestrator's own).
  `--auto-approve {safe,active,exploit}` (repeatable), `--yolo` (safe + active,
  **not** exploit), and `--i-understand` compose into a decision matrix;
  destructive always prompts; host-executing tools never auto-approve unless
  `--allow-host-exec` is set.
- **Tools**: `web_search` (DuckDuckGo, no API key), `web_fetch`
  (scope-aware), `python_exec`, a pure-Python `http_replay`, binary wrappers for
  `sqlmap`, `nmap`, `ffuf`, `nuclei`, `httpx_tool`, `jwt_tool`, `curl_raw`, and
  `burp_traffic` (parse Burp Suite XML exports: list_hosts / search / get).
  Per-host token-bucket rate limiter (`--rate-limit 100/min` default).
- **Findings store + report**: confirmed/rejected/pending markdown under
  `.pentest/findings/{verdict}/`, scored with **CVSS 4.0**, deduped by
  `(normalized_endpoint, cwe, payload_shape, method)`. `finalize` (or hitting the
  turn budget) writes a **final report** to `.pentest/report.md` separating
  *found & approved* from *tested & rejected/failed*.
- **Docker sandbox** (on by default): a `kalilinux/kali-rolling` image with the
  binary tool surface baked in. Tools run **inside** the container — by default
  the whole registry is proxied (`--remote-all-tools`, on) through an RPC server
  on `127.0.0.1:<ephemeral>`; CPU / memory caps and `host.docker.internal`
  connectivity are wired in. Falls back to host execution if Docker is
  unavailable. (The wire is TCP, not a unix socket: uvicorn `chmod`s a UDS after
  binding and that fails over Docker-Desktop-on-macOS virtiofs.)
- **ask_user governor + `zero replay`**: per-agent ask_user cap, near-duplicate
  question suppression by normalized hash, `--yolo` auto-answers non-blocking
  questions; `zero replay <audit.jsonl>` rebuilds a run from its recorded
  assistant messages.
- **Logs under `./.pentest/`**:
  - `audit.jsonl` — append-only JSONL, one line per prompt, response, tool call,
    approval, observation, `ask_user`, dispatch, finding, and finalize. Survives
    partial trailing lines from a crash.
  - `thinking.log` — readable per-turn reasoning for the **orchestrator and every
    subagent** (pentester + validator): the stated `thought` plus the action /
    question / answer it produced.
  - `report.md` — the final report. `findings/{confirmed,rejected,pending}/` —
    per-finding markdown.
- **`pentest.md`** is read raw and prepended to the agent's system prompt under a
  `<project_context>` delimiter (Claude Code's `CLAUDE.md` model — no parser, no
  schema). Keep run mechanics out of it; it is engagement scope/creds/goal only.


## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/) for environment / build management
- The Claude Code CLI (`claude` on `PATH`, authenticated) — required, since
  `zero` runs the Claude Code backend by default.
- Docker, for the default sandboxed execution (build the `zero-sandbox` image
  below). Use `--no-container` to run on the host instead.

## Install

```bash
# clone, then:
uv sync
```

That creates `.venv/` and installs runtime + dev dependencies into it.

## Run

Scaffold a working directory:

```bash
uv run zero init
```

This writes a starter `pentest.md` and a `.pentest/` tree. Edit
`pentest.md` to describe your scope, credentials, and goals — the agent
reads the file verbatim every run.

Start an interactive session. By default `zero` runs the **Claude Code
backend inside the Docker sandbox** (orchestrated mode), so you need the
`claude` CLI on `PATH` (authenticated) and the `zero-sandbox` image built
(see [Building the sandbox image](#building-the-sandbox-image)):

```bash
uv run zero run --goal "Find broken access control on http://localhost:3000"
```

If Docker isn't available, the sandbox falls back to host execution (tool
calls still pass through the approval gate). To force host execution
explicitly, add `--no-container`.

While a run is live, `/running` lists the in-flight pentester/validator
subagents and their targets, and the footer tracks turn / approvals waiting /
findings. Everything lands under `./.pentest/`: the final report
(`report.md`), per-finding markdown (`findings/{confirmed,rejected,pending}/`),
the JSONL audit log (`audit.jsonl`), and a readable reasoning log for the
orchestrator and every subagent (`thinking.log`).

### CLI flags

| Flag | Meaning |
|---|---|
| `--goal <text>` | One-shot goal seeded as the first user message. |
| `--backend {claude}` | LLM backend (Claude Code CLI). Default `claude`. |
| `--execution-mode {a,b,c}` | `c` orchestrated (**default**), `a` single agent, `b` MCP delegated. |
| `--auto-approve {safe,active,exploit}` | Auto-approve tools at this risk class. Repeatable. |
| `--yolo` | Auto-approve `safe` + `active`. Does **not** cover `exploit` (use `--auto-approve exploit --i-understand`) or `destructive` (always prompts). |
| `--i-understand` | Required alongside `--auto-approve exploit`. |
| `--allow-host-exec` | Allow code/binary tools to be auto-approved while running **on the host** (off by default; otherwise host-exec tools always prompt unless sandboxed). |
| `--scope-host <host>` | Mark a host as in-scope; `web_fetch` refuses it so traffic flows through the approval-gated tools instead. Repeatable. |
| `--max-turns <n>` | Orchestrator turn budget (default **10**). It is told its turns-remaining, stops opening new attack ideas as it runs low, then finalizes (auto-finalizes at the limit). |
| `--max-subagents <n>` | Max concurrently-running subagents in mode `c` (default **10**). |
| `--validator-max-turns <n>` | Validator investigation steps, separate from `--max-turns` (default **40**). |
| `--validator-claude-max-turns <n>` | Per-call `claude -p --max-turns` for the Validator backend (default **60**). |
| `--llm-timeout <secs>` | Per-call `claude -p` wall-clock timeout (default **900**), shared by the orchestrator **and** the pentester/validator subagents — one value for all. Raise it for heavy iterative exploitation / large-context turns. |
| `--pentester-max-turns <n>` | Per-pentester turn budget (default **14**). Pentesters no longer inherit `--max-turns`, so one attack idea can't run 40+ turns. |
| `--subagent-deadline <secs>` | Total wall-clock ceiling for one subagent's whole run (default **1200**), checked between turns. `--llm-timeout` only bounds a single `claude -p` call. |
| `--container` / `--no-container` | Run tool calls inside the Docker sandbox. **On by default**; falls back to host execution if Docker is unavailable. |
| `--container-image <tag>` | Sandbox image tag (default `zero-sandbox:latest`). |
| `--host-network` | Use the host network — needed for Burp listening on 127.0.0.1. |
| `--burp-dir <path>` | Mount this host dir read-only at `/work/burp`. |
| `--container-cpus <n>` / `--container-memory <size>` | Resource caps (defaults `4.0` / `4g`). |
| `--remote-all-tools` / `--no-remote-all-tools` | Proxy **every** tool through the sandbox (not just `python_exec`). **On by default**, so binary tools are sandboxed too. |
| `--rate-limit <spec>` | Per-host token bucket, e.g. `100/min` (default) or `5/sec`. `0/sec` disables. |

### In the shell

- Type at the bottom prompt; **Enter** submits (the input line is single-line).
- **Ctrl+C** once: cancel the current agent turn (it receives an
  `interrupted_by_user` observation and may try again). **Ctrl+C** twice
  within 2 s: exit.
- **Approval prompt** (inline, in the stream): `y` accept · `n` deny ·
  `a` accept-this-tool-always · `Esc` deny. One-key answers apply while the input
  is empty; otherwise type your answer + Enter.
- **Ask-user prompt** (inline): type a free-form answer and press Enter, or press
  a digit (1–9) to pick one of the numbered options.

Slash commands (handled by the UI, never sent to the LLM):

| Command | Purpose |
|---|---|
| `/help` | List all commands. |
| `/keys` | Show keyboard shortcuts. |
| `/exit`, `/quit` | Exit the program. |
| `/clear` | Clear the scrollback. |
| `/scope` | Print `pentest.md`. |
| `/findings` | List confirmed findings. |
| `/show-rejected` | List rejected findings. |
| `/running` | List running subagents and their targets (orchestrated mode). |
| `/replay` | Pointer to the `zero replay` CLI subcommand. |
| `/mode` | Show the execution mode (set at launch with `--execution-mode`). |

## Development

The suite is split in two:

- **`tests/unit/`** — offline & free: pure logic plus the orchestrator/subagent
  flow tests driven by a scripted/echo fake backend (no network, no `claude`),
  asserting on the JSONL audit log.
- **`tests/e2e/`** — the *real* tests: the Docker sandbox tests (every tool is
  actually run inside the `zero-sandbox` container — free, just need the built
  image) and the **live** runs that shell out to `claude` and **cost tokens**.

```bash
uv run pytest -q          # DEFAULT: unit + docker sandbox tests — free, no tokens
                          # (docker tests skip with a hint if the image isn't built)
uv run pytest -m live     # opt-in: the token-spending live LLM runs (claude + gruyere)
uv run pytest tests/unit -q   # only the offline unit tests (no Docker needed)
```

Token-spending live runs are **excluded by default** (`addopts = -m 'not live'`),
so a plain `pytest` never costs tokens; `-m live` overrides that filter to run
them. Every registered tool has a sandbox test
(`tests/e2e/test_tools_in_sandbox.py`), and a registry-driven guard fails if a
new tool is added without one.

Lint:

```bash
uv run ruff check .
```

Build the wheel and sdist:

```bash
uv build
# → dist/zero-0.1.0-py3-none-any.whl
# → dist/zero-0.1.0.tar.gz
```

Install the built wheel into another environment:

```bash
uv tool install dist/zero-0.1.0-py3-none-any.whl
# then:
zero --help
```

### Building the sandbox image

The build context is the **repo root** so `src/zero` can be baked into the
image:

```bash
docker build -t zero-sandbox:latest -f docker/Dockerfile .
uv run zero run --goal "Find broken access control on http://localhost:3000"
```

The container mounts:

| Host path        | In-container path  | Mode |
|---|---|---|
| `<cwd>`          | `/work`            | rw   |
| `~/.claude`      | `/root/.claude`    | ro (Claude CLI auth)  |
| `--burp-dir`     | `/work/burp`       | ro (optional) |

The RPC server listens on TCP port 7333 inside the container; Docker
publishes it on `127.0.0.1:<ephemeral>` on the host. With `--host-network`
the in-container port is reachable directly at `127.0.0.1:7333`.

Once the image is built, the Docker sandbox tests run as part of the default
`uv run pytest`. To run just them (every tool exercised in the container):

```bash
uv run pytest -q -m docker tests/e2e
```


## License

See [`LICENSE`](LICENSE).
