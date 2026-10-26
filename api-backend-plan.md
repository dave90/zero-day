# API backend implementation plan

Status: implementation and acceptance completed, 2026-10-26. The original design and
acceptance checklist are retained below.

Implementation verification: the default free suite passed with 722 tests passing,
40 Docker-dependent tests skipped, and 28 live tests deselected. Local HTTP fixtures
exercise the real LiteLLM OpenRouter/OpenAI-compatible and Anthropic adapters, normal
agent workflows, Ralph epic planning/resume, and swarm coordination/follow-up cycles.
Wheel/source builds, lockfile verification, and lint on changed files passed. The real
Docker API workflow also passed without forwarding the provider credential into the
sandbox. A live OpenRouter `z-ai/glm-5.3` blind regression run exercised every target:
17 of 21 expected vulnerability classes were confirmed (about 81% coverage). SQL
injection in Juice Shop was exploited but missed the score because its Validator did
not finish within the target deadline; this is a capability/timing result rather than
an API transport failure.

Add `--backend api` using the LiteLLM Python SDK so Zero can use commercial APIs,
hosted open models, and custom compatible endpoints. The first acceptance model is
OpenRouter's `z-ai/glm-5.3`, selected in LiteLLM as `openrouter/z-ai/glm-5.3`.
Zero retains its agent loops, tool execution, approvals, sandbox, and finding validation.

## 1. Supported workflows and boundaries

| Workflow | Required API behavior |
|---|---|
| Mode `a` | Pentester and background Validators use the selected API model. |
| Mode `c`, swarm size 1 | Orchestrator, Pentesters, and Validators use the API backend. |
| Ralph | Epic/task analysis, planning review, discussion, every orchestrated pass, and completion review use the API backend; resume preserves existing durable progress. |
| Swarm, mode `c` | Every peer, its focused children and Validators, and the dedicated summarizer use the API backend. |
| Mode `b` | Reject `--backend api` clearly: this mode delegates to Claude Code's MCP loop. |
| Ralph plus swarm size above 1 | Keep the existing CLI and runtime rejection. Supporting each independently does not enable their currently unsupported combination. |

Model support means a chat model reachable through LiteLLM that can follow Zero's
instructions and emit action envelopes. Do not maintain a hardcoded model allowlist
or promise equivalent finding quality across models. Native provider tool calling
is not required because Zero's tools are described in the prompt and executed by Zero.

Keep Codex as the default and preserve existing Codex/Claude behavior. No OpenCode
process, LiteLLM gateway, external orchestration framework, per-role model routing,
automatic model failover, or new sandbox tool is part of this feature. Start with
LiteLLM's async chat-completion interface; a separate Responses-specific transport
and native function-calling protocol are follow-up work if a required model needs them.

## 2. User configuration

Proposed commands, available after implementation:

```bash
# Set OPENROUTER_API_KEY in the host environment before running.
uv run zero run --backend api --model openrouter/z-ai/glm-5.3 --goal "..."

uv run zero run --backend api --model openrouter/z-ai/glm-5.3 \
  --ralph --ralph-iterations 2 --goal "..."

uv run zero run --backend api --model openrouter/z-ai/glm-5.3 \
  --swarm-size 3 --goal "..."

# A provider's hosted or self-hosted OpenAI-compatible chat endpoint.
uv run zero run --backend api --model openai/organization/model-name \
  --api-base-url https://inference.example.com/v1 --api-key-env MODEL_API_KEY \
  --context-window 131072 --goal "..."
```

Add an `ApiConfig` carried by `OrchestratorConfig`. Keep the credential reference
in config, not the secret value. Proposed options:

| Option | Behavior |
|---|---|
| `--model` | Required, nonempty, explicit LiteLLM model identifier for the API backend. |
| `--api-base-url` | Optional provider base URL, including its API version prefix where required; never append `/chat/completions` in configuration. |
| `--api-key-env` | Optional explicit environment variable name. Otherwise use the selected provider's standard credential environment, such as `OPENROUTER_API_KEY`. |
| `--api-max-output-tokens` | Positive generation cap; initial default 16384, checked against known model limits. Includes reasoning where the provider counts it toward the cap. |
| `--api-reasoning-effort` | Optional provider-supported value, passed only when supplied; do not impose one cross-provider enum. |
| `--api-response-format` | `auto` (default), `json`, or `text` for envelope-producing actors. |
| `--api-max-concurrency` | Positive engagement-wide limit on in-flight model requests; initial default 4. |
| `--api-retries` | Transient retries after the first attempt; initial default 2. |
| Existing `--llm-timeout` | One total per-backend-call wall-clock budget, covering queue wait, context preparation, attempts, and backoff. The existing envelope-repair call remains separately bounded. |
| Existing `--context-window` | Explicit override, otherwise resolve from bundled provider/model metadata. |

