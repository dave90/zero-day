"""Top-level CLI for `zero`."""
from __future__ import annotations

import asyncio
from pathlib import Path

import click


DEFAULT_PENTEST_MD = """\
# pentest.md

The agent reads this file at the top of every run. Put scope, credentials,
goals, and any out-of-band notes here. The contents are passed verbatim to
the agent — there is no parser in Phase 1.

## Scope

- in scope:
- out of scope:

## Credentials

- low:  user=  password=
- high: user=  password=

## Goal

(describe what to look for)
"""


@click.group()
@click.version_option()
def main() -> None:
    """zero: autonomous web-app pentesting agent."""


@main.command()
@click.option("--goal", default=None, help="One-shot goal seeded as the first user message.")
@click.option(
    "--backend",
    type=click.Choice(["claude", "codex"]),
    default="codex",
    show_default=True,
    help="LLM backend. Shells out to the authenticated Claude Code CLI ('claude') or"
    " the OpenAI Codex CLI ('codex'). With 'codex', pass a Codex --model or omit it.",
)
@click.option(
    "--execution-mode",
    type=click.Choice(["a", "b", "c"]),
    default="c",
    show_default=True,
    help=(
        "Execution mode: c=orchestrated (DEFAULT) — a senior-pentester orchestrator"
        " dispatches concurrent pentester/validator subagents; a=single agent;"
        " b=MCP delegated. Tool calls prompt for approval unless you pass"
        " --auto-approve/--yolo (use --yolo for unattended runs)."
    ),
)
@click.option(
    "--swarm-size",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
    help="Top-level collaborating orchestrators (mode c only). Size 1 preserves legacy mode c.",
)
@click.option(
    "--auto-approve",
    multiple=True,
    type=click.Choice(["safe", "active", "exploit"]),
    help="Auto-approve tools at this risk class. Repeatable.",
)
@click.option(
    "--yolo",
    is_flag=True,
    help="Auto-approve safe + active tool calls. Does NOT cover exploit"
    " (use --auto-approve exploit --i-understand) or destructive (always prompts).",
)
@click.option("--i-understand", is_flag=True, help="Required together with auto-approve=exploit.")
@click.option(
    "--allow-host-exec",
    is_flag=True,
    help=(
        "Allow code/binary tools (python_exec, sqlmap, nmap, …) to be"
        " AUTO-APPROVED while running on the host. Off by default: without it,"
        " host-executing tools always prompt unless sandboxed with --container."
    ),
)
@click.option("--scope-host", multiple=True, help="In-scope host for web_fetch (repeatable).")
@click.option(
    "--max-turns",
    type=int,
    default=10,
    show_default=True,
    help="Orchestrator turn budget. It is told its turns-remaining and stops"
    " opening new attack ideas as the budget runs out.",
)
@click.option(
    "--validator-max-turns",
    type=int,
    default=40,
    show_default=True,
    help="Validator investigation steps (its own turn budget, separate from --max-turns).",
)
@click.option(
    "--validator-claude-max-turns",
    type=int,
    default=1,
    show_default=True,
    help="claude's INTERNAL per-call --max-turns for the Validator (NOT its"
    " investigation budget — that's --validator-max-turns). Keep 1: we drive the"
    " loop, and a higher value lets the model drift into a native tool call.",
)
@click.option(
    "--llm-timeout",
    type=int,
    default=900,
    show_default=True,
    help="Per-call LLM backend wall-clock timeout (seconds), shared by the"
    " orchestrator AND the pentester/validator subagents (one value for all)."
    " Raise it for heavy iterative exploitation / large-context turns.",
)
@click.option(
    "--pentester-max-turns",
    type=int,
    default=14,
    show_default=True,
    help="Per-subagent turn budget for pentesters (no longer inherits --max-turns,"
    " so one attack idea can't run 40+ turns).",
)
@click.option(
    "--subagent-deadline",
    type=int,
    default=1200,
    show_default=True,
    help="Hard wall-clock ceiling (seconds) for ONE subagent's whole run, checked"
    " between turns. --llm-timeout only bounds a single backend call.",
)
@click.option(
    "--max-subagents",
    type=int,
    default=10,
    show_default=True,
    help="Max CONCURRENT subagents in orchestrated mode (c) — how many run at once.",
)
@click.option(
    "--max-total-subagents",
    type=int,
    default=0,
    show_default=True,
    help="Hard cap on the TOTAL subagents (pentesters + validators) dispatched over the"
    " whole run (orchestrated mode c). 0 = unlimited. Cost-control knob.",
)
@click.option(
    "--container/--no-container",
    default=True,
    show_default=True,
    help=(
        "Run tool calls inside the Docker sandbox (image: zero-sandbox). ON by"
        " default (secure). Falls back to host execution if Docker is"
        " unavailable; --no-container forces host execution."
    ),
)
@click.option(
    "--container-image",
    default="zero-sandbox:latest",
    show_default=True,
    help="Sandbox image tag.",
)
@click.option(
    "--host-network",
    is_flag=True,
    help="Run the sandbox on the host network (needed for Burp on 127.0.0.1).",
)
@click.option(
    "--session-resume/--no-session-resume",
    default=True,
    show_default=True,
    help=(
        "Keep one persistent `claude` session per actor and send only each new turn"
        " via --resume (claude holds + caches the history) instead of re-shipping the"
        " whole transcript every call. Big token saver; claude-backend only."
    ),
)
@click.option(
    "--burp-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Host directory mounted ro at /work/burp inside the sandbox.",
)
@click.option(
    "--files-root",
    type=click.Path(path_type=Path),
    default="zero_files",
    show_default=True,
    help="Root folder the read-only fs tools (list_dir/grep/read_file) are jailed to; "
    "resolved against cwd, created if missing, mounted at /zero_files in the sandbox.",
)
@click.option(
    "--container-cpus",
    type=float,
    default=4.0,
    show_default=True,
    help="CPU cap for the sandbox.",
)
@click.option(
    "--container-memory",
    default="4g",
    show_default=True,
    help="Memory cap for the sandbox.",
)
@click.option(
    "--remote-all-tools/--no-remote-all-tools",
    default=True,
    show_default=True,
    help=(
        "Proxy EVERY tool through the sandbox (not just python_exec). ON by"
        " default so binary tools (sqlmap, nmap, …) are sandboxed too."
    ),
)
@click.option(
    "--rate-limit",
    default="100/min",
    show_default=True,
    help="Per-host rate cap, '<int>/sec' or '<int>/min'. Set to '0/sec' to disable.",
)
@click.option(
    "--thought-display",
    type=click.Choice(["condensed", "full", "off"]),
    default="condensed",
    show_default=True,
    help="How much of the agent's reasoning shows in the chat: condensed (one"
    " line), full, or off. The complete thought is always kept in thinking.log.",
)
@click.option(
    "--model",
    default="claude-opus-5[1m]",
    show_default=True,
    help="Model for every LLM call (orchestrator + subagents + validators). For"
    " Claude, use an alias ('opus', 'sonnet') or full id; 'claude-opus-5[1m]' is"
    " Opus 5 with the 1M-context window. Codex ignores this Claude default and uses"
    " its configured default model unless you pass a Codex model id. Pass '' to use"
    " the selected backend's account default.",
)
@click.option(
    "--context-window",
    type=int,
    default=None,
    help="Context-window size for the UI's per-agent 'ctx used/window (%)' display."
    " Default: resolved per backend at startup — Codex reads its real model window"
    " (~272k) at runtime; Claude infers 1M for a '[1m]' model, else 200k.",
)
@click.option(
    "--llm-log/--no-llm-log",
    default=True,
    show_default=True,
    help="Trace the FULL model I/O (argv + system prompt + stdin in, stdout/stderr out)"
    " to one file per agent under ./.pentest/llm/<actor>.log. ON by default; pass"
    " --no-llm-log to disable. ($ZERO_LLM_LOG_DIR overrides the location.)",
)
@click.option(
    "--ralph",
    is_flag=True,
    help="Ralph mode: an analyzer/analyzer_validator build a PRD of pentest tasks,"
    " then zero loops bounded orchestrated passes over it (each works the"
    " top-priority task, records progress, snapshots+resets .pentest/). Wraps"
    " mode c. Run with --yolo (and --auto-approve exploit --i-understand for"
    " unattended exploitation).",
)
@click.option(
    "--ralph-iterations",
    type=int,
    default=5,
    show_default=True,
    help="Ralph: number of loop iterations (stops early when all PRD tasks complete)."
    " With --ralph-resume these are ADDITIONAL iterations on top of the prior run.",
)
@click.option(
    "--ralph-resume",
    is_flag=True,
    help="Ralph: resume the prior run from ./.ralph/ — load the existing prd.json"
    " (keeping completed/pending task state) instead of re-planning, skip the PRD"
    " discussion + approval, and continue iteration numbering from the last backup."
    " Confirmed findings and progress.txt already persist.",
)
@click.option(
    "--ralph-approve-prd/--no-ralph-approve-prd",
    default=True,
    show_default=True,
    help="Ralph: pause for operator approval of the PRD after the analyzer↔validator"
    " discussion, before the loop. The loop iterations always run unattended.",
)
@click.option(
    "--ralph-discussion-rounds",
    type=int,
    default=4,
    show_default=True,
    help="Ralph: max analyzer↔analyzer_validator review rounds before the PRD is"
    " accepted as-is (reused for both the epic and task phases).",
)
@click.option(
    "--ralph-epics/--no-ralph-epics",
    default=True,
    show_default=True,
    help="Ralph: two-phase planning — first agree a set of EPICS that partition the"
    " attack surface, then decompose each into atomic tasks (every epic gets >=1 task,"
    " checked by the validator). --no-ralph-epics uses single-phase task planning.",
)
@click.option(
    "--analyzer-validator-max-turns",
    type=int,
    default=6,
    show_default=True,
    help="Ralph: turn budget for the analyzer_validator (PRD review + completion check).",
)
def run(
    goal: str | None,
    backend: str,
    execution_mode: str,
    swarm_size: int,
    auto_approve: tuple[str, ...],
    yolo: bool,
    i_understand: bool,
    allow_host_exec: bool,
    scope_host: tuple[str, ...],
    max_turns: int,
    validator_max_turns: int,
    validator_claude_max_turns: int,
    llm_timeout: int,
    pentester_max_turns: int,
    subagent_deadline: int,
    max_subagents: int,
    max_total_subagents: int,
    container: bool,
    container_image: str,
    host_network: bool,
    session_resume: bool,
    burp_dir: Path | None,
    files_root: Path,
    container_cpus: float,
    container_memory: str,
    remote_all_tools: bool,
    rate_limit: str,
    thought_display: str,
    model: str,
    context_window: int | None,
    llm_log: bool,
    ralph: bool,
    ralph_iterations: int,
    ralph_resume: bool,
    ralph_approve_prd: bool,
    ralph_discussion_rounds: int,
    ralph_epics: bool,
    analyzer_validator_max_turns: int,
) -> None:
    """Run an interactive pentest session."""
    from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig

    # Per-agent LLM I/O tracing is ON by default; the orchestrator enables it from
    # cfg.llm_log (→ ./.pentest/llm/<actor>.log). --no-llm-log turns it off.

    # `--model ''` ⇒ account default. Leave the context-window denominator unset
    # (None) so the orchestrator resolves it backend-aware at startup — Codex reports
    # its real ~272k window at runtime, Claude infers 1M/200k from the model id. An
    # explicit --context-window is passed through and always honored.
    model_or_none = model or None

    # No implicit auto-approve policy: every tool call (orchestrator's own and
    # its subagents') goes through the ApprovalGate and prompts unless the user
    # opts out with --auto-approve / --yolo. Use --yolo for unattended runs.
    approve = set(auto_approve)

    if swarm_size > 1 and execution_mode != "c":
        raise click.UsageError("--swarm-size > 1 is supported only with --execution-mode c")
    if swarm_size > 1 and ralph:
        raise click.UsageError("--swarm-size > 1 cannot be combined with --ralph")

    cfg = OrchestratorConfig(
        cwd=Path.cwd(),
        goal=goal,
        backend=backend,
        session_resume=session_resume,
        auto_approve=approve,
        yolo=yolo,
        i_understand=i_understand,
        allow_host_exec=allow_host_exec,
        scope_hosts=set(scope_host),
        max_turns=max_turns,
        validator_max_turns=validator_max_turns,
        validator_claude_max_turns=validator_claude_max_turns,
        llm_timeout_seconds=llm_timeout,
        pentester_max_turns=pentester_max_turns,
        subagent_deadline_seconds=subagent_deadline,
        max_concurrent_subagents=max_subagents,
        max_total_subagents=max_total_subagents,
        use_container=container,
        container_image=container_image,
        host_network=host_network,
        burp_dir=burp_dir,
        files_root=files_root,
        container_cpus=container_cpus,
        container_memory=container_memory,
        remote_all_tools=remote_all_tools,
        rate_limit=rate_limit,
        execution_mode=execution_mode,
        swarm_size=swarm_size,
        thought_display=thought_display,
        model=model_or_none,
        context_window=context_window,
        llm_log=llm_log,
        ralph=ralph,
        ralph_iterations=ralph_iterations,
        ralph_resume=ralph_resume,
        ralph_approve_prd=ralph_approve_prd,
        ralph_discussion_rounds=ralph_discussion_rounds,
        ralph_epics=ralph_epics,
        analyzer_validator_max_turns=analyzer_validator_max_turns,
    )
    orch = Orchestrator(cfg)
    asyncio.run(orch.run_interactive())


