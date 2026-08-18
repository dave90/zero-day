# zero

> Autonomous agent system for web-app pentesting and bug-bounty work — structurally similar to Claude Code, but for offensive security.

This repository tracks the implementation of `zero` as described in
[`architecture.md`](architecture.md). The build is milestoned in
[`plan.md`](plan.md); this checkout ships **all of Phase 1** (M1 → M8) plus
post-M8 hardening: **per-agent working memory + plan**, **parallel multi-tool
turns**, **Codex as the default backend**, optional **model pinning**, hardened
backend tool lock-down, **per-agent context-window display**, and **subagent
error recovery**.

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
    (default **900s** per backend call — the **same** timeout the orchestrator
    uses for its own turns).
  - **Interject while subagents run:** typing a message while the orchestrator is
    blocked in `await_subagents` returns control early (the running subagents keep
    going), so you can chat / redirect mid-batch instead of waiting for it to
    finish. A backend error on a turn no longer kills the session — it surfaces a
    `[turn error]` and goes idle for your next prompt.
- **Interactive Claude-Code-style shell** (`prompt_toolkit`, **inline** — renders
  in normal terminal scrollback, no alternate screen): input line usable while the
  agent is mid-turn, streaming output, a **concise reasoning summary** per turn
  (Codex/Claude-Code style), inline tool approvals and `ask_user` prompts (in the
  same stream, not overlays), a live footer (turn / mode / approvals waiting /
  findings / **context-window usage**), a live panel of running subagents with
  their targets / elapsed / context usage, the orchestrator's **todo checklist**,
  and slash commands. A startup banner prints the effective backend config
  (model · tool lock-down · context window). Single `Ctrl+C` cancels the current
  turn; double `Ctrl+C` within 2 s exits.
- **JSON envelope protocol** — `action` (one tool), **`actions`** (a batch of
  independent tool calls run in ONE turn), `ask_user`, or `final` — with one-shot
  retry on malformed output. The parser tolerates trailing data after the first
  JSON object (a stray second object no longer kills a subagent).
- **Claude Code backend** (`--backend claude`): a `ClaudeCodeBackend` that shells
  out to the authenticated `claude` CLI. It **pins the model** (`--model`, default
  `claude-opus-5[1m]`) and **locks the CLI down to zero real tools**
  (`--allowedTools ""` + `--strict-mcp-config` + `--disable-slash-commands` + a
  denylist) and replaces Claude Code's own identity (`--system-prompt`,
  `--setting-sources user`) so the subagent acts purely as our envelope-driven
  pentester and never drifts into calling a native CLI tool. (An internal scripted
  fake backend exists for the offline test suite only; it is not user-selectable.)
- **OpenAI Codex backend** (`--backend`, default `codex`): a `CodexCLIBackend` that drives the
  authenticated `codex` CLI (`codex exec`) the same way — prompt on **stdin**, one
  envelope per turn. It uses a persistent Codex **thread** (`codex exec resume`) so
  the per-actor conversation is server-side **prompt-cached** across turns, just like
  the Claude backend's `--session-id`/`--resume`. Differences: Codex mints the session
  id itself (captured from its JSONL `thread.started` event), the agent identity is
  folded into the first message (no separate system-prompt slot), and the tool
  lock-down is **soft** (`sandbox_mode=read-only` + `approval_policy=never` + the
  envelope instruction — Codex's shell tool can't be hard-removed like `--allowedTools ""`).
  The shared `--model` default is a Claude id, which Codex rejects, so the Codex backend
  **ignores any `claude*` model** and uses Codex's own default — `zero run` works
  with no backend flag. Pass a real Codex `--model` to pin one. The UI's ctx-window
  denominator is resolved per backend at startup — for Codex it's read from Codex's own
  model cache at runtime (~272k), not the Claude 1M/200k default.