Change Click's raw `--model` default to unset and resolve defaults by backend:
preserve the current Claude default and Codex configured default; API requires a
model. Preserve explicit `--model ''` behavior for the CLI backends and reject it
for API. Test programmatic configuration as well as Click invocation.

Validate configuration before starting Docker or dispatching any agents. An
explicit key variable that is unset/empty is an error, not permission to choose
another credential. Use provider-aware credential validation without assuming
every provider uses a single bearer key; document the standard API-key paths
first. Unknown custom model IDs remain usable with explicit endpoint, credentials,
and context size. Reject API-only options with a CLI backend to catch mistakes.
Do not require a model-list API, a paid discovery call, or a coding CLI installation.

## 3. API adapter and request protocol

Implement `ApiBackend(LLMBackend)` in `src/zero/llm/api.py`. Add LiteLLM as a host
dependency through `uv add litellm`; commit the generated lockfile. Import it lazily
so non-API backends do not trigger provider initialization. Check Python 3.11
compatibility and installed SDK behavior before pinning the supported version range.

Each call must:

1. Use `litellm.acompletion` with explicit per-call config, without mutating global
   provider keys, API bases, callbacks, or model settings.
2. Send current system instructions and the tool/custom-tool catalog, followed by
   the actor's conversation. Preserve user/assistant roles and encode observations
   as clearly tagged user messages; the current observation type has no native
   provider tool-call ID and must not become an invalid `tool` message.
3. Keep `action`, `actions`, `ask_user`, and `final` exactly compatible with
   `parse_envelope`, including the batch cap and existing format-repair retry.
   Extract shared catalog formatting from `claude_code.py` where useful, while
   keeping existing CLI prompt output stable.
4. Normalize text from provider content blocks into `LLMResponse.text`. Preserve
   provider reasoning metadata separately where needed for continuation, never
   concatenate reasoning into the action JSON or invent a final answer from it.
5. Report empty output, refusal, unexpected native tool calls, and output truncation
   as distinct errors. Do not execute a partial action or turn a truncated answer
   into a successful final result. Include actionable hints for insufficient output
   budget on reasoning models.

In `auto`, enable JSON-object mode only when the selected provider/model is known
to support it; otherwise request text and rely on envelope validation. An explicit
`json` setting must either be honored or fail clearly. Allow at most one bounded
fallback in `auto` for a specific unsupported-format response; do not treat an
arbitrary HTTP 400 as a reason to retry without constraints. Remember the discovered
format capability for that engagement/model. Do not silently drop user options.
Strict JSON Schema generation is deferred: Zero's variable tool arguments and
provider schema subsets deserve separate compatibility work.

The swarm summarizer is a special case: its current contract is plain text, not an
action envelope. Give the backend factory an explicit request purpose/output mode.
For summarization and context compression, omit envelope instructions, tool catalogs,
and forced JSON. An empty tool list alone must not determine the response format,
because some ordinary agent turns also have no tools.

## 4. Actor isolation, history, and context limits

Use full-history requests for API actors initially. `AgentLoop.history` remains the
authoritative local transcript; `_make_session` returns `None` for this backend so
the CLI delta/resume path is never applied to a stateless API call. Keep actor names
for tracing separate from persistent CLI session IDs.

Create an actor-specific API backend/context view for every Orchestrator, Pentester,
Validator, Ralph analyzer/reviewer, swarm peer, and summarizer. These instances share
only engagement-owned transport/concurrency services and immutable configuration;
they must not share conversation, reasoning continuation, or compression state.
If a provider requires reasoning blocks to be returned on later turns, preserve
those provider-specific blocks per actor and associate them with the correct
assistant turn; do not replay them across actor or pass boundaries.

Context handling is required for the first release, particularly for Ralph and swarm:

- Resolve limits from the explicit override first, then bundled metadata. For an
  unknown model require `--context-window`; never assume the Claude 200k/1M limit.
  Reserve output tokens and a conservative token-count margin before sending.
- Use local token estimation without downloading tokenizers or metadata during
  ordinary offline tests. Estimate conservatively where an exact tokenizer is not
  available; label estimates separately from provider-reported usage.
