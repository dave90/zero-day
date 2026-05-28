# Architecture Design Document
## Penetration Testing & Bug Bounty Agent System

**Status:** Draft v0.1
**Document type:** Architecture Design
**Scope:** Initial release — Web application security

---

## 1. Overview

This document describes the architecture of an autonomous agent system designed to assist security researchers, penetration testers, and bug bounty hunters in finding vulnerabilities in web applications.

The system is conceptually similar to Claude Code, but optimized for offensive security work rather than software engineering. Unlike Claude Code, the architecture is intentionally **loose and extensible**: there is no rigid skill structure. Instead, the agent has access to a curated, growing list of security tools (e.g. `sqlmap`, `nmap`, `ffuf`, `nuclei`, `httpx`) and can decide which to invoke based on the task.

The system uses a **dual-agent design**:

- **Pentest agent** — discovers, exploits, and assesses vulnerabilities.
- **Validator agent** — independently verifies findings and assigns risk scoring.

The two agents iterate together until a finding is either confirmed, rejected, or flagged for human review.

### 1.1 Key design choices

| Decision | Rationale |
|---|---|
| Containerized execution (Docker) | Isolates offensive tooling from the host; prevents accidental damage to the user's machine. |
| Burp Suite as the traffic source (not MCP browser) | MCP browsers are unreliable, slow, and poor at authenticated flows. Burp is the de-facto tool pentesters already use. |
| LLM backed by Claude Code CLI (`-p --output-format json`) | Avoids forcing the user to pay for API credits; reuses their existing Claude subscription. |
| Pluggable LLM backend | Same interface can wrap Codex CLI, Aider, or other coding agents. |
| Human-in-the-loop by default | Every executed command requires user approval unless `--yolo` (or equivalent) is set. |
| Project context file (`pentest.md`) | Mirrors `CLAUDE.md` — lets the user define scope, credentials, rules of engagement. |

### 1.2 Non-goals (v1)

- Network-layer / Active Directory pentesting (future).
- Mobile or binary exploitation (future).
- Replacing the human pentester. The system is an assistant, not an autonomous attacker.
- Running outside Docker. The host is never a supported execution environment.

---

## 2. High-Level Architecture

```
                       ┌─────────────────────────────┐
                       │         User (CLI)          │
                       │  - approves commands        │
                       │  - reviews findings         │
                       │  - writes pentest.md        │
                       └──────────────┬──────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────┐
│                       Orchestrator (host)                        │
│  - Loads pentest.md (scope, creds, rules)                        │
│  - Spawns Docker container                                       │
│  - Routes messages between agents                                │
│  - Enforces approval gate on tool calls                          │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│                   Docker container (sandbox)                     │
│                                                                  │
│  ┌────────────────┐         ┌────────────────┐                   │
│  │ Pentest Agent  │ ◄─────► │ Validator Agent│                   │
│  │ (LLM loop)     │ findings│ (LLM loop)     │                   │
│  └───────┬────────┘         └────────────────┘                   │
│          │                                                       │
│          ▼                                                       │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                       Tool Layer                           │  │
│  │  burp_traffic  http_replay  sqlmap  nmap  ffuf  nuclei     │  │
│  │  httpx  curl  jwt_tool  custom_python  notes  ...          │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │              LLM Backend Adapter (pluggable)               │  │
│  │     ClaudeCodeBackend  │  CodexBackend  │  ...             │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
               ▲
               │ Burp proxy export (JSON / .saz / live API)
               │
       ┌───────┴───────┐
       │  Burp Suite   │ ◄── User browses target manually
       └───────────────┘
```

---

## 3. Components

### 3.1 Orchestrator

The orchestrator lives on the host. It is the only component the user interacts with directly.

**Responsibilities:**

- Parse CLI arguments and the `pentest.md` project file.
- Build / start the Docker container with the right volume mounts.
- Maintain a message bus between the Pentest agent, the Validator agent, and the user.
- Enforce the **command approval gate**: every tool invocation produced by the Pentest agent is shown to the user for confirmation, unless `--auto-approve` (a.k.a. `--yolo`) was set. The Validator agent's tool calls follow the same rule.
- Route **agent-initiated questions** to the user when an agent emits an `ask_user` envelope, and feed the answer back as the next observation (see §3.9).
- Persist conversation logs, findings, and artifacts to `./.pentest/` in the host project directory.
- Surface the agent's reasoning, planned actions, and pending approvals in the terminal UI.

**Key interfaces:**

```
Orchestrator
 ├── ProjectLoader(pentest.md) → ProjectContext
 ├── ContainerManager → docker run / exec / stop
 ├── AgentRouter → routes between PentestAgent and ValidatorAgent
 ├── ApprovalGate(command, risk_level) → bool
 └── EventLog → JSONL stream of every step
```