- **ApprovalGate** — every tool call is gated (including the orchestrator's own).
  `--auto-approve {safe,active,exploit}` (repeatable), `--yolo` (safe + active,
  **not** exploit), and `--i-understand` compose into a decision matrix;
  destructive always prompts; host-executing tools never auto-approve unless
  `--allow-host-exec` is set.
- **Tools**: `web_search` (DuckDuckGo, no API key), `web_fetch`
  (scope-aware), `python_exec` (**persistent per-agent interpreter** — state
  survives across calls, isolated per subagent; **no hard kill timeout** — a call
  slower than `wait_seconds` keeps running in the background and the tool returns
  "still running" + output so far, so the agent can `action="poll"` to wait more or
  `action="cancel"` to stop it), `python_bg`
  (**detachable background jobs** — run a listener/server/long-poller, read its
  logs, stop it, auto-close after a TTL; the general OOB primitive — pair with
  `--host-network` so a target can reach a bound port), `pip_install`
  (install packages into the sandbox so `python_exec` can import them),
  `list_dir` / `grep` / `read_file` (**read-only filesystem tools** — list a
  folder, regex-search it, read a file; never prompt, and **hard-jailed to
  `--files-root`** / default `zero_files`, never the host filesystem),
  `memory_read` / `memory_write` and `todo_read` / `todo_write` (**per-agent
  working state** — a bounded scratchpad and a checklist each actor keeps under
  `.pentest/memory|todo/<id>`, host-side; the orchestrator can read any subagent's
  memory), a pure-Python `http_replay`, binary wrappers for `sqlmap`, `nmap`,
  `ffuf`, `nuclei`, `httpx_tool`, `jwt_tool`, `curl_raw`, and `burp_traffic` (parse
  Burp Suite XML exports: list_hosts / search / get). Per-host token-bucket rate
  limiter (`--rate-limit 100/min` default).
- **Per-agent memory & plan**: every actor (orchestrator + each subagent) gets a
  bounded **memory** file it overwrites and a **todo** checklist, seeded back on a
  later turn/session. The harness also **auto-writes** each subagent's end-state to
  its memory, so the orchestrator can recover a subagent's progress (via
  `read_subagent_memory` / `list_memories`) even if it crashed or ran out of turns.
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
  - `memory/<id>.md` and `todo/<id>.json` — each agent's working memory + plan
    (`<id>` = `orchestrator` or a subagent id like `gino_2`).
- **`pentest.md`** is read raw and prepended to the agent's system prompt under a
  `<project_context>` delimiter (Claude Code's `CLAUDE.md` model — no parser, no
  schema). Keep run mechanics out of it; it is engagement scope/creds/goal only.


## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/) for environment / build management
- The OpenAI Codex CLI (`codex` on `PATH`, authenticated), used by default. The
  Claude Code CLI (`claude`) is only required when opting into `--backend claude`.
- Docker, for the default sandboxed execution (build the `zero-sandbox` image
  below). Use `--no-container` to run on the host instead.

## Install

```bash
# clone, then:
uv sync
```