- Near the input limit, compress only an older complete-turn prefix into a bounded
  actor-specific summary and retain recent complete turns. Keep current system
  instructions/catalog, operator constraints, exact evidence references, active
  task/child IDs, and relevant recent observations. The complete original transcript
  and evidence stay in the audit/artifact stores.
- Cache the summary and covered-prefix position in the actor's request view, without
  rewriting the authoritative transcript. Later full-history calls must not append
  the same prefix twice. Ralph pass reset discards this view; resume reconstructs
  context from PRD/progress/dossiers instead of requiring remote conversation IDs.
- Bound compression work and its output within the same backend-call timeout and global
  request limit. Do not hold a semaphore permit while waiting recursively for a
  compression request. If fixed instructions/catalog plus the minimum recent context
  cannot fit, fail that actor clearly rather than silently dropping required input.
  Compression inputs must themselves fit the model window; summarize bounded chunks
  with a fixed maximum number of compression calls per invocation, never recursively
  invoke the ordinary agent/compression path.
- Test a provider-reported context overflow with at most one additional compression
  attempt; an oversized fixed prompt must not produce an infinite repair loop.

Normalize usage into Zero's existing accounting convention: fresh input, cached read,
cache creation, and output, with no double counting. Set `context_tokens` from the
provider's prompt occupancy when available and `context_window` from resolved limits.
Retain reasoning-token and request-ID metadata without adding reasoning twice to
billed output. Missing cost information stays unknown, never a fabricated zero.
Record compression and failed/retried request usage separately where exposed, so
the UI's latest context occupancy is not confused with cumulative billed work.

## 5. Reliability, concurrency, and lifecycle

One `ApiRuntime` owned by the engagement controls request permits, client resources,
and shared discovered capabilities. All children, Ralph phases/passes, peers,
summaries, and compression requests use the same limit. The existing target-host
rate limiter remains independent of model API rate limits.

Use one bounded retry policy for transport errors, HTTP 429, and retryable 5xx
responses. Honor a bounded `Retry-After` with jittered backoff inside the remaining
deadline. Disable overlapping LiteLLM/provider SDK retry loops so retries do not
multiply. Audit the actual upstream attempt count. Release permits while backing off.

Authentication, permission, billing, unknown-model, invalid-option, and oversized
fixed-context errors are non-transient. Integrate error classification with the
existing actor error handling so callers do not spend every remaining turn retrying
a permanent failure. A shared credential failure ends the current goal cleanly and
surfaces one actionable UI/audit error; an actor-specific failure remains isolated.
Preserve the existing rule that background failures do not crash the interactive UI.

Cancellation while queued, requesting, compressing, or backing off must propagate
promptly, release permits, and stop new attempts. Local cancellation cannot promise
the upstream provider stopped billing. Close only engagement-owned clients after
actor tasks have drained/cancelled, including errors and initialization failures.
Do not close process-global SDK resources belonging to other engagements.

Add API-specific trace records rather than manufacturing CLI argv. Include actor,
purpose, model, sanitized endpoint, request ID, attempts, timing, text, and usage.
Keep existing `--no-llm-log` behavior. Never serialize resolved keys or authorization
headers into audit, trace, config repr, errors, Ralph snapshots, or swarm state.
Do not forward model credentials into the sandbox environment.

## 6. Integration points and implementation order

1. **Configuration and shared factory.** Update `cli.py`, `OrchestratorConfig`, and
   backend creation for main/child/validator actors. Centralize API config propagation,
   actor labeling, output purpose, and engagement-runtime ownership. Preserve existing
   fixture injection seams and CLI backend-specific settings.
2. **Adapter and protocol.** Add `llm/api.py`, neutral prompt helpers, request/response
   normalization, capability-aware output formats, classified errors, and usage mapping.
3. **Context and reliability.** Add API-local context management, concurrency, bounded
   retries, cancellation, tracing, and shutdown. Verify their interaction before
   enabling API mode in the larger workflows.
4. **Ralph.** Route every builder in `ralph/driver.py` through the actor-aware factory
   instead of sharing `orch.backend` for analyzer/reviewer context state. Cover epic
   planning, discussion revisions, completion judges, new pass creation, log reset,
   snapshot cleanup, and resumed runs. Preserve plan approval and evidence semantics.
5. **Swarm.** Update `swarm/runtime.py`, `worker.py`, and `summarizer.py` factory calls
   as needed. Qualify peer and child actor labels; share request limits across all
   peers. Preserve bootstrap, messaging, claims, validator authority, dedup, response
   cycles, deterministic summary fallback, and `/respond-now` cancellation.
