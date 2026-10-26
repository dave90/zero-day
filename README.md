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

`zero` coordinates focused pentester agents, independently validates candidate findings, and
writes the evidence to a local report—all from an interactive terminal.

> [!CAUTION]
> Use `zero` only on systems you own or have explicit permission to test.

## Why zero?

- **Parallel by default** — an orchestrator dispatches focused pentesters and reacts as they
  finish.
- **Live collaborative swarm** — `--swarm-size N` runs several peer orchestrators with durable
  messaging, leased work claims, evidence handoffs, and one independently validated result set.
- **Independent validation** — a separate validator retests every candidate before it is marked
  confirmed or rejected.
- **Codex or Claude subscriptions** — run through either authenticated CLI using the subscription
  plan already configured for that tool.
- **Controlled sandbox** — Docker isolates Python and security tools; approvals, rate limits,
  turn budgets, and subagent caps keep each run bounded.
- **File context** — agents can list, search, and read files provided through the jailed
  `zero_files/` directory.
- **Auditable results** — reports, CVSS 4.0 findings, tool activity, reasoning, and per-agent memory
  are stored under `.pentest/`.

## Quick start

### Prerequisites

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- An authenticated OpenAI Codex CLI (`codex` on `PATH`), or Claude Code when using
  `--backend claude`, or a provider API key when using `--backend api` (no coding CLI needed)
- Select the model you want in Codex or Claude Code before starting `zero`. The default Codex
  backend uses the model configured in Codex. To use the model selected in Claude Code, run with
  `--backend claude --model ''`; otherwise the Claude backend pins its documented default model.
- Docker for the recommended sandboxed setup (allow roughly 6 GB for the Kali image)

### Install

```bash
git clone https://github.com/dave90/zero-day.git
cd zero-day
uv sync
docker build -t zero-sandbox:latest -f docker/Dockerfile .
mkdir -p targets/testaspnet
cd targets/testaspnet
uv run zero init
```

Use one dedicated directory per target; `zero` mounts the current directory into the sandbox.
`zero init` creates its `.pentest/` workspace, `zero_files/` file jail, and a starter
`pentest.md` without overwriting an existing scope file.

## Basic usage

The Quick Start leaves you in `targets/testaspnet`. Replace the generated `pentest.md` with:

```markdown
# TestASP.NET engagement

## Scope

- Start URL: http://testaspnet.vulnweb.com/about.aspx
- Same-origin pages on testaspnet.vulnweb.com may be tested when needed to discover or validate
  findings.
- Out of scope: every other host or subdomain.
- Do not perform denial-of-service, destructive, or data-changing tests.

## Credentials

- None. Test unauthenticated behavior only.

## Goal

Find and independently validate all reproducible vulnerabilities within scope. Keep request
volume low and report only evidence-backed results.
```

Start the default orchestrated run:

```bash
uv run zero run \
  --max-turns 25 \
  --goal "Find and independently validate all reproducible vulnerabilities starting from http://testaspnet.vulnweb.com/about.aspx. Follow pentest.md exactly."
```

To run several collaborating top-level orchestrators over the same engagement:

```bash
uv run zero run --execution-mode c --swarm-size 4 \
  --goal "Find and independently validate all reproducible vulnerabilities"
```

Peers have separate model sessions, memory, tool state, turn budgets, and focused children. They
coordinate through `.pentest/swarm/state.sqlite3`, share one sandbox and approval policy, hand off
content-addressed evidence, and produce one consolidated response and report. `--swarm-size 1`
keeps the original mode-c runtime. Swarms are intentionally unavailable with modes `a`/`b` and
Ralph.

The first swarm cycle has a mandatory ownership bootstrap. Each peer inspects the shared ledger,
publishes and atomically claims lane-specific work, announces its boundaries, waits for peer
coordination, and completes the bootstrap before target tools or child dispatches are enabled.
Later read-only questions reuse durable findings and coordination state; they return a clean answer
without repeating the engagement report when no findings changed.

The terminal marks these runs as `swarm N` in the startup banner. Phase banners show bootstrap and
consolidation boundaries, while the live footer tracks bootstrap/contribution progress, active
peers, shared work, and message counts. Use `/agents`, `/work`, `/messages`, or `/groups` for detail.

`zero` automatically loads the `pentest.md` in the current target directory. This is a bounded
assessment, so “all vulnerabilities” is the investigation goal—not a guarantee of exhaustive
coverage.

`--max-turns 25` gives the orchestrator up to 25 turns for the current goal. Pentester and
validator subagents use their own turn budgets. At the limit, `zero` waits for running subagents,
writes `.pentest/report.md`, and returns to the interactive prompt. The session stays open, so you
can discuss the findings or provide another goal; that next goal starts with a fresh turn budget.

