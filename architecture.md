# Architecture Design Document
## `zero` — Penetration Testing & Bug Bounty Agent System

**Status:** As-built (reconciled with the implementation)
**Document type:** Architecture / as-built reference
**Scope:** Web application security

> **Legend.** This document describes the system as actually implemented.
> Features that are designed but **not yet built** are called out inline with
> a **📋 Roadmap** tag and collected in §9. A **🚧 Partial** tag marks features
> that exist but are narrower than the original design intended.

---

## 1. Overview

`zero` is an autonomous agent system that helps security researchers,
penetration testers, and bug-bounty hunters find vulnerabilities in web
applications. It is conceptually similar to Claude Code but optimized for
offensive security work.

The architecture is intentionally **loose and extensible**: there is no rigid
skill structure. The agents have access to a curated, growing catalog of
security tools (`sqlmap`, `nmap`, `ffuf`, `nuclei`, `httpx`, `http_replay`,
`jwt_tool`, …) and decide which to invoke.

The default runtime is a **hierarchical, multi-agent design** (execution mode
`c`):

- **Orchestrator** — a senior-pentester agent that does its own recon and
  dispatches focused subagents, keeping the big-picture strategy itself.
- **Pentester subagents** — short-lived, focused agents, each told to exploit
  **one** attack idea against **one** target. Several run concurrently.
- **Validator subagents** — independently investigate a candidate finding from
  scratch and return a confirm/reject/pending verdict plus CVSS scoring.

Mode `c` can also run a fixed-size **live peer swarm** with `--swarm-size N`.
Each top-level peer retains the hierarchy above while coordinating through a
transactional work/evidence/message store. Size one follows the legacy mode-c
path unchanged.

Two simpler execution modes also ship (see §3.2): a single Pentest agent
(mode `a`) and a fully delegated MCP loop (mode `b`).

The optional **Ralph** workflow wraps bounded orchestrated passes with reviewed
task planning, completion checks, and durable progress (§3.15). Engagement-local
**custom tools** extend the shared tool catalog (§3.6.2).

### 1.1 Key design choices

| Decision | Rationale |
|---|---|
| Containerized **tool execution** (Docker), ON by default | Isolates offensive tooling from the host. Note: only *tool calls* run in the container — the agent loops run on the host (see §2, §4). |
| Burp Suite as the traffic source (not MCP browser) | Burp handles auth, websockets, TLS interception, and complex SPAs that browser MCPs choke on. |
| Authenticated CLI backends | `CodexCLIBackend` is the default; `ClaudeCodeBackend` is also supported. Each reuses the selected CLI's authentication. |
| Pluggable `LLMBackend` interface | Modes `a`/`c` share a turn-based interface for Codex and Claude. Mode `b` uses the Claude MCP adapter specifically. |
| Human-in-the-loop by default | Registry tool execution follows the approval policy, with declared no-approval tools and session approvals (§3.9). |
| Project context file (`pentest.md`) | Per-engagement scope, credentials, and rules of engagement; separate from repository guidance in `AGENTS.md`. |
| Hierarchical orchestration (mode `c`) | A senior agent fans out cheap, independent attack ideas in parallel, then chains and validates the results. |

### 1.2 Non-goals (current)

- Network-layer / Active Directory pentesting (future).
- Mobile or binary exploitation (future).
- Replacing the human pentester. The system is an assistant.

---

## 2. High-Level Architecture

The **orchestrator and all agent loops run on the host**. The Docker container
is a **tool sandbox**: tool calls are proxied into it over an HTTP RPC channel
(`ContainerRPC`). This is the single most important correction to keep in mind
— the LLM reasoning loop is never inside the container.

```
                       ┌─────────────────────────────┐
                       │         User (CLI)          │
                       │  - approves tool calls      │
                       │  - answers ask_user         │
                       │  - reviews findings         │
                       │  - writes pentest.md        │
                       └──────────────┬──────────────┘
                                      │  inline terminal UI (prompt_toolkit)
                                      ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Orchestrator process (HOST)                   │
│  - Loads pentest.md (project context)                            │
│  - Runs the agent loop(s): mode a / b / c                        │
│  - ApprovalGate, RateLimiter, AskUserGovernor                    │
│  - SubagentManager (mode c): concurrent pentester/validator      │
│  - AgentRouter: Pentest→Validator handoff + findings persistence │
│  - AuditLog (.pentest/audit.jsonl) + ThinkingLog (thinking.log)  │
│                                                                  │
│   build_orchestrator_loop / build_pentest_loop / build_validator │
│        │  registry calls ───────────────┐                        │
└────────┼───────────────────────────────┼────────────────────────┘
         │ LLM CLI (subprocess)          │ tool RPC (HTTP)
         ▼                                ▼
  ┌──────────────┐         ┌──────────────────────────────────────┐
  │ Codex/Claude │         │        Docker container (sandbox)     │
  │   CLI (host) │         │  Tool Layer (proxied via remote wrap):│
  └──────────────┘         │  burp_traffic http_replay sqlmap nmap │
                           │  ffuf nuclei httpx curl_raw jwt_tool  │
                           │  python_exec web_search web_fetch …   │
                           └──────────────────────────────────────┘
                                      ▲
                                      │ Burp XML export (file)  📋 REST/extension
                              ┌───────┴───────┐
                              │  Burp Suite   │ ◄── user browses target manually
                              └───────────────┘
```

If sandbox startup fails, the system **falls back to host execution** and
applies the host-execution approval policy (§3.9). Pass `--no-container` to force
host execution. Memory/todo calls and orchestrator-only control operations remain
host-side even when the sandbox is running.

---

## 3. Components

### 3.1 Orchestrator

`Orchestrator` (`zero/orchestrator/orchestrator.py`) lives on the host and is the
only component the user interacts with directly.

**Responsibilities:**

- Parse CLI arguments and load `pentest.md` into a project context
  (`zero/orchestrator/context.py`).
- Start the Docker sandbox and swap the tool registry for RPC-proxied versions
  (`_start_container_and_swap_tools`).
- Drive the agent loop for the selected execution mode (§3.2).
- Apply the registry tool **approval policy** (§3.9).
- Route **`ask_user`** questions to the user and feed answers back (§3.10).
- Run the **SubagentManager** (mode `c`) and the **AgentRouter** (Pentest →
  Validator handoff + findings persistence).
- Persist the **audit log** (`.pentest/audit.jsonl`), the human-readable
  **thinking log** (`.pentest/thinking.log`), and findings.
