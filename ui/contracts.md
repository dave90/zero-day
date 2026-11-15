# Studio contracts · version 1

## Shared boundary

`Orchestrator.run_headless(frontend, terminal_announcements=False)` initializes the same
registries, sandbox, router, approval gate, backend factories, and execution dispatch as the CLI.
`run_interactive()` attaches the existing renderer and retains terminal announcements. Structured
runtime/interaction metadata is emitted at its source. `WorkspaceOwnership` uses a nonblocking
OS advisory lock; both CLI/headless sessions use it, as does the web workspace index.

`submit_user_text(text, run_id=...)` accepts optional transport provenance. Actor tasks inherit
run context, so a late validator keeps its original goal ID. Decisions still resolve the core's
actual outstanding futures. Core `Decision.EDIT` remains unsupported.

## Local HTTP API

All routes use `/api/v1`. JSON is required for mutations. The service binds to loopback and checks
Host/Origin. GET `/health` establishes an HttpOnly SameSite=Strict local cookie and returns the
matching `csrf_token`; mutations also require `X-Zero-CSRF`. The dev proxy permits only the exact
configured loopback development origin. Request bodies are bounded to 6 MB.

| Route | Operation |
| --- | --- |
| `GET /health`, `/capabilities` | Schema version, binary presence, local CSRF bootstrap, fixture status, context template |
| `GET /config-schema` | All 38 CLI fields and effective defaults |
| `GET /models?backend=codex` | Dynamic installed-Codex model catalog; `refresh=1` bypasses the short service cache |
| `POST /config/validate` | Strict types/mode/rate/path validation and quoted command export |
| `GET/POST /engagements` | List lightweight sidebar state / create dedicated conversation directory |
| `GET/PATCH /engagements/{id}` | Metadata, archive, draft and next-session configuration |
| `DELETE /engagements/{id}` | Exact `confirm_name` required; `close_session: true` required for an open runtime. Close only owned workers, delete contained engagement files, then transactionally remove sessions/events/commands and the index row |
| `GET/POST /templates` | List setup snapshots / save from `source_id`, `name`, optional boolean `include_context` (default false) |
| `DELETE /templates/{id}` | Remove a setup snapshot; attacks created from it remain independent |
| `GET/PUT /engagements/{id}/context` | Context plus revision; PUT requires current revision |
| `GET/POST /engagements/{id}/files` | Inventory / contained text or base64 binary import |
| `POST /engagements/{id}/sessions` | Immutable config/context capture, preflight, worker startup; requires `idempotency_key` |
| `GET /sessions/{id}/snapshot` | Durable projection through sequence N, config, context, runs, history provenance |
| `GET /sessions/{id}/events?after=N` | SSE; also accepts Last-Event-ID |
| `POST /sessions/{id}/messages` | Queued operator guidance / new goal; requires idempotency key |
| `POST /sessions/{id}/cancel-turn` | Actual model-turn cancellation result; other work can continue |
| `POST /sessions/{id}/close` | Bounded worker cleanup and sealing |
| `POST /sessions/{id}/approvals/{request}` | `decision: yes/no/always`; eligible session grants only |
| `POST /sessions/{id}/questions/{request}` | Option/free-form `answer` checked against the outstanding contract |
| `POST /sessions/{id}/plans/{request}` | `decision: approve/revise/abort`, exact `revision`, optional feedback |
| `POST /sessions/{id}/respond-now` | Existing live Swarm consolidation operation |
| `GET /engagements/{id}/findings` | Core finding records, validator result, artifact IDs |
| `GET /engagements/{id}/artifacts` | Contained, server-owned artifact inventory |
| `GET /engagements/{id}/artifacts/{artifact}` | Inert text preview; `download=1` downloads original bytes |
| `GET /engagements/{id}/runs`, `/conversation` | Immutable sessions/goal records / persistent conversation history |
| `GET /engagements/{id}/swarm` | Read-only SQLite coordination projection |
| `GET /engagements/{id}/logs` | `kind`, `q`, `actor`, `offset`; 100 matching records/page |
| `POST /engagements/{id}/recordings` | Validate and save recorded JSONL without execution |
| `POST /engagements/{id}/replay` | Separate reviewed core-replay session from a registered recording |
| `POST /engagements/{id}/reset` | Explicit `scope: .pentest`; reject while active |