6. **Tests and documentation.** Complete the matrix below; update README examples,
   architecture backend/context sections, CLI help, and AGENTS guidance where stale.
   Update regression selection so API mode does not require an `api` executable.

## 7. Unit and offline flow tests

Use mocked `acompletion`/HTTP clients and scripted provider responses. No real keys,
external network, Docker, or model calls. Keep meaningful existing flow assertions
on audit logs, durable progress, and finding state.

| Proposed test file or extension | Required assertions |
|---|---|
| `test_api_backend.py` | Provider/model/base URL/key forwarding; explicit-key precedence; role/catalog mapping; all envelope types; batch preservation; multi-block output; reasoning separation; plain-text summarizer; refusals, empty/truncated output and unexpected tool calls; JSON auto behavior and bounded fallback. |
| `test_api_context.py` | Full history on successive turns; changed system instructions/catalog included; actor isolation; summary-prefix bookkeeping; exact references/recent turns retained; output reservation; unknown-model limits; context-overflow repair bounded; failure leaves authoritative history usable. |
| `test_api_runtime.py` | Global concurrency across separate backend instances; cancellation in every wait state; timeout includes queue/backoff/compression; retry classification and exact attempt counts; no nested retry multiplication; permit/client cleanup; no compression deadlock. |
| `test_cli_defaults.py` plus API config tests | Legacy defaults and empty-model behavior preserved; API requires explicit model; valid normal/Ralph/swarm configs; mode-b and combined Ralph/swarm rejection at CLI and config boundaries; invalid API options fail before Docker. |
| `test_context_usage.py`, `test_llm_trace.py` | Cache math, reasoning/output accounting, sparse usage, actual versus estimated occupancy; actor attribution; redaction with a sentinel secret, including provider error bodies; tracing disabled. |
| `test_orchestrated_flow.py` and API flow tests | Exercise the real API adapter with mocked provider responses: parent dispatch, child tool action, independent validation, dedup, final report; authentication failure is terminal; one child failure does not crash siblings/UI. Include mode-a background validation. |
| `test_ralph_flow.py`, `test_ralph_resume.py`, `test_ralph_epics.py` | API config reaches every role; two passes have fresh histories and shared limits; discussion/epic roles are isolated; dossiers, findings and progress survive reset; resume skips approved planning and continues snapshot numbering without remote sessions; no keys in durable state. |
| `test_swarm_runtime.py` and API swarm tests | Two API peers coordinate a cross-peer evidence chain, a separate Validator confirms once, and a plain-text summarizer produces one response; qualified child identities, independent context, global request cap, follow-up cycles, partial failure/fallback, and `/respond-now` cancellation. |

Test native-provider request conversion beyond OpenRouter using SDK transport mocks
for at least one different provider. Do not claim broad provider coverage based only
on mocking `acompletion`'s returned object. Block unapproved external traffic in
these tests so SDK metadata discovery cannot contact a provider accidentally.

## 8. End-to-end tests

### 8.1 Free deterministic tests included in the default suite

Add a loopback OpenAI-compatible HTTP fixture and drive the real LiteLLM SDK against
it with dummy credentials. This tests HTTP paths, headers, request serialization,
response decoding, and timeout/retry behavior without tokens. Disable optional SDK
metadata downloads/callbacks and fail any unexpected external connection.

- `tests/e2e/test_api_transport.py`: multi-turn action/observation/final, JSON versus
  text requests, auth headers, cached/reasoning usage, 401/429/5xx, malformed response,
  output truncation, timeout and cancellation. Record exact HTTP attempt counts.
- `tests/e2e/test_api_workflows.py`: exercise real backend construction through the
  public run lifecycle and a queue-driven UI stub; do not replace `ApiBackend` or
  LiteLLM. Cover mode `a`, standard mode `c`, a two-pass/resumed Ralph run, and a
  two-peer swarm including its summarizer. Assert durable artifacts and cleanup.
  Parameterize shared fixtures rather than duplicating an entire flow suite.
- `tests/e2e/test_api_sandbox.py`: combine that loopback model endpoint with the real
  Docker sandbox. Have the model request a registered tool inside the container,
  receive its observation on the host, then finish. Also cover an orchestrated
  child/Validator flow. Assert the sandbox really started rather than accepting
  host fallback, and that provider credentials are absent from container env.