- Drive the inline terminal UI (`zero/ui/app.py`).

**Key collaborators:**

```
Orchestrator
 ├── context.project_context_for(pentest.md) → str injected into prompts
 ├── ContainerManager + ContainerRPC → docker run / HTTP tool RPC / stop
 ├── ToolRegistry → catalog + RPC-remoting
 ├── ApprovalGate(ApprovalRequest) → Decision
 ├── AskUserGovernor → per-agent question caps + dedup + yolo policy
 ├── RateLimiter → per-host token bucket
 ├── SubagentManager → concurrent pentester/validator subagents (mode c)
 ├── AgentRouter + FindingsStore → validation handoff + .pentest/findings/
 ├── AuditLog → JSONL of every step
 └── ThinkingLog → readable reasoning per agent/turn
```

### 3.2 Execution modes (`--execution-mode a | b | c`, default `c`)

The system supports three runtime shapes behind one switch. (This is distinct
from the Mode A / Mode B *tool-execution* axis of the LLM backend in §3.5.1 —
modes `a` and `c` are both built on backend Mode A.)

- **`c` — orchestrated (DEFAULT).** The senior-pentester Orchestrator does recon
  directly and dispatches concurrent pentester/validator subagents via the
  dispatch tools (§3.3). This is the production default.
- **`a` — single agent.** One Pentest agent loop (`build_pentest_loop`) talks to
  the tool catalog directly and submits findings via `finding_submit`, which the
  orchestrator intercepts and routes to a background Validator. There is no
  mode-c pentester dispatch manager.
- **`b` — delegated / MCP.** Control is handed to the `claude` CLI for the whole
  session via an in-process MCP bridge (`ClaudeCodeBackendModeB` +
  `zero/llm/mcp_bridge.py`); the CLI runs its own loop and calls our tools as MCP
  tools. The registry approval policy applies inside the MCP tool boundary.
  This path instantiates `ClaudeCodeBackendModeB` regardless of `--backend`;
  it is not a Codex MCP mode.

Modes `a`/`c` return to an idle input loop after a final response (or `finalize`
in mode `c`). Mode `b` runs one delegated CLI session and emits `SessionEnd`
when it finishes, which closes the UI. `--ralph` selects the outer workflow in
§3.15 instead of these top-level loops. The operator can also exit the UI with
`/exit` or a double Ctrl+C.

### 3.3 Orchestrator loop & subagents (mode `c`)

The Orchestrator runs its own `AgentLoop` (`build_orchestrator_loop`) whose
catalog is the **recon tools** (safe + active registry tools, minus
`finding_submit`) plus **orchestrator-only dispatch, memory inspection, and
custom-tool management tools** (`zero/tools/dispatch.py`,
`zero/tools/custom_tools.py`). The dispatch tools are *not* in the default
registry, so modes `a`/`b` never see them. They are intercepted by the
orchestrator and never `run()` directly:

| Dispatch tool | Effect |
|---|---|
| `dispatch_pentester(attack_idea, target, context?, max_turns?)` | Spawn a focused Pentester subagent. Returns immediately; it runs concurrently. |
| `dispatch_validator(title, summary, endpoint, …, source_subagent?)` | Spawn a Validator subagent to confirm/refute a candidate finding (deduped first). |
| `await_subagents(which="any"\|"all", timeout?)` | Block until subagent result(s) are available. Defaults to returning as soon as the **first** finishes so the orchestrator can react. **Interruptible by operator input** (§5). |
| `list_subagents()` | Non-blocking snapshot of what's running. |
| `read_subagent_memory(agent_id)` / `list_memories()` | Read any subagent's working memory by id — recover what a subagent did even if it ran out of turns or crashed (its memory persists when its final summary doesn't). |
| `finalize(summary)` | Drain remaining subagents, write `report.md`, then go idle. |

These are intercepted host-side (never `run()`/remoted) and added only to the
orchestrator's catalog. The orchestrator may also batch several `dispatch_pentester`
calls in one `actions` envelope to fan out concurrently.

#### 3.3.1 Live peer swarm

With `--swarm-size > 1`, one `SwarmRuntime` owns the engagement lifecycle and runs
the selected number of `SwarmWorker` peers. Every peer has a globally qualified
actor/session id, independent conversation and child `SubagentManager`, plus an
initial coverage lane. The container, registry, approval gate, findings projection,
audit log, and final report remain single engagement-owned services.

`.pentest/swarm/state.sqlite3` is the durable coordination plane. It uses WAL,
foreign keys, short locked transactions, and an ordered event stream for workers,
flat groups, immutable recipient-snapshotted messages, leased/deduplicated work,
content-addressed evidence, response contributions, and centrally reserved finding
candidates. Collaboration tools are host-intercepted and are available only to
top-level peers; Pentester and Validator children cannot recursively join the swarm.

A response cycle freezes its expected peers, collects one contribution or explicit
unavailable result from each, and passes an immutable snapshot to a dedicated
tool-less summarizer. The coordinator writes one atomic report. Candidate dedup is
global to the swarm and only an independent Validator result can commit a terminal
verdict. Modes `a`/`b` and Ralph reject swarm sizes above one.

The initial response cycle is guarded by an ownership bootstrap. A peer must inspect
the shared ledger, atomically claim at least one work item, announce its boundary,
wait for coordination, and call `swarm_finish_bootstrap` before the runtime permits
ordinary target tools or child dispatch. Exact claims serialize in SQLite; semantic
overlap is resolved by peer messages before testing begins. Later response cycles are
follow-ups: peers reuse durable state, the summarizer unwraps accidental final-envelope
JSON, and `SessionSummary`/report generation occurs again only when findings changed.

**SubagentManager** (`zero/orchestrator/subagents.py`) runs each subagent as an
independent `asyncio` task, capped by a semaphore (`--max-subagents`, default
10). It tracks live state for the status panel, records every attack attempt for
the final report, and surfaces completed results through a queue the orchestrator
drains via `await_subagents`.

**Cross-agent isolation.** A subagent does **not** see the operator's full
conversation or the orchestrator's chain-of-thought. The orchestrator seeds each
pentester with a tight brief (target, attack idea, the engagement goal, and any
creds/tokens to chain in `context`). The subagent replies with a structured
`final`:

