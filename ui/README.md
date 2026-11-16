# Zero Studio

A local browser interface for Zero. See the [main quick start](../README.md#quick-start)
for creating your first attack, or the [CLI guide](../CLI.md) for terminal use.
React/TypeScript/Vite, a Starlette/SQLite service, and
one host worker process per active engagement. The terminal interface remains independent.

## Install and launch

From the repository root, with Python 3.11+, uv, and Node 22.12+:

```sh
uv sync --project ui/server
npm --prefix ui/web ci
./ui/dev.sh
```

Open **http://127.0.0.1:4319**. Development proxies `/api` to the service on port 4318.
On **Windows**, use PowerShell and the built UI commands below in place of `./ui/dev.sh`.
Studio runs natively and reuses the authenticated Codex or Claude CLI for your Windows user.
Docker Desktop should use Linux containers for the tool sandbox. For frontend development,
run `uv run --project ui/server zero-web --dev-origin http://127.0.0.1:4319` in one terminal
and `npm --prefix ui/web run dev -- --port 4319 --strictPort` in another.

For one-origin production serving (including Windows):

```sh
npm --prefix ui/web run build
uv run --project ui/server zero-web
```

Open **http://127.0.0.1:4318**. `zero-web --port 4321 --data-root /path/to/workspace`
selects another loopback port and a dedicated local data directory. Run one service per
data directory. Stop with Ctrl+C; the service supervises cleanup of its owned workers.
Keep this checkout available: the service depends on the shared core as an editable package.

The normal application starts empty and uses authenticated `codex` or `claude` on PATH.
It never launches an assessment just because a page opens. Build the sandbox separately:

```sh
docker build -t zero-sandbox:latest -f docker/Dockerfile .
```

Create a conversation, import/edit `pentest.md`, attach context, configure the run, then
send the first goal. Studio checks CLI authentication and Docker/image readiness before
starting a live session. A later sandbox startup failure is shown as **host execution**.
Scope is advisory; `scope_host` preserves `web_fetch` blocklist semantics.

## Using the workspace

- **New attack:** choose the working style, backend and model, main/pentester/validator turn
  budgets, max subagents, and approvals directly during creation. Codex uses the discovered model
  picker; Claude accepts its model value. Backend switches retain separate model selections.
  Templates prefill these settings and can be adjusted before saving. Exploit auto-approval
  requires **I understand**. Selecting **Swarm** reveals **Swarm size** (at least two peers);
  saved templates prefill the peer count. Creating the attack saves the settings without starting a run.
- **All attacks:** create, search, open, archive, or restore conversations. Switching attacks
  keeps assessments running. Unsent drafts are stored by the local service.
- **Attack actions (⋯):** available on each attack card and beside Configure. **Create template**
  saves configuration, target, and the first goal; optionally include `pentest.md` (including
  credentials). Choose **Save & create attack** for a fresh copy immediately, or reuse it later
  through **New attack → Start from**. Review the new name, target, scope, and goal before creating.
  Templates are independent snapshots: they survive source deletion and do not follow later edits.
  Conversation, findings, uploads, agents, recordings, and run history are never copied. Ralph
  resume is switched off; import referenced Burp/context files again.
- **Templates:** open from the sidebar, beside New attack on All attacks, or through
  **Manage templates** in the New attack dialog. Each saved setup offers **Create attack**
  and **Delete**. Deletion requires confirmation and preserves attacks already created from
  that template. Returning from management keeps an unfinished new-attack form intact; deleting
  its selected template preserves the copied fields as a standalone draft.
- **Delete attack:** under Attack actions, type its name to confirm permanent deletion. If it
  has an open runtime, explicitly confirm ending it first. This closes only that attack's workers
  and removes its workspace directory, conversations, sessions, event journal, and cached command
  responses. Saved templates and other attacks remain. This does not erase native model CLI history
  or externally configured trace directories outside this workspace. Archive remains available
  when you want to keep the evidence.
- **Conversation:** submit goals and queued guidance; inspect grouped tools; allow once,
  deny, or grant eligible tools for the session; answer questions and revision-specific
  Ralph checkpoints. Stop current turn and End session are separate controls.
  Progress explicitly distinguishes **Assessment running**, **Waiting for your decision**,
  **Goal finished**, and terminal failure/interruption. Active agent counts and tool names
  show outstanding work; findings collected during a run do not imply completion. Last activity
  is the timestamp of the latest runtime event, not a browser heartbeat. A quiet or disconnected
  run is never presented as finished. A finished goal leaves the runtime open for follow-up;
  background validation still running is shown separately.
- **Configure:** all 38 effective CLI defaults in eight sections, four working-style presets, server
  validation, and a quoted copyable CLI command. Changes during an active session are
  next-session drafts. End that session before starting a different runtime configuration.
  Main-agent, pentester, and validator investigation budgets default to **50** turns each;
  up to **100** subagents can run concurrently per peer by default.
  See [important configuration](../README.md#important-configuration) for budgets and approvals.
- **Findings:** independent verdict filters, endpoint/CWE search, submitted evidence,
  validator rationale/reproducer, CVSS vector, duplicate links, and artifact exports.
  Missing vectors remain unscored; the UI has no manual verdict control.
  **Copy to clipboard** beside **Export finding** copies the full original Markdown record,
  including evidence and validation, and confirms when the copy succeeds.
  Severity badges appear in the list and finding details. The **Severity** filter combines
  with verdict and search, with counts for the matching records. Validator CVSS 4.0 scores with
  vectors use [FIRST's qualitative bands](https://www.first.org/cvss/v4.0/specification-document#Qualitative-Severity-Rating-Scale).
  Otherwise recognized agent hints are explicitly marked **estimate**; absent or unrecognized
  hints are **Unrated**. Missing CVSS remains **Unscored**, distinct from an actual score of 0.0.
  Severity never changes a finding's confirmed/pending/rejected verdict.
  The three verdict summary cards also serve as filters; click a selected card again to show
  all verdicts. The toolbar contains severity and search without a duplicate verdict selector.
- **Agents:** actual actor/peer IDs and parent metadata, objectives and turns, measured context
  where available, matching read-only memory/todos, and attributed tool output.
  Memory and todo contents open directly inside the agent's tabs. Memory renders as Markdown;
  todos show task status in a read-only checklist. Live content refreshes automatically, while
  sealed session artifacts retain their historical contents.
  In Swarm mode, pentesters and validators are nested beneath their owning peer.
  Use the peer's chevron to collapse or expand its agents; counts remain visible while
  collapsed. Clicking the peer or any child still opens its details. Collapse choices persist
  when switching tabs and are kept separate for each session.
- **Workspace:** scope revision conflicts, binary-safe file/folder import, custom-tool
  inspection, structured Ralph epics/tasks and dossiers, Swarm ledger/messages, and history.
  Files are limited to 1 MB each. Burp paths refer to imported engagement-relative directories.
- **Activity:** searchable/paginated Audit, Reasoning, Model I/O, and Swarm records. Auto refresh
  refreshes the selected page; missing logs are explicit. Raw content is rendered as text.
- **Runs & snapshots:** open a sealed session; inspect its immutable context/configuration,
  goal-boundary snapshots under `goals/<run-id>/`, and Ralph's own pass snapshots. A boundary
  snapshot captures evidence at that time; background validation can still finish afterward.
  The final sealed session includes later evidence. Goal snapshot files are browsable after
  session close; they do not claim model-session recovery.
- **Recorded audits:** import validated JSONL (5 MB maximum), inspect it read-only, or explicitly
  start a separate **recorded replay** session. Replay uses core `ReplayBackend` in mode c,
  reviewed approvals, no automatic grants, no Ralph/Swarm, and may execute tools again. It
  is not a simulation of captured tool results or a new live model validation.
- **Reset .pentest:** a separate, confirmed operation while inactive. Preserves conversation,
  sealed history, scope, context files, recordings, `.ralph/`, and `.ralph_logs/`. Archive
  only changes navigation; it removes no evidence.

Keyboard: Cmd/Ctrl+N creates an attack, Cmd/Ctrl+K focuses attack search, Enter sends,
Shift+Enter adds a newline, Escape closes a dialog. Dialogs use native focus trapping and
return focus. Panel resize separators support pointer dragging and left/right arrow keys.
The inspector collapses below 1190px to preserve the desktop workspace.

## Runtime and persistence

See [`contracts.md`](contracts.md) for API, IPC, event, and ownership contracts.
Default data is ignored under `ui/.local/`:

```text
workspace.sqlite3                     engagement/session index, templates, drafts, messages, events
engagements/<id>/work/                 worker cwd, pentest.md, zero_files/, .pentest/, Ralph state
engagements/<id>/history/<session-id>/  sealed evidence, original context, per-goal snapshots
```

The browser's localStorage holds only display preferences and scroll positions. Credentials,
context, messages, drafts, tool arguments, and evidence remain service-owned. Native model
sessions are not recovered after service/worker loss: sessions become interrupted, pending
requests expire, and continuation starts a new worker (or explicit Ralph resume).

File/memory roots and model tracing are process-local. Each worker clears inherited tool roots.
An operator-set `ZERO_LLM_LOG_DIR` is namespaced beneath
`<override>/zero-studio/<engagement-id>/<session-id>/`; the effective path is emitted by the
runtime, readable in Activity, and copied into sealed history. An environment override enables
logging even when the configuration flag is off, matching core precedence.

## Tests

All commands below are offline and do not call a live target or model:

```sh
uv run --project ui/server pytest ui/server/tests -q
npm --prefix ui/web run test
npm --prefix ui/web run build
npm --prefix ui/web exec -- playwright install chromium
npm --prefix ui/web run test:e2e
uv run pytest -q
```

Browser tests use a **real service and worker**, with scripted model adapters selected only by
`ZERO_WEB_TEST_MODE=1` on the server process. This is not a browser configuration option.
The UI displays a persistent synthetic-fixture banner. Test workspaces and screenshots are ignored
under `.local/` and `ui/web/test-results/`. Tests cover separate workers, approvals across tabs,
refresh, independent validator fixtures, Ralph revision/pass/resume, Swarm ownership/messages,
file containment, binary imports, replay/reset, history, schema parity, and desktop widths
1100, 1280, 1440, 1728, 1920. Core Docker tests run through the existing default suite. `.github/workflows/ui.yml` defines
separate service, browser/build, real sandbox, and core jobs; it never starts the live matrix.

UI tests are deliberately outside the root testpaths; run their dedicated commands. Normal
frontend E2E and both Python default selections exclude live model spending.

## Explicit live release verification

The live inventory and capability matrix have **not been run** for this implementation.
[`tests/live/codex-models.json`](tests/live/codex-models.json) therefore contains no invented
verified models. The runner reuses the exact-instance scope fixture from
[`tests/gruyere_fixture.py`](../tests/gruyere_fixture.py).

```sh
# Read local model/cache hints; no model or target calls.
uv run --project ui/server python ui/tests/live/matrix.py --discover --list
```

Review the discovered IDs, declare supported models and justified exclusions in the manifest,
and complete the account inventory. Cache entries alone do not establish entitlement.
The following commands **spend model tokens** and are deliberate opt-ins:

```sh
uv run --project ui/server python ui/tests/live/matrix.py --preflight
uv run --project ui/server python ui/tests/live/matrix.py --run --smoke
# After marking release_inventory_complete, the complete serial matrix:
uv run --project ui/server python ui/tests/live/matrix.py --run --overall-deadline 14400
# Equivalent release test selection, after the same preflight:
ZERO_UI_LIVE=1 uv run --project ui/server pytest ui/tests/live -m 'live and live_codex and gruyere and ui_live'
```

`--list` prints every effective case and budget before execution: every declared explicit model
plus account-default × four modes × three profiles, plus Ralph epic/checkpoint variants.
Smoke is one account-default orchestrated P2 case and is explicitly incomplete. Cases run serially,
are bounded by an overall deadline, drive the browser and API, retain screenshots/journals/config,
check actual Docker execution, and distinguish plumbing from independently confirmed known XSS.
P2 requires capability confirmation. P1 requires Deny and Allow once. P3 may be inconclusive.
A detected explicit off-prefix URL is a failure; URL-argument auditing cannot detect every
network destination embedded inside arbitrary Python, binary arguments, or request bodies.

## Release status and limits

Offline integration and Chromium verification do not replace the manual and live release checks.
Use the [manual walkthrough](tests/manual/studio-review.md) for release verification.

Still required before release: account-verified complete model inventory and live matrix, a human
manual review including real model results, cross-browser/zoom/accessibility review. The free UI-specific Docker integration test verifies
two real workers, separate containers, isolated file mounts, and cleanup. The browser suite currently runs
Chromium only. The live runner itself has offline contract tests, but its token-spending path is
unverified. It audits explicit URL arguments, not a mandatory network firewall.

No arbitrary host-directory picker or archive extraction is exposed. Import folders through the
browser; configure their engagement-relative paths. Custom tools, memory, todos, and evidence
have no unrestricted editor/execute API. Large uploads and artifacts are capped; previews are
inert text and downloads are attachments. Goal snapshots are temporal evidence copies, not
transactional filesystem snapshots across still-running tools.

## Choose a Codex model just for Zero

In **Configure → Model & sessions → Model**, search and select a model from the installed
Codex catalog. Refresh queries Codex again; model names and IDs are not hardcoded in Studio.
The service uses the Codex app-server's `model/list` operation without starting a thread or
model turn. If discovery is unavailable, visible entries from Codex's local cache are clearly
labeled as cached hints; model access is still checked by Codex when the run starts.

An explicit choice is passed as `codex exec --model <id>` on both initial and resumed actor
calls. It applies to that Zero runtime and its agents, without editing `config.toml` or changing
other Codex sessions. **Codex default** follows the model currently configured in Codex;
choose an explicit entry when you want to pin Zero independently. A saved selection that
is missing from a later catalog is retained visibly instead of being silently replaced.

Configuration remains immutable for an active Zero runtime. Changing a saved model applies to
its next runtime session; end the current Zero session before starting with the new model.
Claude still accepts its backend-specific model value; the discovered picker is for Codex.