### 3.2 Pentest Agent

The discovery and exploitation agent.

**Inputs:** project context, captured HTTP traffic, user's natural-language goal (e.g. "look for IDORs in the orders API").

**Loop:**

1. Read the latest user goal and any prior findings.
2. Inspect HTTP traffic via the `burp_traffic` tool.
3. Form a hypothesis ("the `order_id` parameter looks predictable; let me try IDOR").
4. Propose tool calls (e.g. `http_replay` with a tampered parameter, or `sqlmap` on a specific endpoint).
5. Wait for user approval (unless `--auto-approve`).
6. Observe results.
7. Either iterate, or emit a **Finding** object and hand off to the Validator.

**Output: Finding object**

```json
{
  "id": "F-2026-0007",
  "title": "IDOR in GET /api/orders/{id}",
  "endpoint": "GET https://target.example/api/orders/{id}",
  "evidence": {
    "requests": ["req-id-1", "req-id-2"],
    "diff": "Changing order_id from 1042 -> 1041 returns another user's data"
  },
  "exploit_steps": [...],
  "preliminary_risk": "High",
  "cwe": "CWE-639",
  "status": "awaiting_validation"
}
```

### 3.3 Validator Agent

A second, independently prompted agent. Its job is to **distrust the Pentest agent's findings** and either confirm or reject them.

**Loop:**

1. Read the Finding object.
2. Re-derive the proof: re-issue the requests, examine the response diff with fresh eyes, check that the second user account is actually different, that the data really belongs to another tenant, etc.
3. Check the finding against the scope defined in `pentest.md` (out-of-scope findings are downgraded).
4. Assign a final risk score using CVSS 4.0 (or 3.1 if preferred), considering exploitability, impact, prerequisites, and business context from `pentest.md`.
5. Return a **Validation** object back to the Pentest agent.

**Tool access — same surface as the Pentest agent.**

The Validator gets the full tool catalog from §3.5, *minus* `finding_submit` (it produces `Validation` objects instead). It needs the same surface — `sqlmap`, `nmap`, `ffuf`, `nuclei`, write-method `http_replay`, `python_exec`, `jwt_tool`, `curl_raw`, etc. — because **many exploits can't be proven without running them**: you can't independently confirm an SQLi without firing the payload yourself; you can't confirm an IDOR write without issuing the PUT or DELETE; you can't confirm an SSRF without watching the OOB callback land. A read-only validator would be forced to take the Pentest agent's word for any active-only finding, which defeats the point of the second pass.

The "distrust" property lives elsewhere — specifically in (a) the system prompt, (b) the cross-agent isolation where only structured `Finding` and `Validation` objects cross the boundary (the Validator never sees the Pentest agent's chain-of-thought), and (c) the Validator's instruction to set up its own test conditions (its own accounts, its own payloads, its own clocks) rather than literally replaying the Pentest agent's requests. Tool restriction is the wrong knob for this — it would make the Validator weaker without making it more independent.

Validator tool calls still flow through the §3.8 approval gate, so the user can stop a destructive re-test the same way they can stop a Pentest agent re-test.

**Memory model: shared conversation history.**

The Validator keeps its own conversation history *across* findings within a single session, the same way Claude Code preserves history within an interactive session. This means:

- When the Validator looks at finding #3, it remembers what it confirmed/rejected for findings #1 and #2 — useful for spotting duplicates and reusing context (e.g. "I already verified Bob's account leaks data; I don't need to re-verify the auth setup").
- The Pentest agent likewise keeps its own history across the session.
- The two agents **do not** share each other's histories. They communicate only through structured `Finding` and `Validation` objects passed through the orchestrator. This preserves the adversarial separation (the Validator is not influenced by the Pentest agent's chain-of-thought) while still letting each agent build up context within its own role.
- Sessions are bounded by a configurable token budget. When approached, the orchestrator summarizes older turns into a compressed memory blob (same pattern Claude Code uses for auto-compact) so the agents keep useful context without blowing the context window.

**Output: Validation object**

```json
{
  "finding_id": "F-2026-0007",
  "verdict": "confirmed | rejected | needs_more_evidence",
  "cvss_vector": "CVSS:4.0/AV:N/AC:L/...",
  "cvss_score": 8.1,
  "notes": "Confirmed cross-tenant data access. Reproduced with two accounts.",
  "follow_ups": ["Check PUT and DELETE on the same endpoint"]
}
```

If the verdict is `needs_more_evidence` or `rejected`, the Pentest agent reads the notes and tries again (bounded by a max-iterations setting to prevent infinite loops).

#### 3.3.1 Findings store

Findings live in `./.pentest/findings/` as one file per finding, written by the orchestrator after the Validator returns its verdict. The directory is the source of truth for everything downstream (the user-facing UI, the final report, deduplication, and cross-session memory).