```json
{
  "status": "EXPLOITED | NO_FINDING | INCOMPLETE",
  "summary": "...",
  "candidate_findings": [{"title": "...", "summary": "...", "endpoint": "...",
                          "method": "...", "cwe": "...", "severity_hint": "...",
                          "evidence": "...", "payload_shape": "..."}],
  "discovered": ["creds / tokens / notes to chain into the next subagent"]
}
```

**Strict validation.** A pentester subagent never confirms its own finding, and
the orchestrator may not self-confirm. `finalize` is **refused** if pentesters
returned candidate findings but no Validator was ever dispatched — only a
Validator subagent's verdict counts.

### 3.4 Validator

A separate, independently prompted agent (`zero/agents/validator.py`,
prompt in `agents/prompts/validator_system.md`). Its job is to **distrust** a
candidate finding and confirm or reject it.

**Loop:** read the `Finding`, re-derive the proof with fresh eyes (its own
accounts, payloads, markers — not a literal replay of the pentester's requests),
check scope/business context from `pentest.md`, assign a CVSS 4.0 score, and
return a `Validation`.

**Tool access — same surface as the pentesters, minus `finding_submit`.** Many
exploits can't be proven without running them (you can't confirm SQLi without
firing the payload, an IDOR-write without the PUT/DELETE, an SSRF without the OOB
callback). A read-only validator would have to take the pentester's word for any
active-only finding. The "distrust" property lives in (a) the system prompt,
(b) the cross-agent isolation (only structured `Finding`/`Validation` objects
cross the boundary), and (c) the instruction to set up its own test conditions.

Validator registry tool calls follow the same approval policy (§3.9).

**Memory model.** In mode `a`, the orchestrator runs validations as background
tasks so the pentest loop never blocks; in mode `c`, each validation is its own
subagent. Each validator subagent investigates one finding with its own fresh
history.

> 📋 **Roadmap — shared cross-finding history & token-budget summarization.**
> The original design called for the Validator to keep one conversation across
> findings and for the orchestrator to auto-compact older turns near a token
> budget. Today each validation runs with a fresh history and there is no
> harness-managed history summarization/compaction of agent context. Backend CLI
> session behavior is separate from this harness limitation. (Each actor *does*
> keep a self-managed per-run **memory** file — §3.14 — but that's a scratchpad,
> not automatic context compaction. The UI also compacts tool-observation
> *rendering*, which is unrelated.)

**Output: `Validation` object** (`zero/findings/schema.py`):

```json
{
  "verdict": "confirmed | rejected | pending",
  "cvss40_vector": "CVSS:4.0/AV:N/AC:L/...",
  "cvss40_score": 6.9,
  "cwe": "CWE-79",
  "rationale": "Confirmed cross-tenant data access. Reproduced with two accounts.",
  "reproducer": "GET /api/orders/1041 with user A's cookie → user B's body",
  "additional_evidence": "..."
}
```

A `rejected` or `pending` verdict is fed back to the pentester/orchestrator with
the rationale + reproducer so the next attempt addresses the gap.

#### 3.4.1 Findings store

Findings live in `./.pentest/findings/`, one markdown file per finding, written
by the `FindingsStore` (`zero/findings/store.py`) after the Validator returns.
The directory is the source of truth for the UI, the report, dedup, and
cross-session memory.

```
.pentest/
├── findings/
│   ├── confirmed/
│   │   └── c5deb5e5de.md        # human-readable + frontmatter
│   ├── rejected/
│   │   └── 5d28bb3817.md        # retained and included in reports
│   ├── pending/
│   │   └── 7a3fc2e10b.md        # validator couldn't fully confirm
│   └── index.json               # dedup index; rebuilt if absent/unreadable
├── audit.jsonl
├── thinking.log
└── report.md                    # written by finalize / at the turn budget
```

- **Finding IDs** are 10-hex strings (`uuid4().hex[:10]`, e.g. `c5deb5e5de`).
- **Verdict values** are exactly `confirmed | rejected | pending` (the design's
  `needs_more_evidence` is implemented as `pending`).

**Visibility.** `write_report` (`zero/findings/report.py`) includes confirmed,
rejected, and pending findings. Confirmed findings include the validator's
reproducer; rejected findings include the rationale, and pending findings include
notes. Mode `c` also supplies unsuccessful attempt summaries. The session-summary
event carries all three verdict categories, and the inline UI points to the
report; `/findings` lists confirmed files
and `/show-rejected` lists rejected files. Visibility does not imply confirmation.

**Deduplication is file-based.** Before a finding reaches a Validator,
the orchestrator computes a `dedup_hash` over a normalized tuple
(`zero/findings/dedup.py`) of `(normalized_endpoint, cwe, payload_shape,
http_method)`, where `normalized_endpoint` strips IDs/query values
(`/orders/1041` → `/orders/{id}`). A hash matching an on-disk finding — or one
currently in-flight this session — is dropped with a "duplicate" observation so
the agent stops rediscovering the same bug. The store rebuilds `index.json` when
it is absent or unreadable; it does not automatically reconcile a valid but stale
index at every startup. The normalized tuple is a heuristic, not semantic dedup.

**Cross-session memory.** Findings persist between runs, so a second session
against the same target inherits the dedup state. Wipe it with `zero reset`
(deletes the whole `./.pentest/`) or scope per-target by running from a different
directory.

### 3.5 LLM backend adapter

The LLM is abstracted behind one interface (`zero/llm/backend.py`):

```python
class LLMBackend(ABC):
    @abstractmethod
    async def run(self, system_prompt: str, messages: list[Message],
                  tools: list[dict]) -> LLMResponse: ...
```

**Implementations today:**

- `CodexCLIBackend` — shells out to `codex exec` in backend Mode A. **Default.**
  Reuses the user's authenticated Codex CLI and resumes one persistent thread per
  actor so subsequent turns retain server-side context and prompt caching.
- `ClaudeCodeBackend` — shells out to `claude -p ... --output-format stream-json`
  (backend Mode A). Reuses the user's Claude subscription; pins the
  model by default and disables its native tools (§3.5.1).
- `ClaudeCodeBackendModeB` — registers an in-process MCP bridge and runs the
  `claude` CLI in agent mode (backend Mode B; used by execution mode `b`).
- `EchoBackend`, `ReplayBackend` — test/replay fixtures (not for production).

> 📋 **Roadmap — `ApiBackend`.** The interface can also support a raw API backend
> for pay-per-token use; that adapter is not implemented yet.

#### 3.5.1 Tool execution: two backend modes