During the run, some tool calls pause for permission. Press `y` to allow once, `n` to deny, or
`a` to always allow that tool for the rest of the session. To configure automatic approvals when
starting `zero`, see [Approval controls](#approval-controls).

### Provide files to the agents

Place text files or folders in the engagement's `zero_files/` directory before or during a run:

```bash
cp /path/to/request.txt zero_files/
cp -R /path/to/source-snapshot zero_files/
```

Agents access them with the built-in `list_dir`, `grep`, and `read_file` tools. These tools are
read-only, use paths relative to `zero_files/`, and cannot access files outside that directory.
Use `--files-root <path>` to provide a different directory.

### Run output

| Path | Contents |
| --- | --- |
| `.pentest/report.md` | Confirmed findings and rejected or failed test paths |
| `.pentest/findings/` | Individual confirmed, rejected, and pending findings |
| `.pentest/audit.jsonl` | Full event and tool-call audit trail |
| `.pentest/thinking.log` | Readable reasoning from the orchestrator and subagents |
| `.pentest/llm/` | Full model input/output traces, enabled by default |
| `.pentest/memory/`, `.pentest/todo/` | Durable per-agent context and plans |

## Configuration

Most runs only need `pentest.md`, `--scope-host`, and a goal. CLI backends use existing
authentication; the API backend reads the selected provider's credential environment.

| Option | Default | Use it to |
| --- | --- | --- |
| `--backend <name>` | `codex` | Select `codex`, `claude`, or direct model API access through `api` |
| `--model <id>` | Codex CLI default; `claude-opus-5[1m]` for Claude | Pin one model for every agent |
| `--scope-host <host>` | None | Route an in-scope host away from the safe `web_fetch` tool; repeat per host |
| `--max-turns <n>` | `10` | Bound orchestrator turns |
| `--swarm-size <n>` | `1` | Run fixed-size collaborating top-level peers in mode `c` |
| `--max-subagents <n>` | `10` | Limit concurrent pentester and validator children **per swarm peer** |
| `--max-total-subagents <n>` | `0` (unlimited) | Cap total children **per swarm peer** (`N × cap` maximum for a swarm of size `N`) |
| `--rate-limit <spec>` | `100/min` | Limit requests per host, for example `5/sec` |
| `--files-root <path>` | `./zero_files` | Expose local files to the jailed read-only file tools |
| `--no-container` | Container enabled | Explicitly run tools on the host |
| `--no-llm-log` | Logging enabled | Disable full per-agent model I/O traces |

See every option with:

```bash
uv run zero run --help
```

### API models through LiteLLM

Set your provider key in the host environment, for example `OPENROUTER_API_KEY`, then run:

```bash
uv run zero run --backend api --model openrouter/z-ai/glm-5.3 --goal "..."
uv run zero run --backend api --model openrouter/z-ai/glm-5.3 --ralph --goal "..."
uv run zero run --backend api --model openrouter/z-ai/glm-5.3 --swarm-size 3 --goal "..."
```

LiteLLM runs inside Zero and calls the selected provider. No OpenCode installation or
LiteLLM gateway is needed. Use an explicit LiteLLM `provider/model` identifier; hosted
open models and commercial models use the same agent/tool workflow. API access supports
modes `a` and `c`, all Ralph phases/resume, and swarm peers, children, validation, and
summarization. Ralph and swarm sizes above one remain mutually exclusive. Mode `b`
requires the Claude Code MCP backend and rejects `--backend api`.

For a hosted or self-hosted OpenAI-compatible chat endpoint:

```bash
# Set MODEL_API_KEY in the environment first.
uv run zero run --backend api --model openai/organization/model-name \
  --api-base-url https://inference.example.com/v1 --api-key-env MODEL_API_KEY \
  --context-window 131072 --goal "..."
```

`--api-base-url` follows the provider's LiteLLM convention: OpenAI-compatible endpoints
usually include `/v1`; Anthropic uses the server root because its adapter adds
`/v1/messages`. Do not include a completion endpoint or credentials in the URL.
`--api-key-env` overrides standard credential discovery without putting a key in argv.
Other providers can use their standard LiteLLM credential environment.

The API output cap defaults to 16384 tokens, including provider-counted reasoning;
adjust `--api-max-output-tokens` if a reasoning model exhausts its output before emitting
an envelope. `--api-reasoning-effort` passes an explicit supported effort value.
`--api-response-format auto|json|text` defaults to capability-aware JSON/text selection.
Every response is validated by Zero's envelope parser. The swarm summarizer uses plain text.

`--api-max-concurrency` defaults to 4 across the whole engagement, including all swarm
peers and Ralph roles; it is separate from target-host rate limits. `--api-retries`
defaults to 2 transient retries; `--llm-timeout` bounds a whole backend call including
queueing, compression, attempts and backoff. Authentication and configuration errors
stop the current actor without repeatedly spending its turn budget.

Context limits come from bundled metadata, with a conservative fallback for the initial
GLM 5.3 integration. Unknown model IDs require `--context-window`. API actors keep local
history and compress older turns near the limit while retaining recent turns and the
original audit history. Token estimates are conservative and do not download tokenizers.
Provider/model compatibility does not imply equal finding quality; new models should
be checked with the opt-in capability regression suite.

Default unit and local API end-to-end tests use mocks or dummy keys and spend no credits.
Live provider tests require an explicit model and credentials:

```bash
ZERO_TEST_API_MODEL=openrouter/z-ai/glm-5.3 \
  uv run pytest tests/e2e/test_live_api.py -m live_api -q
uv run pytest -m regression --regression-backend api \
  --regression-model openrouter/z-ai/glm-5.3 -k dvwa
```

The live tests also accept `ZERO_TEST_API_BASE_URL`, `ZERO_TEST_API_KEY_ENV`, and
`ZERO_TEST_API_CONTEXT_WINDOW`. Regression equivalents are `--regression-api-base-url`,
`--regression-api-key-env`, and `--regression-context-window`. All live API and capability
regression tests remain excluded from the default test run and spend provider credits
when explicitly selected. Loopback transport tests cover OpenAI-compatible and Anthropic
request formats; live compatibility must be verified with your chosen provider/model.

### Approval controls

- With no approval flags, tool actions prompt in the terminal.
- `--yolo` auto-approves safe and active actions only.
- Exploit actions require both `--auto-approve exploit` and `--i-understand`.
- Destructive actions always prompt.
- Host-executing tools cannot be auto-approved unless `--allow-host-exec` is also set.

## How it works

`pentest.md + goal → orchestrator → parallel pentesters → independent validators → report`

Candidate findings remain pending until a validator reinvestigates them from scratch. Confirmed
findings are deduplicated and scored with CVSS 4.0 before the final report is written.

## Useful commands

| Command | Description |
| --- | --- |
| `uv run zero init` | Scaffold an engagement workspace without overwriting `pentest.md` |
| `uv run zero run --goal "..."` | Start an interactive engagement |
| `uv run zero replay .pentest/audit.jsonl` | Replay recorded assistant messages from a run |
| `uv run zero reset` | Delete `.pentest/` after confirmation |
| `uv run zero --help` | List top-level CLI commands |

During a run:

| Input | Description |
| --- | --- |
| `/running` | Show active pentester and validator agents |
| `/agents`, `/work` | Inspect swarm peers and the shared work ledger |
| `/messages`, `/groups`, `/history` | Inspect durable swarm coordination state |
| `/respond-now` | Freeze current contributions and finish the active swarm response |
| `/findings` | List confirmed findings |
| `/show-rejected` | List rejected findings |
| `/scope` | Display the loaded `pentest.md` |
| `/help` | List all shell commands |
| `Ctrl+C` | Cancel the current turn; press twice within two seconds to exit |

## Advanced usage

Use Claude Code instead of Codex:

```bash
uv run zero run --backend claude \
  --scope-host host.docker.internal \
  --goal "Assess http://host.docker.internal:3000"
```

Use Ralph mode for a reviewed task plan followed by bounded orchestrated passes:

```bash
uv run zero run \
  --ralph \
  --ralph-iterations 5 \
  --yolo \
  --max-total-subagents 20 \
  --scope-host host.docker.internal \
  --goal "Assess http://host.docker.internal:3000"
```

Resume it with `--ralph --ralph-resume`. `--yolo` still does not approve exploit or destructive
actions.

For external capability evaluation, see the
[BountyBench integration](benchmarks/bountybench/README.md).

## Important notes

- `pentest.md` is passed to the agents as context; it is not a network firewall.
  `--scope-host` only blocks that hostname in the safe `web_fetch` tool. Other security tools are
  not restricted to hosts listed in `pentest.md`.
- If the Docker sandbox cannot start, `zero` prints a warning and falls back to host execution.
  Host code and binary calls then require approval unless `--allow-host-exec` is enabled.
- The sandbox mounts the engagement directory read/write at `/work`. Run `zero` from a dedicated
  engagement directory when the current directory contains unrelated sensitive files.
- `.pentest/audit.jsonl`, `thinking.log`, and `.pentest/llm/` can contain credentials, payloads,
  and full prompts. Protect or delete them according to the engagement's data-handling rules.
- Live runs consume the selected backend's subscription or API allowance. Set
  `--max-total-subagents` when you need a hard subagent cost cap.

## Development

```bash
uv run pytest tests/unit -q  # offline unit tests; no Docker or LLM calls
uv run pytest -q             # unit + Docker tests; live/token-spending tests are excluded
uv run ruff check .
uv build
```

The Docker tests skip with guidance when the sandbox image is unavailable. Live and regression
runs are opt-in; see [`tests/regression/`](tests/regression/) and the
[BountyBench guide](benchmarks/bountybench/README.md) before running token-spending suites.

For implementation details, see [`architecture.md`](architecture.md).

## License

Licensed under the [GNU General Public License v3.0](LICENSE).