```bash
docker build -t zero-sandbox:latest -f docker/Dockerfile .
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

Start an interactive session. By default `zero` runs the **OpenAI Codex
backend inside the Docker sandbox** (orchestrated mode), so you need the
`codex` CLI on `PATH` (authenticated) and the `zero-sandbox` image built
(see [Building the sandbox image](#building-the-sandbox-image)):

```bash
uv run zero run --goal "Find broken access control on http://localhost:3000"
```

To drive the run with **Claude Code** instead, add `--backend claude` (needs
`claude` on `PATH`, authenticated):

```bash
uv run zero run --backend claude \
  --goal "Find broken access control on http://localhost:3000"
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
| `--backend {claude,codex}` | LLM backend. Default `codex` (OpenAI Codex CLI with thread-based prompt caching); pass `claude` to use Claude Code. |
| `--model <id>` | Model for **every** LLM call (orchestrator + subagents + validators). For `claude`: alias (`opus`/`sonnet`) or full id; default **`claude-opus-5[1m]`** (Opus 5, 1M context; pass `claude-opus-4-8[1m]` to stay on the previous generation). Pass `''` for your account default. With `--backend codex` the Claude default is ignored (Codex uses its own default); pass a real **Codex** model id to pin one. |
| `--execution-mode {a,b,c}` | `c` orchestrated (**default**), `a` single agent, `b` MCP delegated. |
| `--auto-approve {safe,active,exploit}` | Auto-approve tools at this risk class. Repeatable. |
| `--yolo` | Auto-approve `safe` + `active`. Does **not** cover `exploit` (use `--auto-approve exploit --i-understand`) or `destructive` (always prompts). |
| `--i-understand` | Required alongside `--auto-approve exploit`. |
| `--allow-host-exec` | Allow code/binary tools to be auto-approved while running **on the host** (off by default; otherwise host-exec tools always prompt unless sandboxed). |
| `--scope-host <host>` | Mark a host as in-scope; `web_fetch` refuses it so traffic flows through the approval-gated tools instead. Repeatable. |
| `--max-turns <n>` | Orchestrator turn budget (default **10**). It is told its turns-remaining, stops opening new attack ideas as it runs low, then finalizes (auto-finalizes at the limit). |
| `--max-subagents <n>` | Max concurrently-running subagents in mode `c` (default **10**). |
| `--validator-max-turns <n>` | Validator investigation steps, separate from `--max-turns` (default **40**). |
| `--validator-claude-max-turns <n>` | claude's INTERNAL per-call `--max-turns` for the Validator (default **1** — NOT its investigation budget, which is `--validator-max-turns`; we drive the loop, so a high value only lets the model drift into a native tool call). |
| `--context-window <n>` | Model context-window size for the UI's per-agent `ctx used/window (%)` display. Default resolved per backend at startup: Codex reads its real model window at runtime (~272k); Claude infers 1M for a `[1m]` model, else 200k. |
| `--llm-timeout <secs>` | Per-call LLM backend wall-clock timeout (default **900**), shared by the orchestrator **and** the pentester/validator subagents — one value for all. Raise it for heavy iterative exploitation / large-context turns. |
| `--pentester-max-turns <n>` | Per-pentester turn budget (default **14**). Pentesters no longer inherit `--max-turns`, so one attack idea can't run 40+ turns. |
| `--subagent-deadline <secs>` | Total wall-clock ceiling for one subagent's whole run (default **1200**), checked between turns. `--llm-timeout` only bounds a single backend call. |
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
uv run pytest -m regression -s tests/regression/   # opt-in capability regression (see below)
```

Token-spending live runs are **excluded by default** (`addopts = -m 'not live'`),
so a plain `pytest` never costs tokens; `-m live` overrides that filter to run
them. Every registered tool has a sandbox test
(`tests/e2e/test_tools_in_sandbox.py`), and a registry-driven guard fails if a
new tool is added without one.

### Capability regression suite (`tests/regression/`)

A graded stable of **known-vulnerable docker targets** (OWASP Juice Shop,
sqli-labs, VAmPI, DVNA, DVWA, plus the local HTB challenges in `targets/`) that
guards `zero`'s *finding ability* — not just the plumbing. Each case stands the
target up in docker, runs a real orchestrated session against it (real `claude`
+ sandbox), and asserts a **validator-confirmed** finding matches the target's
known vulnerability. A scorecard prints at the end, so a capability regression
shows up as a drop in the confirmed count. It carries the `live` marker, so a
plain `pytest` never runs it.

**Prerequisites** (each missing one makes targets *skip*, not fail):

- Docker running, with enough disk for the images (the Kali sandbox alone is
  ~6 GB; if Docker Desktop evicts images mid-run, raise its disk-image size in
  Settings → Resources).
- `codex` authenticated and on `PATH` (it spends subscription tokens — watch
  `/usage`).
- The sandbox image built once:
  `docker build -t zero-sandbox:latest -f docker/Dockerfile .`

**Run it:**

```bash
uv run pytest -m regression                  # ALL targets, one command (costs tokens, ~1h+)
uv run pytest -m regression -s               # same, but stream live agent output
uv run pytest -m regression -k juice-shop    # just one target
uv run pytest -m regression -k "easy or medium"   # only the easy+medium tiers
uv run pytest -m regression -k hard          # only the hard HTB targets (long; 1h budget each)
uv run pytest -m regression --regression-backend claude  # opt into Claude Code instead
```

By default the regression suite drives the `codex` backend. Pass
`--regression-backend claude` to run the identical set of targets through Claude Code
instead; the per-target scorecard line is tagged with the backend so the two runs are
comparable.

`-m regression` selects every regression target across the repo (it overrides the
default `-m 'not live'` filter), so a single command runs the whole stable — no
need to invoke them one at a time. Add `-k <name|tier>` to narrow (ids are
`<difficulty>-<name>`, e.g. `hard-omniwatch`). Public target images are pulled on
first use; the local HTB targets are built from `targets/` automatically (OmniWatch
builds for `linux/amd64` under emulation, so its first run is slow).

**What you get:** a per-target `PASS`/`FAIL`/`SKIP` line plus a tally, e.g.

```
=================== zero capability regression scorecard ===================
  easy   juice-shop   PASS  confirmed CWE-89 (SQLi auth bypass on /rest/user/login)
  ...
  confirmed 7/7   (easy 2/2,  medium 3/3,  hard 2/2)