There are two ways to plug the tool catalog into a coding-agent CLI. Both exist:

**Backend Mode A — envelope-driven reasoning** (used by execution modes `a` and `c`).
The host requests a JSON response per turn and executes the requested registry
actions. With session resume enabled (the default), each actor has its own
persistent Codex thread or Claude session, and subsequent calls send only new
messages. The stateless path sends full history; `EchoBackend`/`ReplayBackend`
remain fixtures. `AgentLoop` retains local history for recovery if a CLI session
fails.

1. The tool catalog (names, descriptions, JSON schemas) is passed to the LLM **as
   data inside the prompt**, not as callable tools.
2. The LLM must answer in a strict, **flat, `type`-tagged JSON envelope**
   (`zero/llm/envelope.py`):
   ```json
   {"type": "action",   "tool": "sqlmap", "args": {...}, "thought": "..."}
   {"type": "actions",  "thought": "...", "calls": [{"tool": "...", "args": {...}}, ...]}
   {"type": "ask_user", "question": "...", "options": [...], "free_form": true, "blocking": true}
   {"type": "final",    "answer": "..."}
   ```
   `actions` is the **batch** form: several INDEPENDENT tool calls run in ONE turn
   (one LLM round-trip). The orchestrator/pentester batch executor can run them
   concurrently while serializing stateful interpreter/package-install work.
   The generic validator driver handles batches sequentially. Each call appends
   an observation; `action` (singular) still works unchanged.
3. **Claude-specific tool restrictions:** `ClaudeCodeBackend` disables native
   tools so the model answers with text (our envelope):
   - **`--allowedTools ""`** — an empty *whitelist* grants no tools. (An empty
     `--tools ""` does **not** work — it's a greedy variadic and leaves Claude
     Code's defaults enabled; that bug let subagents still see and call `LSP`.)
   - **`--strict-mcp-config`** — ignore the user's MCP servers (Mode A passes no
     `--mcp-config`).
   - **`--disable-slash-commands`** — no skills.
   - **`DEFAULT_DISALLOWED_TOOLS`** denylist as a backstop (Bash/Read/…/`LSP`/
     `ToolSearch`/`Monitor`/…), kept exhaustive vs the installed CLI build.
4. The Claude CLI's **identity** is replaced so the subagent acts as our agent:
   **`--system-prompt-file`** carries our agent prompt on session creation
   (also dropping the default cwd/env/git/memory sections), and
   **`--setting-sources user`** drops project/
   local settings to avoid importing engagement-project guidance.
5. **Model selection** is backend-aware. Codex uses its configured model by
   default; Claude defaults to `claude-opus-5[1m]`. An explicit `--model`
   overrides the selection; an empty value lets Claude use its CLI default.
6. The orchestrator parses the envelope, runs the tool (in the container), and
   feeds the result back as the next observation.

