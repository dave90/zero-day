<div align="center">

<pre>
 ███████╗███████╗██████╗  ██████╗
 ╚══███╔╝██╔════╝██╔══██╗██╔═══██╗
   ███╔╝ █████╗  ██████╔╝██║   ██║
  ███╔╝  ██╔══╝  ██╔══██╗██║   ██║
 ███████╗███████╗██║  ██║╚██████╔╝
 ╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝
</pre>

<p><strong>An autonomous, multi-agent system for web application pentesting and bug-bounty work.</strong></p>

<p>Parallel testing · independent validation · sandboxed tools · auditable reports</p>

</div>

`zero` investigates web applications through **Zero Studio**, a local browser interface for
creating attacks, coordinating agents, reviewing evidence, and exporting reports.

![Zero Studio conversation with assessment progress, agents, and findings](docs/images/studio-overview.png)

*Screenshots show the actual interface with synthetic offline demonstration data.*

> [!CAUTION]
> Use `zero` only on systems you own or have explicit permission to test.

## Why zero?

- **One workspace per attack** — keep scope, goals, conversations, configuration, and evidence
  together. Switch between attacks while their assessments continue.
- **Parallel investigation** — an orchestrator dispatches focused pentesters; Swarm mode adds
  collaborating peer orchestrators with shared work and evidence.
- **Independent validation** — separate validators confirm or reject candidate findings.
- **Visible progress** — inspect active agents, tools, memory, todos, and pending decisions.
- **Useful results** — filter findings by verdict and severity, inspect reproduction steps,
  copy a finding to the clipboard, or export the report.
- **Your model backend** — use an authenticated Codex or Claude CLI, with configuration per attack.
- **Sandboxed tools** — Docker runs security tools while the agent loops run on the host.

Prefer the terminal? See the separate [command-line guide](CLI.md).

## Quick start

### Prerequisites