The API server is contacted by the host, not the sandbox. Keep the model fixture
address separate from target addresses reachable by container tools. Docker tests
skip with the existing build hint when prerequisites are absent. No new tool means
no new registry-coverage exception is needed.

### 8.2 Opt-in live provider tests

Add `tests/e2e/test_live_api.py` with both `live` and new `live_api` markers. Read
the model from `ZERO_TEST_API_MODEL` and credentials from the normal environment;
optional test endpoint/key-variable overrides mirror production configuration.
Use `openrouter/z-ai/glm-5.3` for initial acceptance, without making it a hidden
production default. Missing prerequisites skip; a configured provider returning
an error fails the test rather than being treated as an unavailable prerequisite.

Cover separately selectable scenarios with bounded turns/output/timeouts:

1. A valid envelope plus multi-turn recall and one real tool observation.
2. Standard orchestration with a focused child and independent Validator.
3. Ralph planning/review, bounded execution, completion review, report, and resume.
4. Two-peer swarm coordination, child validation, and a usable plain-text summary.

Use benign local fixtures for transport/workflow smoke tests; do not require finding
a vulnerability to prove API plumbing. Do not assert exact natural-language output,
provider cache hits, fixed prices, or timing thresholds across real providers. Missing
required tool/role activity is a failed scenario, not grounds to weaken assertions.
Run a second provider/model before claiming tested portability beyond OpenRouter.

### 8.3 Finding capability regression

Extend `tests/conftest.py`'s `--regression-backend` choices with `api` and add API
model/base URL/key-env options. Update prerequisite checks and both strict/blind
configuration paths to use provider credentials instead of `shutil.which('api')`.
Preserve the existing manifest, independent validation rules, and offline matcher.

Run one existing known-vulnerable target with OpenRouter GLM 5.3 first and require a
confirmed finding matching the manifest. Broader targets stay separately selectable.
Keep regression tests marked `regression` and `live`; plumbing success alone is not
evidence of finding capability. Do not add token-spending tests to the default run.

## 9. Validation and completion criteria

During implementation, run focused new tests first, then:

```bash
uv run pytest tests/unit -q
uv run pytest -q
uv run ruff check .
uv build

# Explicit opt-in; spends provider credits. Proposed new marker/config options.
ZERO_TEST_API_MODEL=openrouter/z-ai/glm-5.3 \
  uv run pytest tests/e2e/test_live_api.py -m live_api -q

uv run pytest -m regression --regression-backend api \
  --regression-model openrouter/z-ai/glm-5.3 -k dvwa
```

The live API module must retain the `live` marker on every test, including tests
selected by `live_api`; command-line `-m` overrides the default marker expression.
Register the new marker in `pyproject.toml`. Report skipped Docker/live coverage
explicitly; do not claim a live acceptance test passed without a provider call.

Completion requires all of the following:

- Existing CLI backend tests pass, and the default suite still spends no tokens.
- An OpenRouter key and `openrouter/z-ai/glm-5.3` work without a coding CLI or gateway.
- Real SDK transport tests pass through standard, Ralph, and swarm workflows.
- Live normal/Ralph/swarm scenarios pass with the configured acceptance model;
  the known-vulnerability regression confirms its expected finding.
- Actor histories remain isolated; request concurrency, retries, context, and
  shutdown are bounded; credential redaction is verified.
- Documentation distinguishes tested providers from generic compatibility and keeps
  the existing unsupported Ralph-plus-swarm combination explicit.

## 10. Research references

- [LiteLLM SDK overview](https://docs.litellm.ai/docs/) describes the in-process
  provider abstraction and the separately deployable gateway.
- [LiteLLM OpenRouter adapter](https://docs.litellm.ai/docs/providers/openrouter)
  documents `OPENROUTER_API_KEY` and `openrouter/<model>` identifiers.
- [OpenRouter GLM 5.3](https://openrouter.ai/z-ai/glm-5.3) supplies the acceptance
  model ID and documents its reasoning behavior. Availability/limits must be
  rechecked when running live acceptance.
- [LiteLLM compatible endpoints](https://docs.litellm.ai/docs/providers/openai_compatible)
  documents custom API bases and compatible model routing.
- [LiteLLM input parameters](https://docs.litellm.ai/docs/completion/input) and
  [JSON mode](https://docs.litellm.ai/docs/completion/json_mode) describe request
  options and capability checks; verify mappings against the locked SDK version.
- [LiteLLM retry configuration](https://docs.litellm.ai/docs/routing) distinguishes
  LiteLLM retry loops from provider SDK retries, relevant to preventing multiplication.