@main.command()
def init() -> None:
    """Scaffold ./pentest.md and ./.pentest/ in the current directory."""
    cwd = Path.cwd()
    pentest_md = cwd / "pentest.md"
    if pentest_md.exists():
        click.echo(f"{pentest_md} already exists; leaving it alone.")
    else:
        pentest_md.write_text(DEFAULT_PENTEST_MD, encoding="utf-8")
        click.echo(f"wrote {pentest_md}")

    pdir = cwd / ".pentest"
    for sub in ("findings/confirmed", "findings/rejected", "findings/pending", "traffic", "memory"):
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    click.echo(f"scaffolded {pdir}/")

    # The read-only files jail the agent's list_dir/grep/read_file tools are
    # locked to (config default --files-root). It IS auto-created at run time,
    # but scaffolding it here makes it visible up front so the operator can drop
    # target files in before launching.
    files_root = cwd / "zero_files"
    files_root.mkdir(parents=True, exist_ok=True)
    click.echo(f"created {files_root}/ (drop files here for the agent to read)")


@main.command()
@click.confirmation_option(prompt="Delete ./.pentest/ ?")
def reset() -> None:
    """Remove ./.pentest/ in the current directory."""
    import shutil

    pdir = Path.cwd() / ".pentest"
    if pdir.exists():
        shutil.rmtree(pdir)
        click.echo(f"removed {pdir}")
    else:
        click.echo(f"{pdir} does not exist")


@main.command()
@click.argument("audit_path", type=click.Path(exists=True, path_type=Path))
@click.option("--goal", default=None, help="Optional new goal; defaults to recorded.")
def replay(audit_path: Path, goal: str | None) -> None:
    """Replay a session from an audit.jsonl file (M7)."""
    from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig

    cfg = OrchestratorConfig(
        cwd=Path.cwd(),
        goal=goal,
        backend="replay",
        replay_audit_path=audit_path,
        yolo=True,  # don't prompt for approvals — we're replaying
        rate_limit="0/sec",
    )
    orch = Orchestrator(cfg)
    asyncio.run(orch.run_interactive())


if __name__ == "__main__":
    main()