**File layout:**

```
.pentest/
├── findings/
│   ├── confirmed/
│   │   ├── F-2026-0007.md     # human-readable + frontmatter
│   │   └── F-2026-0011.md
│   ├── rejected/
│   │   └── F-2026-0009.md     # kept for dedup, NOT shown to user
│   └── pending/
│       └── F-2026-0012.md     # awaiting validator
└── findings.index.json        # hashes for dedup
```

**File format (`F-2026-0007.md`):**

```markdown
---
id: F-2026-0007
status: confirmed
title: IDOR in GET /api/orders/{id}
endpoint: GET https://api.staging.acme.example/v1/orders/{id}
cwe: CWE-639
cvss_vector: CVSS:4.0/AV:N/AC:L/...
cvss_score: 8.1
risk: High
dedup_hash: 7a3f...c2e1
created: 2026-05-28T14:22:11Z
validated: 2026-05-28T14:24:03Z
---

## Description
Changing the `order_id` parameter returns another user's order data...

## Exploit
1. Authenticate as user A.
2. Send `GET /api/orders/1041` (an ID belonging to user B).
3. Observe user B's order body returned.

## Evidence
- Request: req-id-1
- Response: 200 OK with cross-tenant fields...

## Validator notes
Confirmed cross-tenant data access. Reproduced with two accounts.

## Follow-ups
- Check PUT and DELETE on the same endpoint.
```

**Two rules drive the design:**

**Rule 1 — Findings the Validator rejects are never shown to the user.** They are written to `findings/rejected/` instead. The user-facing terminal UI, the final report, and any external integrations only read from `findings/confirmed/`. Rationale: the whole point of the second agent is to filter out the Pentest agent's hallucinations and overclaims before the user sees them. If we showed rejected findings, we'd reintroduce exactly the false-positive noise the Validator exists to remove.

Rejected files are kept on disk (not deleted) for two reasons: they fuel deduplication (see Rule 2), and they're useful for debugging ("why did the agent flag this and then reject it?"). The user can opt into seeing them via `--show-rejected` for review purposes, but they never appear in the default UI or the report.

`needs_more_evidence` findings live in `findings/pending/` and are not shown to the user either, until the Pentest agent comes back with enough evidence for the Validator to flip the verdict to `confirmed`.

**Rule 2 — Deduplication is file-based.** When the Pentest agent is about to submit a new finding, the orchestrator computes a `dedup_hash` over a normalized tuple:

```
(normalized_endpoint, cwe, payload_shape, http_method)
```

where:
- `normalized_endpoint` strips IDs and query values (`/orders/1041` → `/orders/{id}`).
- `payload_shape` is a structural fingerprint of the exploit payload (e.g. SQLi via `' OR 1=1--` and `' UNION SELECT…` share the same shape `sqli/boolean-or-union`).

Before the Pentest agent's `finding_submit` reaches the Validator, the orchestrator checks `findings.index.json`:

- **Hash matches a `confirmed` finding** → silently dropped, the Pentest agent gets back `{ "observation": { "result": "duplicate_of: F-2026-0007" } }` so it doesn't loop trying to "rediscover" it. Counts toward an internal stat but not toward the report.
- **Hash matches a `rejected` finding** → silently dropped with `{ "result": "previously_rejected: F-2026-0009, reason: <validator notes>" }`. This is important: it prevents the Pentest agent from re-submitting the same false positive ten times in a session.
- **Hash matches a `pending` finding** → merged into the pending one (evidence appended) rather than creating a duplicate.
- **No match** → proceeds to the Validator normally.

`findings.index.json` is rebuilt from the directory contents on startup, so the system is self-healing if the index gets out of sync with the files.

**Cross-session memory.** Because findings persist on disk between runs, a second session against the same target naturally inherits the dedup state — the agent won't waste cycles rediscovering yesterday's IDOR. The user can wipe state with `pentest reset --findings` or scope it per-target by running from a different working directory.

### 3.4 LLM Backend Adapter

The LLM is abstracted behind a single interface so the system can swap backends.

```python
class LLMBackend(ABC):
    @abstractmethod
    def run(
        self,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSchema],
    ) -> LLMResponse: ...
```

**Implementations:**

- `ClaudeCodeBackend` — shells out to `claude -p ... --output-format json` (Mode A) or registers an in-container MCP server and invokes the CLI in interactive/agent mode (Mode B). Default backend. Reuses the user's Claude subscription, no API key needed.
- `CodexBackend` — wraps `codex` CLI similarly, supporting both modes.
- `ApiBackend` — direct Anthropic / OpenAI API (Mode B only — there is no "CLI loop" to delegate to, so this backend uses native function-calling directly). For users who want to pay per token instead of using a subscription.