- Python 3.11 or newer and [`uv`](https://docs.astral.sh/uv/).
- Node.js 22.12 or newer, including npm.
- An authenticated `codex` or `claude` CLI available on `PATH`. Studio uses it to run the agents.
- Docker, running locally, for the recommended tool sandbox. Allow roughly 6 GB for the image.

### Install and open Studio

Run these commands from your checkout:

```bash
git clone https://github.com/dave90/zero-day.git
cd zero-day
uv sync --project ui/server
npm --prefix ui/web ci
docker build -t zero-sandbox:latest -f docker/Dockerfile .
./ui/dev.sh
```

Open **http://127.0.0.1:4319** and keep the launch terminal running. The local service listens on
port 4318 and the frontend on 4319. Opening Studio does not start an assessment.

For serving a built UI from a single port or selecting another data directory, see the
[Studio setup guide](ui/README.md#install-and-launch).

## Create your first attack

An **attack** is a workspace for one assessment: its target, settings, conversation, and findings.
You can choose the main settings during creation, without opening Configure afterward.

For your first run, choose **Blank assessment** and **Orchestrated**, use the backend you have
already signed into, and keep the default budgets and manual approvals.

### 1. Describe what you want to test

Click **New attack** in the sidebar. In **A new assessment**, leave **Start from** set to
**Blank assessment** and fill in these fields:

| Field | What to enter | Example |
| --- | --- | --- |
| **Attack name** | A name you will recognize in your workspace. | Staging application review |
| **Target / start URL** | The application you have permission to test. | `https://staging.example.com` — replace this example with your target. |
| **Engagement context** | What Zero may test, what it should leave alone, and any test accounts or constraints. | Only the staging host; test accounts only; no destructive actions. |
| **First goal** | The task you want Zero to investigate first. | Review login and access controls. Validate reproducible findings and explain the evidence. |

You can type the engagement context or choose **Import file** to load an existing `pentest.md`.
For example, replace the host below with your authorized target:

```markdown
# Staging application assessment

## Scope
- Only https://staging.example.com and its same-origin APIs.
- Test accounts and synthetic records only.
- Exclude production, denial-of-service, and destructive actions.

## Credentials
- Add dedicated test accounts here if needed.

## Goal
Review authentication and access-control boundaries.
```

Context files are optional. Use **Add context files** to attach request samples, source files,
or Burp exports if they would help the investigation. Each imported file can be up to 1 MB.

### 2. Choose how Zero will run

Continue down the same **A new assessment** form to choose the working style and run settings.

**Working style** controls how the agents organize their work:

| Working style | When to use it |
| --- | --- |
| **Orchestrated** | Start here. One lead agent investigates and assigns focused tasks to pentesters and independent validators. |
| **Swarm** | Use several collaborating lead agents. Selecting it reveals **Swarm size**, which starts at **2** and must be a whole number of at least **2**. |
| **Ralph** | Review a proposed plan, then run a sequence of bounded investigation passes. |
| **Single agent** | Use one pentester with background validators. |

A pentester investigates a possible issue; a validator checks the evidence independently.
In Swarm, each lead agent has its own investigation budgets and subagent limits.

Under **Backend & model**, choose the CLI that Zero will use:

- **Backend:** choose **Codex** or **Claude**, matching the CLI you installed and authenticated.
  Codex is the default. The selection applies to the lead agent, pentesters, and validators.
- **Model:** with Codex, keep **Codex default** or choose a model from the searchable list.
  With Claude, keep the prefilled model, enter another available Claude model, or clear the field
  to use its account default. Switching backends keeps their model choices separate.

Under **Investigation budgets**, you can keep these defaults for your first run:

| Setting | Default | Meaning |
| --- | --- | --- |
| **Max turns** | **50** | Maximum investigation steps for the main agent on one goal. |
| **Pentester max turns** | **50** | Maximum steps for each assigned pentester. |
| **Validator max turns** | **50** | Maximum steps for each independent validator. |
| **Max subagents** | **100** | Concurrent pentesters and validators in Orchestrated mode; in Swarm, the limit applies to each lead agent. |

These are upper limits. Agents can finish earlier, and Zero creates subagents as needed.

Under **Approvals**, start with **Auto approve** empty and **Yolo**, **I understand**, and
**Allow host exec** switched off. When a tool needs approval, Zero will show a decision card in
the conversation. You can review the proposed action and allow or deny it.

If you later enable automatic approvals, selecting **Exploit** also requires **I understand**.
See [Important configuration](#important-configuration) for the full approval rules and advanced
budget settings.

![Backend, model, investigation budgets, and approvals in the new-assessment form](docs/images/studio-new-attack.png)

### 3. Create the workspace, then start testing

1. Click **Create attack**. Studio saves your settings and opens the attack's **Conversation**.
   Your first goal is ready in the message box. **No assessment has started yet.**
2. Review the goal, then click **Start assessment** or press **Enter** to send it. Studio checks
   the selected backend's authentication and the sandbox setup before starting. Live assessments
   consume the selected backend's allowance.
3. Watch the conversation. Answer questions and review any approval cards. Ralph also asks you to
   review its plan before the investigation passes begin.

**Configure** remains available for advanced options, such as sandbox settings and rate limits,
or to change the saved setup later. An open runtime keeps its current configuration; end that
runtime before starting another with different settings.

### 4. Follow progress and review results

- **Conversation** shows messages, tool activity, operator decisions, and assessment progress.
  **Goal finished** means Zero is ready for another goal; the runtime remains open. **Background
  work running** means agents or tools are still active. Last activity shows how recently the
  runtime reported an event.
- **Agents** shows each actor's objective, tools, memory, and todos. In Swarm mode, expand or
  collapse a peer to inspect its pentesters and validators. Memory and todos open inside the
  selected agent's tabs.
- **Findings** shows evidence, independent validation, severity, and reproduction steps. Click a
  confirmed/pending/rejected summary card to filter by verdict; click it again to clear that
  filter. Combine it with **Severity** and search. Agent severity hints are marked **estimate**;
  missing validator scores remain **Unscored**.
- **Workspace** contains scope, imported files, reports, and saved run snapshots.
- **Activity** contains the audit trail, reasoning, model I/O, and Swarm records.

![Finding verdict cards, severity filtering, and validated evidence in Zero Studio](docs/images/studio-findings.png)

Open a finding and choose **Copy to clipboard** or **Export finding**. Use **Export report** on
the Findings page, or **Report** in the attack header, when a report is available.

To continue, send another goal in the same conversation. To stop the runtime and its background
work, use **End runtime session** in the attack header; conversation and evidence are preserved.

## Important configuration

Open **Configure** on an attack to review these settings. The defaults below apply to new blank
attacks and CLI runs. Existing attacks and templates keep their saved values; update them in
Configure when you want different budgets. An open runtime keeps its configuration until it ends.

### Backend: Codex or Claude

Choose **Backend** in **A new assessment**, or **Configure → Execution → Backend** later:

| Backend | Required CLI | Authentication |
| --- | --- | --- |
| **Codex** (`codex`, default) | `codex` on `PATH` | Uses your existing authenticated Codex CLI session. |
| **Claude** (`claude`) | `claude` on `PATH` | Uses your existing authenticated Claude Code session. |

The selected backend runs the main agent, pentesters, and validators for that runtime. Both
backends support all four working styles. Studio checks that the selected CLI is installed and
authenticated before starting. Live runs consume the selected backend's subscription or API
allowance.

Choose the model under **Model & sessions → Model**. Codex provides a discovered model picker;
Claude accepts a model value for its backend. From the terminal, select the backend with
`uv run zero run --backend codex` or `uv run zero run --backend claude`.

### Agents & budgets

In **Configure → Agents & budgets**, the three investigation turn limits default to **50**:

| Setting | Default | What it controls |
| --- | --- | --- |
| **Max turns** | **50** | Main agent's turn limit for a goal; each Swarm peer has its own budget. |
| **Pentester max turns** | **50** | Investigation steps for each dispatched pentester. |
| **Validator max turns** | **50** | Investigation steps for each independent validator. |
| **Max subagents** | `100` | Pentesters and validators allowed to run concurrently per peer. |
| **Max total subagents** | `0` (unlimited) | Total child dispatches per peer; set a positive value to cap dispatches. |
| **Subagent deadline** | `1200` seconds | Elapsed-time limit per subagent, checked between turns. |
| **LLM timeout** | `900` seconds | Maximum wait for one model call, shared by main agents and subagents. |

Turn limits are independent: changing Max turns does not change the pentester or validator
budget. Agents can finish before reaching their limit. Deadlines are checked between turns and
do not interrupt an in-flight approval wait or tool call; model and tool timeouts are separate.
**Validator Claude max turns** controls Claude's internal per-call behavior, separately from the
50-step validator investigation budget.

In Swarm mode, concurrency and total-child caps apply **per peer**. For example, four peers with
Max total subagents set to 20 can dispatch up to 80 children in total. These settings bound work,
not a monetary or token allowance.

### Approvals

In **Configure → Approvals**, automatic approvals are disabled by default. Calls that require
approval show a decision card in the conversation.

| Setting | Default | Effect when enabled |
| --- | --- | --- |
| **Auto approve** | No risk classes selected | Automatically approve the selected `safe`, `active`, or `exploit` classes. Exploit also requires **I understand**. |
| **Yolo** | Off | Automatically approve `safe` and `active` calls. Exploit and destructive calls are not covered. |
| **I understand** | Off | Acknowledgement required together with `exploit` in Auto approve. It does not grant approvals by itself. |
| **Allow host exec** | Off | Permit code/binary tools running on the host to use the configured approval policy. It does not approve calls by itself. |

For each approval card, choose **Allow once** or **Deny**. Eligible tools also offer **Allow tool
this session**; this grant lasts for the current runtime. Destructive actions always require a
decision. Host-executed code/binary tools still prompt unless Allow host exec is enabled, even
when a risk class or session grant would otherwise approve them. Approval settings do not remove
Ralph plan checkpoints or agent questions.

## Reuse and organize attacks

Open an attack's **⋯ menu → Create template** to save its configuration and first goal. Optionally
include `pentest.md`, including any credentials it contains. **Save & create attack** immediately
opens a fresh assessment; saved templates are also available under **New attack → Start from**.
New attacks start without the source's messages, findings, uploads, agents, or run history.

Use **Templates** in the sidebar to create attacks from saved setups or delete a template.
Deleting a template preserves attacks already created from it.

An attack's **⋯ menu** also offers **Archive** and **Delete attack**. Archive keeps its evidence;
deleting an attack permanently removes its workspace and conversation after confirmation.

## Where your data is stored

Studio stores its workspace locally under `ui/.local/` by default:

```text
ui/.local/
├── workspace.sqlite3                     attacks, templates, drafts, sessions, and events
└── engagements/<attack-id>/
    ├── work/
    │   ├── pentest.md                    engagement context
    │   ├── zero_files/                   imported files (default files root)
    │   └── .pentest/                     findings, reports, audit, memory, todos, and model logs
    └── history/<session-id>/             sealed session evidence and goal snapshots
```

Ralph also records planning and progress in `.ralph/` and `.ralph_logs/` within the attack's
working directory. These directories and model traces can contain credentials and full prompts;
handle them according to your engagement's data-handling rules.

Scope context guides the agents; it is not a network firewall. If sandbox startup fails after
preflight, Zero can fall back to host execution and displays that state. Review tool approvals
and the execution location shown by Studio. Live assessments consume the selected backend's
allowance; use turn budgets and total subagent caps to bound work.

## Documentation

- [Command-line guide](CLI.md) — terminal setup, flags, scope files, approvals, Swarm, and Ralph.
- [Studio guide](ui/README.md) — configuration, storage, launch options, and UI verification.
- [Architecture](architecture.md) — shared runtime, orchestration, tools, and ownership.
- [BountyBench integration](benchmarks/bountybench/README.md) — external capability evaluation.

## Development

```bash
uv sync
uv run pytest tests/unit -q  # offline core tests
uv run pytest -q             # core + Docker tests; live model tests are excluded
uv run ruff check .
uv build
```

Run UI checks separately using the [Studio test commands](ui/README.md#tests). Docker tests skip
with guidance when the image is unavailable. Live and regression suites spend model tokens and
require explicit opt-in; see [regression tests](tests/regression/) before running them.

## License

Licensed under the [GNU General Public License v3.0](LICENSE).