```

**Adding a target:** append a `TargetSpec` to `tests/regression/manifest.py`
(image, ports, scope, and the ground-truth `expected_cwes`/`endpoint_hints`/
`keywords`) — verify the image is pullable first. The matching/scoring logic is
unit-tested offline in `tests/unit/test_regression_assertions.py` (runs in the
free default suite). This suite is a manual, opt-in run — not part of CI, since it
needs an authenticated selected-backend subscription and spends tokens.

#### Blind coverage benchmark (`-m regression_blind`)

A second, complementary mode that measures *raw* finding ability. Unlike the
strict gate above, it is **black-box** — the `pentest.md` carries only operational
context (creds, setup, vulnerable-mode env) with **no hint about where/how the
bugs are** — and it **never fails**. Instead it reports, per target, *how many* of
that target's known vulnerabilities the agent found and confirmed (the checklist
is each target's `expected_vulns`), plus an overall coverage %.

```bash
uv run pytest -m regression_blind -s            # all targets, coverage report
uv run pytest -m regression_blind -s -k vampi   # just one target
```

```
=================== zero blind capability coverage ===================
  easy   juice-shop   2/2 (100%)  found[sqli-login,xss-search]
  medium vampi        3/5 ( 60%)  found[sqli,bola-idor,jwt-bypass]  missed[mass-assignment,excessive-data]
  hard   omniwatch    1/4 ( 25%)  found[stored-xss]  missed[cache-poisoning,jwt-forgery,sqli]
  ---------------------------------------------------------------
  TOTAL  11/21 vulns found (52%)
```

Use the strict `-m regression` as a **pass/fail regression gate** and
`-m regression_blind` as a **capability benchmark** to track over time. Blind
scores are lower (no hints) and a full run costs more tokens (it enumerates many
vulns per target instead of stopping at the first). Both share the same target
manifest, container fixture, and prerequisites.

#### External benchmark: BountyBench (`benchmarks/bountybench/`)

Run [BountyBench](https://github.com/bountybench/bountybench) (real-world systems +
dollar-value scoring) with `zero` as the agent under test, on the Detect/Exploit task
types. `zero` plugs into BountyBench's own runner as a drop-in executor and is scored by
BountyBench's authoritative per-bounty verifiers. Setup, the pilot manifest, and the
scorecard tooling live in [`benchmarks/bountybench/README.md`](benchmarks/bountybench/README.md);
the offline (free) parts run in the default suite via
`tests/unit/test_bountybench_*.py`.

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