Artifact/log/coordination reads accept `session_id` to select sealed historical state. IDs are
resolved against that engagement's inventory. No endpoint accepts an unrestricted host path.
Paths with traversal, absolute/drive paths, reserved hidden import names, or symlink escapes fail.
Malformed input returns 422, missing records 404, stale context/decisions or active restart 409.
Retrying the same mutation key returns its saved acknowledgement; a new stale decision cannot
resolve an expired core future. Normal approvals never edit tool arguments.

Creation accepts optional `config`, `draft`, and `template_id`. Configuration is validated before
creating any attack; a template supplies its saved configuration and optional context unless
explicitly overridden. Every attack gets a new identity and empty session/history. Templates copy
the saved next-session configuration and original goal, never current-run messages or follow-up
drafts; a never-started draft supplies the goal only if configuration has none. `ralph_resume` is
cleared because its durable files are not copied. Scope context is opt-in and copied verbatim.
Templates have no dependency on their source attack.

Deletion and per-engagement edits share the session manager's lock, preventing delayed draft or
upload writes from recreating a deleted attack. Cross-process workspace ownership and symlink
containment also apply. File deletion failure retains the index for an explicit retry; success
returns the removed `id` and `session_ids`. Existing SSE streams end when their session is removed.
Only workspace-owned engagement files are deleted; native CLI history and external model-log
overrides are outside this operation.

## Event journal and IPC

Each durable event has `schema_version`, unique `id`, monotonic SQLite `seq`, `timestamp`,
`engagement_id`, `session_id`, `run_id`, `actor_id`, `kind`, typed `payload`, and optional stable
`message_id`. Events commit before SSE exposes them. Snapshot N followed by events after N
avoids gaps; duplicate sequences are ignored. Context/model counters are never fabricated.

Messages retain stable IDs across interleaved actors. The projection independently tracks
lifecycle, activity, actors, proposals/running tools/results, questions, approvals, plan revisions,
findings, and actual effective backend/model/location/log root. Terminal states expire interactions.
Actor snapshots contain source IDs, hierarchy, peer/generation, measured usage, and exact memory/todo
references. Disk finding verdicts remain authoritative; the journal is not the core audit log.

The service starts `sys.executable -m zero_web.worker` with an argument array and structured
initialization. No goal/config is interpolated into shell text. NDJSON IPC has `version: 1`,
correlated `command_id` and acknowledgement/error. Worker stdout is protocol only; ordinary logs
and backend diagnostics use stderr. The service drains stderr to a bounded in-memory tail.

Every engagement has a separate worker cwd/environment, model sessions, tool state and sandbox.
Pipe EOF ends operator input and triggers cleanup. Service shutdown sends close, then bounded
termination of only owned live process handles/groups. A crashed worker is interrupted, invalidates
requests, and preserves evidence; another worker continues. Restart does not pretend model recovery.
Sealed history sits outside worker cwd. Goal-boundary copies retain earlier memory/report versions;
final session sealing retains later validation, original context, context files and model logs.

## Codex model discovery

The model resource returns `models` (`id`, `name`, `description`), `configured_default`,
`source` (`codex_app_server`, `codex_cache`, `unavailable`), retrieval/cache timestamps,
and a human-readable `warning`. The model's invocation identifier comes from the
app-server's `model` field, rather than assuming its opaque picker `id` is executable.
Discovery initializes a short-lived stdio app-server, pages through `model/list`,
filters hidden records, and reaps the process on success, timeout, or cancellation.
It never starts/resumes a thread or turn, writes settings, or grants a server request.
Only display metadata is exposed; cached identity, auth and model instructions are omitted.
The cache is an explicitly labeled fallback, not proof of account entitlement.