The Claude adapter sends conversation input over stdin, reads streamed JSON CLI
events, and extracts the agent's envelope text. On resume, the CLI retains the
initial system prompt/settings. Its internal `--max-turns` (default **4**,
with a small retry bonus on `error_max_turns`) is only internal bookkeeping — small
on purpose: a few turns of headroom let a rare stray native-tool_use attempt fail
harmlessly (the tool isn't callable) and the model recover to a text envelope,
instead of the whole call hard-failing with `error_max_turns (stop=tool_use)`.
A startup banner prints the effective model / lock-down / context window so a stale
process (Python doesn't hot-reload) is obvious. Subagent backends use the same shape
with the shared per-call timeout (§5).

**Codex-specific limitation:** the Codex adapter uses a read-only CLI sandbox
and an envelope instruction; native shell access is not removed with the same
hard tool whitelist as Claude. The intended text-only role is therefore not an
equivalent enforcement boundary (`zero/llm/codex_cli.py`).

Registry actions are auditable and handled by the host's approval and rate-limit
paths. Persistent sessions reduce repeated prompt input; they do not add
harness-managed context compaction or guarantee deterministic replay.

**Backend Mode B — delegated** (used by execution mode `b`). Claude runs its own
loop; our catalog is exposed via an MCP server and the approval policy applies
inside each MCP tool implementation. **Pros:** native tool-calling, the CLI handles
history/streaming. **Cons:** less tight control; harder to replay.

### 3.6 Tool layer

Each tool (`zero/tools/`) is a thin wrapper around a binary, library, or internal
capability. Tools declare a Pydantic `Args` model, a `RiskLevel`, and flags.

**RiskLevel** (`zero/tools/base.py`): `safe` (read-only / local disk),
`active` (touches the target — probes/fuzz/scans), `exploit` (proves an exploit
— SQLi, IDOR write), `destructive` (writes/deletes target data). Tools also carry
`runs_on_host` (local code/binary execution; remoted wrappers clear this flag) and
`requires_approval` (local no-side-effect tools like `burp_traffic`
bypass the gate).

**Catalog (registered in `default_registry`):**

| Tool | Backed by | Risk | Purpose |
|---|---|---|---|
| `burp_traffic` | local Burp XML | safe (no prompt) | Query a Burp export: `list_hosts` / `search` / `get`. |
| `http_replay` | `httpx` | exploit | Replay one HTTP request with modifications (write-method replays can prove exploitation). |
| `httpx` | `httpx` lib | active | Probe URLs / fingerprint. |
| `curl_raw` | `curl` | active | Escape hatch for arbitrary raw requests. |
| `sqlmap` | `sqlmap` binary | exploit | SQLi detection/exploitation. |
| `ffuf` | `ffuf` binary | active | Content discovery / parameter fuzzing. |
| `nuclei` | `nuclei` binary | active | Template-based scanning. |
| `nmap` | `nmap` binary | active | Port/service discovery. |
| `jwt_tool` | `jwt_tool` | active | Inspect / tamper JWTs. |
| `python_exec` | sandboxed python | active | Run a Python snippet. **Persistent per-actor session** — globals/imports/helpers survive across calls; each agent gets its own isolated interpreter. **No hard kill timeout:** code runs in a worker thread; if a call exceeds `wait_seconds` it KEEPS RUNNING and the tool returns "still running" + output-so-far, so the agent can `action="poll"` (wait more) or `action="cancel"` (stop + reset the session). Stops one slow call (e.g. a recursive `glob` over `/usr/share`) freezing the turn forever. |
| `pip_install` | pip | active | Install Python packages into the sandbox so `python_exec` can import them. Non-interactive, **hard-timeout** (won't wedge a session), package specs sanitized against option injection. Common libs (pdfminer.six, pypdf, bs4, cryptography, …) are pre-baked. |
| `python_bg` | sandboxed python | active | Detached background jobs with bounded captured logs, explicit lifecycle management, and TTL cleanup. Unlike `python_exec`, these jobs do not occupy an actor's persistent interpreter worker. Network reachability depends on the container/network configuration. |
| `list_dir` | sandboxed python | safe | Plain listing of a directory's sub-dirs + files (dirs first, trailing `/`; hidden shown; optional recursive). **Read-only, never prompts, hard-jailed to `--files-root`** (`/zero_files` in the sandbox) — can't escape via `..`/absolute/symlink. |
| `grep` | sandboxed python | safe | Regex-search files under the files root → `path:lineno: line` (recursive, `include` glob, `context`). Skips binary/oversized files. **Read-only, never prompts, hard-jailed to `--files-root`.** |
| `read_file` | sandboxed python | safe | Read a file's content with `offset`/`max_bytes` paging. **Read-only, never prompts, hard-jailed to `--files-root`.** |
| `memory_read` / `memory_write` | host file | safe (no prompt) | Each actor's bounded **working memory** (replace-model markdown) under `.pentest/memory/<id>.md`. Keyed by the injected `session_id`; runs **host-side, excluded from remoting** (see §3.14). |
| `todo_read` / `todo_write` | host file | safe (no prompt) | Each actor's **plan/checklist** (`{step,status}` list) under `.pentest/todo/<id>.json`; rendered live in the UI (§3.14). |
| `custom_tool_list` / `custom_tool_call` | engagement-local scripts | safe (list, no prompt) / active (call) | Shared listing/execution surface for stored custom tools (§3.6.2). |
| `web_search` | DuckDuckGo (`ddgs`) | safe | Search the public web. |
| `web_fetch` | `httpx` | safe | Fetch a public URL (scope-host blocklist applies). |
| `finding_submit` | internal | safe | Submit a candidate finding (orchestrator-intercepted). |

Plus the **orchestrator-only dispatch/inspection tools** (§3.3) and custom-tool
management (§3.6.2), added to the orchestrator's catalog at runtime, never in
`default_registry`.

Adding a tool is mechanical: drop a class in `tools/`, register it in
`default_registry`. No skill manifest.

#### 3.6.1 Web search and web fetch

The agents need public-web access (CVE lookups, PoCs, advisories, fingerprinting).
We own these as first-class tools rather than relying on the CLI's built-ins, so
behavior is identical in both backend modes and every call is audited.

- `web_search` — backed by **DuckDuckGo** via the `ddgs` library (no API key).
- `web_fetch` — `httpx` fetch. It is **blocked for any host in the engagement
  scope blocklist** (`--scope-host`): if the agent wants to talk to the target it
  must use `http_replay`/`httpx`, which go through auth, rate-limit, and capture.

> 🚧 **Partial / 📋 Roadmap — pluggable providers & caching.** The design called
> for a `WebSearchProvider` abstraction (Brave / Serper / SearXNG / DuckDuckGo)
> and an on-disk `web_cache`. Today only the DuckDuckGo provider is implemented,
> there is no provider-selection knob, and results are not cached to disk.

#### 3.6.2 Custom-tool storage and authority

`zero/tools/custom_tools.py` separates orchestrator-only management from the
registry tools available to all agents. Management calls are intercepted on the
host; `custom_tool_list` and `custom_tool_call` use the normal registry/remoting
path. Execution follows the registry approval policy and has a per-call timeout.

`zero/tools/_customtools.py` stores Python scripts under
`<files-root>/custom_tools/`, validates their syntax before writing, and derives
descriptions from module docstrings without executing them. Store paths use the
files-root resolver. This constrains storage paths, not the filesystem/network
permissions of an executing script. Scripts are shared through the files-root
mount and persist independently of `.pentest/`, including across Ralph resets.

### 3.7 Burp ingestion bridge

The agent's view of the world is its view of the HTTP traffic.

**Implemented — file export.** `burp_traffic` reads a Burp Suite **XML export**
(*Right-click → Save items*). The XML is parsed by `parse_burp_xml`
(`zero/traffic/burp_parser.py`, via `lxml`) into a `TrafficStore` of
`CapturedRequest` records. Loading is **lazy and tool-driven**, not a startup
step: the tool resolves a path from its args or auto-discovers a default
(`./burp/session.xml`, `./burp.xml`, `./burp/burp.xml`) and caches the parsed
store. A host directory can also be mounted into the sandbox at `/work/burp` via
`--burp-dir`.

`CapturedRequest` (`zero/traffic/store.py`) carries: `id, host, method, url,
status, request_headers, request_body, response_headers, response_body,
response_content_type, tags, captured_at, source`. The `burp_traffic` tool
searches it by host, url substring, status, content-type, tag, free text, or
method.

> 📋 **Roadmap — live ingestion.** Burp REST API polling (Burp Pro) and a
> Burp-extension push path (Community) are designed but not implemented; only
> file export works today.

### 3.8 Project context file (`pentest.md`)

An engagement-specific markdown brief, separate from repository development
guidance in `AGENTS.md`. `zero init` scaffolds it. The orchestrator
(`zero/orchestrator/context.py`) reads it and injects it into every agent's
system prompt (scope, credentials, prohibitions, notes).

```markdown
# Target: ACME Corp staging
## Scope
- IN:  *.staging.acme.example, api.staging.acme.example
- OUT: /admin/internal/*, prod (*.acme.example)
## Credentials
- user_low:  alice@test.local / Pa55w0rd!  (role: customer)
- user_high: bob@test.local   / Pa55w0rd!  (role: admin)
## Notes
- Multi-tenant SaaS; cross-tenant leakage is the #1 concern.
- Rate limit 100 req/min per IP. Do NOT run sqlmap --risk 3 / --level 5.
```

> 🚧 **Partial — scope is advisory, not a hard gate (see §3.9 & §8).**
> 📋 **Roadmap — credential substitution.** The design called for credentials to
> be referenced by name (`{{user_low}}`) and substituted at tool-execution time
> so the LLM never sees them in clear text. This is **not implemented**: the
> context (including any credentials written in `pentest.md`) is injected into the
> prompt as-is.

### 3.9 Approval gate

Registry calls use `ApprovalGate` (`zero/orchestrator/approval.py`) unless the
tool declares `requires_approval=False`. Orchestrator-only control operations
are intercepted separately. The risk-class auto-approval rules are:

| Risk | Behavior |
|---|---|
| `safe` | Prompt, unless `--yolo` or `--auto-approve safe`. Local no-side-effect tools (`burp_traffic`) skip the gate entirely. |
| `active` | Prompt, unless `--yolo` or `--auto-approve active`. |
| `exploit` | Prompt, unless `--auto-approve exploit` **and** `--i-understand`. |
| `destructive` | No risk-class flag (`--yolo` included) auto-approves it. |
| host-exec | A tool with `runs_on_host` prompts unless `--allow-host-exec`, even if its risk would otherwise be auto-approved. |

`Decision` is `YES | NO | ALWAYS` (`ALWAYS` adds the tool to the session
auto-approve set). The inline UI offers one-key `y` / `n` / `a`, with `Esc` =
deny (so the agent never deadlocks) or typing a reply + Enter.

**`--yolo` covers only `safe` + `active`.** It does **not** auto-approve
`exploit` (which still needs `--auto-approve exploit` *and* `--i-understand`) or
`destructive`, or host-execution checks by itself.

**Policy limitation:** session-wide per-tool approvals are evaluated separately
and can take precedence over the risk/host checks in `ApprovalGate`. Accordingly,
the risk-class table is not a guarantee that destructive or host actions always
prompt. No-approval tools and intercepted control operations are also outside
that table; the gate is not a universal sandbox boundary.

The prompt shows the tool, args, risk level, the subagent label (in mode `c`),
and a one-line "why" derived from the agent's thought:

```
  → approve tool call?  pentester[#3 /snippets]  tool: sqlmap  risk: exploit
    why: test the uid parameter for boolean SQLi
    args: {"request_id": "r12", "level": 2, "risk": 1}
    [y] yes   [n] no   [a] always-this-tool   (or type + Enter; Esc denies)
```

Concurrent subagents can raise several approvals at once; the UI shows one at a
time and buffers chat output so the prompt stays put.

> 🚧 **Partial — `EDIT` decision.** A `Decision.EDIT` ("let me edit the args")
> exists in the enum but is **not wired into the inline UI**; only y/n/a/Esc are
> reachable today.
> 📋 **Roadmap — hard scope blocking.** The original design had the gate
> `BLOCK` any out-of-scope tool call. The gate does **not** implement a general
> scope check. The only scope enforcement that exists is the `web_fetch`
> host-blocklist (`--scope-host`). Out-of-scope discipline otherwise relies on the
> system prompt + the per-call approval gate. See §8.

### 3.10 Agent-initiated user questions (`ask_user`)

Any agent can pause and ask the user a question — a first-class envelope shape
(§3.5.1), not a tool. Useful when the capture is ambiguous ("which of two login
endpoints is real?"), an OOB step needs input, or the user has target knowledge
not in `pentest.md`.

```json
{"type": "ask_user", "question": "...", "options": ["..."], "free_form": true, "blocking": true}
```

- `options` render as numbered choices (pick by digit); `free_form` lets the user
  type a custom answer.
- `blocking: true` halts that agent's loop until answered.

**Noise control** (`AskUserGovernor`): a per-agent cap (10 for Pentest, 3 for
Validator), near-duplicate suppression (a repeated question is auto-answered from
the prior answer), and a system-prompt nudge to prefer trying a tool over asking.

**`--yolo`** auto-answers **non-blocking** questions (with an empty
"use your judgment" answer) and still defers **blocking** ones to the user.

Every question and answer is appended to `.pentest/audit.jsonl`.

### 3.11 Rate limiting

`RateLimiter` (`zero/orchestrator/rate_limit.py`) enforces a per-host token
bucket. Default `--rate-limit 100/min` (`<int>/sec` or `<int>/min`); `0/sec`
disables it. The host is extracted from common arg shapes (`url`, `target`) and
the limiter is acquired before the tool runs.

This meters tool invocations with recognized host arguments, not every network
request made inside a binary or script. It is not a network-wide request quota.

### 3.12 Audit, thinking log, replay

- **`audit.jsonl`** — append-only JSONL of every prompt, envelope, tool proposal,
  approval decision, observation, dispatch, finding, and `ask_user`. Some recorded
  outputs are truncated; this is an audit trail, not a complete traffic capture.
- **`thinking.log`** — a human-readable per-turn reasoning trace for the
  orchestrator and every subagent.
- **Model traces** — `.pentest/llm/` stores per-actor model input/output, enabled
  by default. Context and logs may contain credentials; redaction is not built in.
- **`ReplayBackend`** — returns recorded assistant messages from `agent_step`
  events in order. Replay is not a deterministic simulation of tool results.

### 3.13 Terminal UI

An **inline** prompt_toolkit UI (`zero/ui/app.py`) — not a full-screen TUI. The
transcript prints to normal scrollback (native scroll/copy work); only a small
input prompt + a status toolbar are redrawn at the bottom. Each turn shows a
**concise reasoning summary** (the agent's `thought`, Codex/Claude-Code style). The
toolbar shows each running subagent with its goal, elapsed time, and **context-window
usage** (`ctx used/window %`), plus the orchestrator's own ctx in the status line and
its **todo checklist** (`☐/◐/☑`); ↑/↓ select a subagent, Enter expands it. A startup
banner prints the effective backend config (model · tool lock-down · context window).
Subagent reasoning/todo are kept out of the main chat (they carry a label). Slash
commands (`/running`, `/exit`, …) are handled locally without hitting the LLM.

### 3.14 Per-agent working state: memory & plan

Every actor (the orchestrator and each pentester/validator subagent) keeps two
bounded files of working state, so progress survives a long run, a re-dispatch, or a
crash:

- **Memory** (`memory_read`/`memory_write` → `.pentest/memory/<id>.md`) — a running
  summary the agent overwrites (replace-model, so it never grows like the transcript).
- **Plan** (`todo_read`/`todo_write` → `.pentest/todo/<id>.json`) — a checklist of
  `{step, status}`, rendered live in the UI.

Both are keyed purely by the per-actor `session_id` the orchestrator injects in
`_invoke_tool` (the LLM never sets it — the same mechanism as `python_exec`), so
these tools address only the actor's own files. They run **host-side and are
excluded from sandbox remoting**. Their storage under `.pentest/` is still inside
the engagement directory mounted at `/work`; tool-level ownership is not general
filesystem isolation. At the start of a run each agent is **seeded** with its own
memory, and the orchestrator can read any subagent's via `read_subagent_memory` /
`list_memories` (§3.3).

**Harness auto-write (safety net).** In practice models rarely call `memory_write`
on their own, so the harness also persists each subagent's **end-state** (final
status + summary + discovered creds/tokens, or the validator's verdict + rationale)
to its memory on completion — merged below any notes the agent wrote. This is what
makes `read_subagent_memory` reliably useful even when a subagent produced a thin
final or died.

> 📋 **Roadmap — harness-managed token-budget auto-compaction** of agent context
> is still not implemented. Scratchpads and backend CLI session management are
> separate from automatic history summarization in the harness. See §9.

### 3.15 Ralph planning and iteration

`--ralph` selects `RalphDriver` (`zero/ralph/driver.py`) instead of the top-level
`a`/`b`/`c` loop. It composes the existing agent and orchestrator primitives:

1. An **analyzer** and **analyzer_validator** review a task plan (PRD), with
   epic-level grouping enabled by default. The operator plan checkpoint is also
   enabled by default. Planning review is bounded and is not a guarantee of
   complete coverage.
2. Each iteration starts a fresh orchestrator session with the plan and durable
   progress, performs a bounded pass, and records evidence in task dossiers. The
   analyzer_validator reviews task completion separately from the finding
   validators. The driver stops at the iteration limit or when the plan's tasks
   are marked complete.
3. The driver snapshots each pass and writes a cumulative findings report.

`zero/ralph/prd.py` defines the plan model; `progress.py` and `dossier.py` retain
the journal and per-task evidence under `.ralph/`. `state.py` manages
`.ralph_logs/.pentest_<n>/` snapshots and `.ralph_logs/ralph_report.md`. Between
passes, findings and the open master audit/thinking logs carry forward; other
`.pentest/` working state is reset. Custom tools live outside that reset root.

Resume loads a usable saved PRD, retains task status/progress, skips the initial
planning/checkpoint, and continues snapshot numbering. If no usable plan exists,
it falls back to fresh planning. Each pass has a fresh subagent manager, so its
total-subagent cap is per pass rather than a cumulative Ralph budget.

---

## 4. Containerization

The **tool execution environment** is a single Docker container; the agent loops
stay on the host (§2). Container support is ON by default and falls back to host
execution if sandbox startup fails (including missing Docker or image).

**Why Docker:** `sqlmap`/`nmap`/etc. make many network calls and leave artifacts;
pinned tool versions per release; one-command install.

**Image layout** (`docker/`): a Dockerfile plus vendored tools; the container
runs a small RPC server (`zero/container/rpc.py` ↔ host `ContainerRPC`) that
executes tool calls and returns results over HTTP. (A unix-socket transport was
attempted but HTTP is used because UDS-in-shared-volume is unreliable on Docker
Desktop / macOS.) The image also bakes **wordlists** — `seclists`, `dirb`/
`dirbuster`, and an unpacked `rockyou.txt` under `/usr/share/wordlists` — so
`ffuf` (default wordlist `/usr/share/wordlists/dirb/common.txt`), `sqlmap`, etc.
work out of the box. Changing a baked tool/wordlist requires rebuilding the image.

**Mounts:**
- `./` (project dir) → `/work` read-write — `pentest.md`, findings, logs.
- `~/.claude/` → `/root/.claude` read-only when that host directory exists;
  `ContainerSpec` resolves it automatically. This mount does not move the model
  CLI or agent loops into the container.
- `--burp-dir` → `/work/burp` read-only.
- `--files-root` (default `./zero_files`, resolved against cwd, auto-created) →
  `/zero_files` read-write — the **jail root** the read-only fs tools
  (`list_dir`/`grep`/`read_file`) are confined to, advertised via the
  `ZERO_FILES_ROOT` env. Those tools can never read outside it (`..`/absolute/symlink
  escapes are rejected) — a hard boundary, unlike the advisory `--scope-host`.

**Network:** bridge by default; `--host-network` for talking to Burp on
`127.0.0.1`; otherwise `host.docker.internal` (with `--add-host` on Linux).

**Resource limits:** `--container-cpus` / `--container-memory` (default 4 CPUs /
4 GB). Binary tools also carry a per-invocation wall-clock timeout (default
300 s / 5 min, configurable per tool).

**Tool remoting:** by default registry tools are proxied into the container
(`--remote-all-tools`), except host-side memory/todo tools. Intercepted dispatch,
inspection, custom-tool management, and finding handoffs run on the host.
`--no-remote-all-tools` proxies only `python_exec`. The read-only filesystem tool
jail does not constrain arbitrary execution tools or hide the `/work` mount.

---

## 5. Subagent lifecycle & resource bounds (mode `c`)

Subagents are deliberately bounded so a single attack idea can't run away. The
limits compose:

| Knob (CLI flag) | Default | What it bounds |
|---|---|---|
| `--llm-timeout` | 900 s | Wall-clock for **one** model CLI call in modes `a`/`c` — shared by the orchestrator and pentester/validator backends. |
| `--pentester-max-turns` | 14 | Per-pentester turn budget. Pentesters do **not** inherit the orchestrator's `--max-turns`. |
| `--subagent-deadline` | 1200 s | **Total** wall-clock per subagent, checked between turns (pentesters and validators). |
| `--validator-max-turns` | 40 | Validator investigation steps (our loop). |
| `--validator-claude-max-turns` | 4 | Claude's internal per-call turn budget; distinct from validator investigation steps and not used by Codex. |
| `--max-subagents` | 10 | Max concurrent subagents (semaphore). |
| `--max-total-subagents` | 0 (unlimited) | Total dispatches for one subagent manager; a fresh manager is created per Ralph pass. This is not a token or monetary budget. |
| `--max-turns` | 10 | The **orchestrator's** own turn budget (turn-awareness pacing toward `finalize`). |

The subagent deadline is checked *between* turns; it does not cancel an in-flight
approval wait or tool call. The model timeout only bounds a model call, and tools
have their own timeout/yield behavior. These controls therefore do not establish
a strict end-to-end wall-clock bound. When the loop observes an expired deadline,
it returns an error or pending outcome rather than starting another turn.

**Operator can interject while subagents run.** `await_subagents` races the
subagent wait against operator input: if the user types a message while the
orchestrator is blocked waiting, the await returns early (with whatever finished),
the running subagents keep going, and the orchestrator reads + responds to the
message before resuming. This keeps the chat responsive instead of frozen for the
whole batch.

**Turn-error recovery (orchestrator AND subagents).** A backend timeout, a
CLI failure, or a parse error on a turn no longer immediately kills the loop.
The orchestrator records `turn_error` and retries with corrective feedback; after
repeated consecutive failures it goes idle for the next prompt. **Subagents also
recover:** the pentester
loop and the validator driver catch a `loop.step()` error, feed it back as a
corrective observation ("emit one JSON envelope as text; don't call tools natively"),
and **retry the turn** — only giving up after a few consecutive failures, and then
*gracefully* (an `ERROR` result with its end-state written to memory, or — for the
validator — re-raising so the finding is parked pending *with the error in its
notes*). Previously a single backend glitch turned the whole subagent into `ERROR`
and discarded its work.

**Shutdown.** The session ends only when the UI exits. On exit the orchestrator
cancels in-flight subagents. `finalize` (or hitting the turn budget in mode `c`)
attempts to drain running work before writing the report. `_emit_report` limits
the subagent wait to 600 seconds and the background-validation wait to 10 seconds;
it can write a report after those waits expire, so a report is not proof that all
work finished.

---

## 6. Data Flow: a full example (mode `c`)

User goal: *"Find access-control bugs in the orders API."*

1. User browses the staging app in Burp as `alice` then `bob`, and exports to
   `./burp/session.xml`.
2. `zero run --goal "Find access-control bugs in the orders API"`.
3. Orchestrator loads `pentest.md`, starts the sandbox, and swaps the tool
   registry for RPC-proxied tools.
4. Orchestrator does recon directly: `burp_traffic.search(host=…, url_substring="/orders")`
   → 14 requests across two users; it spots a sequential `order_id` and an
   `X-Acme-Tenant` header.
5. It `dispatch_pentester(attack_idea="IDOR via order_id", target="/api/orders/{id}")`
   and a second for the tenant header — both run concurrently.
6. A pentester replays Alice's request with Bob's cookie; the approval gate
   prompts; the user accepts. It returns `status: EXPLOITED` with a candidate
   finding and `discovered: ["tenant b0c1 data leaks when header swapped"]`.
7. The orchestrator `await_subagents` (returns on the first completion), reacts,
   and `dispatch_validator(...)` on the candidate. The Validator re-proves it
   from scratch, scores CVSS 4.0, returns `confirmed`. The finding is persisted to
   `findings/confirmed/<id>.md`.
8. The orchestrator chains the tenant-header discovery into a new pentester, and
   so on, until it `finalize`s (or hits `--max-turns`): remaining subagents drain,
   `report.md` is written, and the session goes idle for the next prompt.

---

## 7. Extensibility

- **New tool:** add a class in `tools/`, declare `Args`/`risk_level`/flags,
  register in `default_registry`. Nothing else changes.
- **New LLM backend:** implement `LLMBackend` and register. Modes `a`/`c` already
  support Codex and Claude; a raw API adapter remains roadmap work.
- **New agent role:** the dispatch model makes adding roles (e.g. a Reporter or
  Triage agent) a matter of a new subagent kind + prompt.
- **New traffic source:** feed a `TrafficStore` (📋 ZAP / mitmproxy / HAR).

---

## 8. Security & ethical considerations

- **Approval policy is the primary operator control.** Registry tools follow
  risk/host rules, with no-approval declarations and session-wide per-tool
  decisions. Intercepted operations are separate; see §3.9 for limitations.
- **Sandboxed tool execution** by default (Docker), with host-exec clearly warned
  about at startup when the sandbox is off/unavailable.
- **Rate limiting** per target host (§3.11).
- **Audit trail and model traces** of run activity, with some outputs truncated
  (§3.12).
- **Artifact locations:** findings/logs in `.pentest/`, Ralph state/snapshots in
  `.ralph/` and `.ralph_logs/`, and custom tools under the files root. These are
  storage conventions, not a universal write boundary; `/work` is writable.

> 🚧 **Scope enforcement is currently advisory, not mandatory.** Contrary to the
> original design, there is **no hard scope-blocking gate**. Scope from
> `pentest.md` is injected into the prompt, and `--scope-host` blocklists hosts for
> `web_fetch` only. Out-of-scope discipline otherwise depends on the system prompt
> and the per-call approval gate. A mandatory `violates_scope → BLOCK` check is
> roadmap (§9).
> 📋 **Credential redaction** (referencing creds by name and substituting at
> execution time) is also roadmap; today `pentest.md` content is sent to the LLM
> as-is.
> **Legal:** users are responsible for confirming authorization for every target
> they configure.

---

## 9. Roadmap (designed, not yet built)

| Area | Item |
|---|---|
| Backends | `ApiBackend` (pay-per-token); Codex and Claude CLI adapters already ship. |
| Web tools | Pluggable `web_search` providers (Brave / Serper / SearXNG); on-disk `web_cache`. |
| Burp | REST API ingestion (Pro); Burp-extension push (Community). |
| Scope | Mandatory `violates_scope → BLOCK` gate (allowlist semantics), not user-overridable. |
| Credentials | Reference-by-name (`{{user_low}}`) + execution-time substitution so the LLM never sees secrets. |
| Memory | Shared cross-finding validator history; harness-managed token-budget auto-compaction. (Per-agent memory/plan and Ralph task dossiers already ship — §3.14–3.15.) |
| Approval UI | Wire up the `EDIT` decision (edit args before approving). |
| Roles | Triage / Reporter agents (dedup across sessions; HackerOne/Bugcrowd report templates). |
| Platforms | Network-layer tools (smb/ldap), mobile (Frida), cloud (S3/IAM), basic binary. |

---

## 10. Open questions

1. Burp Community has no REST API — is extension-push the right "live" mode, or do
   Community users stay on file export?
2. Token-budget tuning once history summarization lands — the Validator's context
   is much narrower per finding than the Orchestrator's.
3. **Per-agent backend mode.** Intuition: Validator wants backend Mode A (rigor,
   per-step auditability), Pentester wants Mode B (exploration, native tool
   calling). Needs paired evals on a benchmark of intentionally-vulnerable targets
   before we pick defaults.
4. The right default for `--subagent-deadline` vs `--llm-timeout` — should a
   stuck in-flight call be hard-killed at the deadline (risking cutting off a
   legitimate long exploitation step), or left to finish as it is today?