#### 3.4.1 Tool execution: two modes, one switch

There are two fundamentally different ways to plug our tool catalog into a coding-agent CLI like Claude Code or Codex. We don't yet know which produces better pentest outcomes, so the architecture supports **both, switchable per run** via a single config knob:

```yaml
# pentest.md or CLI flag
llm:
  backend: claude_code        # claude_code | codex | api
  execution_mode: orchestrator  # orchestrator | delegated
```

The `LLMBackend` interface is the same in both modes; only its `run()` implementation differs.

---

**Mode A — `orchestrator` (LLM as pure reasoner).**

The LLM CLI is treated as a stateless `prompt → JSON` function. Our orchestrator drives the ReAct loop and is the *only* component that executes tools.

1. The orchestrator passes the tool catalog (names, descriptions, JSON schemas) to the LLM **as data inside the prompt**, not as actual callable tools.
2. The system prompt instructs the LLM to respond in a strict JSON envelope with three possible shapes — tool call, user question, or final answer:
   ```json
   { "thought": "...", "action": { "tool": "sqlmap", "args": {...} } }
   ```
   ```json
   { "thought": "...", "ask_user": { "question": "...", "options": [...] } }
   ```
   ```json
   { "thought": "...", "final": "..." }
   ```
3. The system prompt also explicitly tells the LLM: *do not use any built-in tools you may have (WebSearch, WebFetch, Bash, etc.); route everything through the envelope.*
4. The orchestrator parses the JSON, runs the tool inside the Docker container, and feeds the result back as the next observation:
   ```json
   { "observation": { "tool": "sqlmap", "stdout": "...", "exit_code": 0 } }
   ```

Invocation:
```bash
claude -p "$PROMPT" --output-format json --max-turns 1
```

We use `--max-turns 1` to *guarantee* the CLI doesn't enter its own multi-step loop — we want exactly one reasoning step per invocation, then control back to us.

**Pros:** total control. Every action passes through our approval gate, scope check, rate limiter, audit log. Behavior is identical regardless of backend. Replayable.
**Cons:** verbose — we re-send the full history each turn. We re-implement loop control that the CLI already has. The model is told to *describe* tool calls in JSON rather than *invoke* them via its native function-calling, which is a slightly weaker training-data path.

---

**Mode B — `delegated` (LLM CLI runs its own tool loop).**

The LLM CLI is left in its native mode and runs its own multi-step loop. Our tool catalog is exposed to it via the CLI's tool-extension mechanism (MCP server, custom tool definitions, etc., depending on backend). The orchestrator becomes a thinner layer that bootstraps the session and post-processes results.

Per backend:

- **Claude Code:** the orchestrator starts an MCP server inside the container that exposes our tool catalog. Claude Code is invoked with that MCP server registered, plus a system prompt that says "use only these MCP tools and your built-in tools." Built-in tools like `WebSearch` may be left enabled or explicitly disabled via `--disallowedTools`. Claude Code runs its full loop and reports back when it hits a final answer.
- **Codex:** same shape, via Codex's own tool definition format.
- **API backend:** same shape, via native function-calling on the API.

**Pros:** less prompting overhead, uses the model's native tool-calling pathway, the CLI handles history/compaction/streaming for us. Likely faster and possibly higher-quality reasoning because the model is in its trained-on regime.
**Cons:** we lose tight control. The CLI may invoke tools in rapid succession before our approval gate or scope check can intercept (the gate has to be enforced *inside* the MCP tool implementation rather than around it). Behavior diverges across backends — each CLI compacts history differently, surfaces errors differently, etc. Replays are harder.

To keep the safety invariants from §3.8, the MCP tool implementations themselves call into the approval gate and scope check before doing anything observable. That means even in delegated mode, every tool call is still gated — it's just gated at the tool boundary rather than at the loop boundary.

---

**Why both:**

Mode A is the safer default — particularly for the first releases, where we want auditability and the ability to compare runs deterministically.

Mode B is likely better for raw effectiveness on long, exploratory engagements, because it lets the model use its native tool-calling pathway and the CLI's own context-management features (auto-compact, sub-agents, etc.).

The right answer is probably backend-specific and even task-specific (Validator agent in Mode A for rigor, Pentest agent in Mode B for exploration). We don't have data yet, so the architecture keeps both behind a single config switch and we'll let early users tell us what wins.

**What's the same in both modes:**

- `LLMBackend` interface.
- The tool catalog (same `tools/` directory, same schemas).
- The approval gate (§3.8) and scope checks — enforced at the loop boundary in Mode A, at the tool-implementation boundary in Mode B.
- The `ask_user` envelope / capability (§3.9) — implemented as a normal MCP tool in Mode B.
- The audit log — written by the same code path that enforces the gate.
- The Pentest ↔ Validator handoff (§3.3) — orchestrated above the backend, mode-agnostic.

This is why the `LLMBackend` interface remains a clean abstraction: the choice of mode is internal to the implementation, and the rest of the system (agents, tool catalog, orchestrator-level logic) doesn't change.

### 3.5 Tool Layer

Each tool is a thin Python wrapper around a binary, library, or internal capability. All tools live inside the Docker container.

**Catalog (v1 — web security focus):**

| Tool | Backed by | Purpose |
|---|---|---|
| `burp_traffic` | internal store | Query the captured Burp traffic (list / search / get full request/response). |
| `http_replay` | `httpx` / `requests` | Replay a single HTTP request with modifications. |
| `sqlmap` | `sqlmap` binary | SQLi detection / exploitation against a specific request. |
| `ffuf` | `ffuf` binary | Content discovery, parameter fuzzing. |
| `nuclei` | `nuclei` binary | Template-based vulnerability scanning. |
| `httpx` | `httpx` binary | Probe URLs, fingerprint tech stack. |
| `nmap` | `nmap` binary | Port/service discovery on in-scope hosts. |
| `jwt_tool` | `jwt_tool` | Inspect / tamper with JWTs. |
| `curl_raw` | `curl` | Escape hatch for arbitrary requests. |
| `python_exec` | python sandbox | Run a short Python snippet (e.g. crafting a payload, decoding a blob). |
| `web_search` | pluggable provider | Search the public web for CVEs, advisories, exploit writeups, vendor docs (see §3.5.1). |
| `web_fetch` | `httpx` | Fetch and parse a specific public URL (e.g. a CVE page, an HTML writeup, an API doc). |
| `notes` | local file | Append to the agent's scratch notebook. |
| `finding_submit` | internal | Emit a Finding object to the Validator. |

Each tool declares:

```python
@tool
class SqlmapTool:
    name = "sqlmap"
    description = "Run sqlmap against a specific request to test for SQL injection."
    schema = {
        "type": "object",
        "properties": {
            "request_id": {"type": "string", "description": "ID of a captured Burp request to test"},
            "level": {"type": "integer", "minimum": 1, "maximum": 5, "default": 1},
            "risk": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
            "extra_args": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["request_id"]
    }
    risk_level = "high"   # used by the approval gate

    def run(self, args, ctx) -> ToolResult: ...
```

Adding a new tool is mechanical — drop a file in `tools/`, register it, done. There is no skill manifest, no rigid metadata.

#### 3.5.1 Web search and web fetch

Penetration testing needs web access constantly: looking up CVEs by ID, finding exploit PoCs, reading vendor advisories, fingerprinting an unknown product, finding the auth flow docs for a third-party SSO provider, etc. So the agent needs a way to search and read the public web.

There are two ways to do this:

1. **Let the underlying LLM CLI handle it.** Claude Code (and similar) ship with their own `WebSearch` / `WebFetch` tools. We could simply leave those enabled when we shell out.
2. **Own the web tool ourselves**, as just another entry in our tool catalog.

We default to option **(2)** and we ship our own `web_search` / `web_fetch` as first-class container-side tools. The reason is that option (1) only works in Mode B (delegated) and would silently bypass our approval gate, scope check, rate limiter, and audit log. It also makes behavior diverge across backends — Claude Code has WebSearch, Codex has its own variant, the API backend has neither. The whole point of the LLM adapter is interchangeability.

By owning the tools, we get the same web-access behavior in both Mode A and Mode B (in Mode B, our `web_search` / `web_fetch` are exposed via the MCP server, and the system prompt disables the CLI's built-in equivalents via `--disallowedTools` or its analog).

That said, Mode B users who explicitly trust their backend can opt back into the CLI's built-in web tools via `llm.allow_builtin_web: true` — at the cost of losing the audit guarantees for those calls. Off by default.

**`web_search` provider — pluggable.**

```python
class WebSearchProvider(ABC):
    @abstractmethod
    def search(self, query: str, n: int = 10) -> list[SearchResult]: ...
```

Implementations:

- `BraveSearchProvider` — Brave Search API. Default. Has a generous free tier and a privacy-respecting ToS suitable for security research.
- `SerperProvider` — Serper.dev (Google results). Higher quality, paid.
- `SearxngProvider` — self-hosted SearXNG instance. The "no third party sees my queries" option. Recommended for sensitive engagements.
- `DuckDuckGoProvider` — no key needed, lower quality, useful for the zero-config first run.

The provider is selected via `pentest.md` or a CLI flag. No provider is mandatory — the user picks based on their privacy and quality needs.

**`web_fetch`:**

A thin wrapper around `httpx` that:

- Resolves the URL and downloads the body, capped at a configurable size (default 5 MB).
- Strips boilerplate HTML and extracts the main text (via `trafilatura` or `readability`), so the agent doesn't waste tokens on navigation menus.
- Returns both the cleaned text and the original headers + status, so the agent can see if a page was a redirect, a 404, etc.

**Critical: scope rules apply to web tools too.**

A naive implementation would allow `web_fetch` to hit the target itself ("let me just fetch the site to confirm…") — which bypasses the entire scope/rate-limiting machinery in §3.6 and §3.8. So:

- `web_fetch` is **blocked** for any URL whose host falls inside the engagement scope. If the agent wants to talk to the target, it must use `http_replay` or `httpx`, which go through the proper auth, rate-limit, and capture pipeline.
- `web_search` queries that contain the target hostname are flagged (logged but not blocked — sometimes a researcher *does* want to find writeups about their own target).

**Caching:**

Both tools cache results to `./.pentest/web_cache/` keyed by query / URL. This is cheap and prevents the agent from re-fetching the same CVE page 5 times during a long session.

### 3.6 Burp ingestion bridge

This is the most critical part of the system, because the agent's view of the world is its view of the HTTP traffic.

**Why Burp:** Burp Suite is what real testers already have open. It handles auth, websockets, TLS interception, and complex modern SPAs that browser-based MCPs choke on.

**Three supported ingestion modes:**

1. **File export (simple, default).**
   The user does *Right click in Burp → Save items* → produces an XML file. The user points the orchestrator at it. The orchestrator parses and loads it into the traffic store.

2. **Burp's REST API (live).**
   Burp Suite Professional exposes a REST API on `http://127.0.0.1:1337/`. The orchestrator polls or subscribes and ingests requests in near real time. Best UX, but requires Burp Pro.

3. **Burp extension (push).**
   A small Burp extension (Jython / Montoya API) that POSTs every in-scope request to a local endpoint exposed by the orchestrator. Works on Burp Community.

All three feed into a single **TrafficStore** with this shape:

```python
class CapturedRequest:
    id: str                      # stable hash
    method: str
    url: str
    host: str
    headers: dict
    body: bytes
    response_status: int
    response_headers: dict
    response_body: bytes
    timestamp: datetime
    tags: list[str]              # e.g. "authenticated", "json-api", "graphql"
```

The `burp_traffic` tool lets the agent search this store: by host, by URL pattern, by status code, by content-type, by tag, by free text in the body.

### 3.7 Project context file (`pentest.md`)

Modeled on `CLAUDE.md` — a single markdown file at the project root that the user authors and the agents always read first.

```markdown
# Target: ACME Corp staging

## Scope
- IN: *.staging.acme.example, api.staging.acme.example
- OUT: anything matching /admin/internal/*, the WAF management UI, prod (*.acme.example)

## Out-of-scope vulns
- Self-XSS
- Missing security headers without demonstrable impact
- SPF / DMARC

## Credentials
- user_low:  alice@test.local / Pa55w0rd!  (role: customer)
- user_high: bob@test.local   / Pa55w0rd!  (role: admin)
- api_token_low: env:LOW_TOKEN

## Notes
- The product is a multi-tenant SaaS. Cross-tenant data leakage is the #1 concern.
- Rate limit is 100 req/min per IP — do not exceed.
- Do NOT run sqlmap with `--risk 3` or `--level 5`.
```

The orchestrator parses this into a `ProjectContext` and injects the relevant pieces into every agent turn's system prompt (scope, credentials by reference, prohibitions).

### 3.8 Approval gate

Every tool call from either agent flows through this gate before execution.

```python
def gate(tool_call: ToolCall, ctx: ProjectContext) -> Decision:
    if ctx.auto_approve and tool_call.risk_level in ctx.auto_approved_risks:
        return Decision.APPROVE
    if violates_scope(tool_call, ctx.scope):
        return Decision.BLOCK
    return Decision.PROMPT_USER
```

The user sees something like:

```
[Pentest agent] wants to run:

  Tool: sqlmap
  Target: GET https://api.staging.acme.example/v1/orders?id=1042
  Args: --level 2 --risk 1 --batch

Estimated risk: HIGH (active exploitation, may insert test data)
Approve? [y]es / [n]o / [a]lways for sqlmap on this host / [e]dit args
```

**Auto-approve flags (composable):**

- `--auto-approve safe` — read-only tools only (`http_replay` GETs, `httpx`, `nuclei` passive templates).
- `--auto-approve active` — adds `ffuf`, `nuclei` active templates.
- `--auto-approve exploit` — adds `sqlmap`, JWT tampering. **Requires `--i-understand`.**
- `--yolo` — everything in-scope, no prompts.

Out-of-scope calls are always blocked regardless of flags.

**Chained exploits are always allowed within scope.** The Pentest agent is free to follow a vulnerability chain (e.g. open redirect → SSRF → cloud metadata → IAM token) without any extra gate, as long as every hop stays inside the `pentest.md` scope. The reasoning: artificially capping chains makes the agent worse at finding the bugs that actually matter — real impact almost always comes from chaining — and the per-tool approval gate already gives the user a chance to stop any individual step. The first hop *outside* scope is hard-blocked by the orchestrator, no override.

### 3.9 Agent-initiated user questions

Either agent can pause and ask the user a question, the same way Claude Code does when it's unsure how to proceed. This is a first-class capability — not a tool, but a distinct envelope shape returned by the LLM (see §3.4.1).

**Why it matters for pentesting specifically:**

- The Burp capture often contains ambiguity the agent can't resolve from data alone ("there are two login endpoints, which is the real one?", "this header looks like a tenant ID — is it?").
- Out-of-band exploitation steps may require the user's input ("paste the OAuth callback URL after you click the link", "what's the OOB DNS listener domain you want me to use?").
- The user may have target knowledge that isn't in `pentest.md` ("is `userId=42` you, or another tester?").
- Better to ask than to guess and waste a long tool chain on the wrong assumption.

**Question envelope:**

```json
{
  "ask_user": {
    "question": "string — what the agent wants to know",
    "options": ["optional", "list", "of suggested answers"],
    "free_form": true,
    "blocking": true
  }
}
```

- `options`: if present, rendered as numbered choices the user can pick by number.
- `free_form`: if `true` (default), the user can type a custom answer even when `options` are shown.
- `blocking`: if `true`, the agent loop halts until the user answers. If `false`, the orchestrator may answer with a default ("skip / use your judgment") after a timeout — useful in `--yolo` mode.

**UX (terminal):**

```
[Pentest agent] needs input:

  ?  I see two login endpoints in the captured traffic:
       /api/login           (used by the web app)
       /auth/v2/login       (used by the mobile app, in the Burp capture)
     Which should I focus on?

  Options:
     1) /api/login
     2) /auth/v2/login
     3) both
  Your answer (number, or type a custom reply): _
```

**Rate limiting and noise control:**

To keep the agent from devolving into a 20-questions session, the orchestrator enforces:

- A configurable max-questions-per-session (default 10 for Pentest, 3 for Validator).
- A "you already asked this" check — if a near-duplicate question is asked, the orchestrator answers it from the prior answer's record and tells the agent in the next observation. This prevents loops where the agent forgets and re-asks.
- The system prompt explicitly tells the agent: *prefer trying a tool over asking, only ask when blocked or when an answer would save many tool calls.*

**Auto-answer mode:**

Under `--yolo`, `ask_user` envelopes with `blocking: false` are auto-answered as "use your best judgment" and logged. Blocking questions still block — `--yolo` does not silence them, because they exist precisely for cases where the agent itself decided it can't proceed safely without input.

**Audit:**

Every question and answer is appended to `./.pentest/audit.jsonl` alongside the tool calls. This means a session can be replayed deterministically, including the human-in-the-loop responses, which is useful for retesting after a fix is deployed.

---

## 4. Containerization

The entire execution environment is a single Docker container.

**Why Docker:**

- Tools like `sqlmap` and `nmap` make many HTTP/network calls and can leave artifacts on disk; the user does not want those on their host.
- Pinning tool versions per release.
- One-command install for users — they don't need to install 12 Go binaries.

**Image layout:**

```
docker/
├── Dockerfile               # base: kali-rolling or ubuntu + apt installs
├── entrypoint.sh
└── tools/
    ├── sqlmap/              # vendored at known commit
    ├── nuclei/
    ├── ffuf/
    └── ...
```

**Mounts:**

- `./` (project dir) → `/work` read-write — for `pentest.md`, findings, notes.
- `~/.claude/` → `/root/.claude` read-only — so the in-container `claude` CLI can use the host's auth (only if the user opts in).
- Burp export files → `/work/burp/` read-only.

**Network:**

- Default: bridge network, outbound only. No published ports.
- Optional `--host-network` flag for the (rare) case where the user wants the container to talk to Burp on `127.0.0.1` directly. Off by default; the more secure default uses `host.docker.internal`.

**Resource limits:**

- CPU and memory caps configurable via CLI flags, defaulting to 4 CPUs / 4 GB.
- A wall-clock timeout per tool invocation (default 5 minutes; configurable per tool).

---

## 5. Data Flow: A full example

User goal: *"Find access-control bugs in the orders API."*

1. User opens Burp, logs into the staging app as `alice`, browses the orders area, then as `bob`, browses the same area. Exports to `burp/session-1.xml`.
2. User runs `pentest run --goal "Find access-control bugs in the orders API"`.
3. Orchestrator loads `pentest.md`, starts the container, ingests the Burp export into TrafficStore.
4. Orchestrator spawns the Pentest agent with the goal.
5. Pentest agent calls `burp_traffic.search(host="api.staging.acme.example", path_contains="/orders")` and gets back 14 requests across the two users.
6. It notices that some requests carry a `X-Acme-Tenant` header and others don't, and emits an `ask_user` envelope: *"Some requests carry `X-Acme-Tenant: 7af2`. Is this the tenant scoping I should be tampering with, or just a tracing header?"* The orchestrator pauses the loop, shows the question to the user, who answers *"yes, that's the tenant ID — there are 3 tenants in staging: 7af2, b0c1, e9d4"*.
7. It calls `notes.append("Both users hit /api/orders/{id}. IDs look sequential. Worth tampering. Tenant header confirmed: 7af2.")`.
8. It calls `http_replay` to send Alice's request with Bob's session cookie (and vice versa), and a second replay swapping the tenant header to `b0c1`. The approval gate prompts; the user accepts.
9. Response shows Bob's order data returned with Alice's session, *and* tenant `b0c1`'s data returned when the header is swapped. Agent calls `finding_submit` — twice, one finding per issue.
10. Validator agent receives both findings, re-runs the requests itself, confirms, computes CVSS 4.0 for each, returns Validations.
11. Pentest agent reads the validations, marks both findings `confirmed`, and follows up on the Validator's hints (try `PUT` and `DELETE` on the same endpoint). Because chains are always in-scope, it then pivots on the tenant-header leak to look for cross-tenant write primitives without needing extra approval beyond the per-tool gate.
12. When the loop terminates (max iterations, user stop, or agent says `final`), the orchestrator writes a markdown report to `./.pentest/report.md`.

---

## 6. Extensibility

The architecture is loose on purpose. To extend:

- **New tool:** add a class in `tools/`, declare schema and risk level, register. No other change needed.
- **New LLM backend:** implement `LLMBackend` and register. The dual-agent loop is backend-agnostic.
- **New agent role:** the current pair is Pentest + Validator. A future `Triage` agent (deduplicates findings across sessions) or `Reporter` agent (writes the final markdown / HackerOne submission) plugs into the same message bus.
- **New traffic source:** implement the `TrafficSource` interface (e.g. ZAP, mitmproxy, HAR file). The agent code does not change — it always talks to TrafficStore.

---

## 7. Security & ethical considerations

- **Scope enforcement is mandatory.** The agent will refuse — and the orchestrator will block — any tool call whose target falls outside the `pentest.md` scope. This is not user-overridable mid-session.
- **No silent destructive operations.** Tools tagged `destructive` (e.g. anything that writes to the target) require explicit approval even under `--yolo`.
- **Credentials are never put in prompts verbatim by default.** They are referenced by name (`{{user_low}}`) and substituted at tool-execution time inside the container. This prevents the LLM provider from seeing them in clear text. Users can opt out if they trust the backend.
- **Rate limiting.** The orchestrator enforces a global outbound rate limit per target host (configurable in `pentest.md`).
- **Audit log.** Every prompt, response, tool call, approval decision, and tool output is appended to `./.pentest/audit.jsonl`. Useful for retesting, for disputes, and for writing the report.
- **Legal:** The system shows a one-time prompt on first run that the user must accept, confirming they have authorization to test the targets they configure.

---

## 8. Open questions

1. Burp Community has no REST API. Is the extension-push path good enough as the "live" mode there, or do we accept that Community users only get file-export?
2. Token budget tuning for the shared-history mode — what's the right summarization trigger for the Pentest agent vs the Validator? They probably need different thresholds because the Validator's context is much narrower per finding.
3. **Mode A vs Mode B (§3.4.1) — which performs better, and for which agent?** Pure intuition is that Validator wants Mode A (rigor, auditability per step) and Pentest wants Mode B (exploration, native tool-calling). But we won't know until we run paired evals on a benchmark set of intentionally-vulnerable targets. v0.1 ships both modes with Mode A as the default; v0.2 should add a small eval harness so we can compare.

---

## 9. Roadmap

| Phase | Scope |
|---|---|
| **v0.1 (this doc)** | Web security only. Burp file import. Claude Code backend. Pentest + Validator agents. Docker sandbox. |
| **v0.2** | Burp REST API ingestion. Codex backend. Findings deduplication. |
| **v0.3** | Burp extension push mode. HackerOne / Bugcrowd report templates. |
| **v1.0** | Network-layer tools (nmap deeper integration, smb, ldap). Triage agent. |
| **v1.x** | Mobile (Frida), cloud (S3/IAM misconfig scanners), binary (basic). |