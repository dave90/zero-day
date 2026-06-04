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
    type=click.Choice(["claude"]),
    default="claude",
    show_default=True,
    help="LLM backend. Shells out to the authenticated Claude Code CLI.",
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
    default=60,
    show_default=True,
    help="Per-call `claude -p --max-turns` for the Validator backend.",
)
@click.option(
    "--subagent-timeout",
    type=int,
    default=900,
    show_default=True,
    help="Per-call `claude -p` wall-clock timeout (seconds) for pentester/validator"
    " subagents. Raise it for heavy iterative exploitation; the orchestrator's own"
    " backend keeps the standard 300s.",
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
    " between turns. --subagent-timeout only bounds a single `claude -p` call.",
)
@click.option(
    "--max-subagents",
    type=int,
    default=10,
    show_default=True,
    help="Max concurrent subagents in orchestrated mode (c).",
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
    "--burp-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Host directory mounted ro at /work/burp inside the sandbox.",
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
def run(
    goal: str | None,
    backend: str,
    execution_mode: str,
    auto_approve: tuple[str, ...],
    yolo: bool,
    i_understand: bool,
    allow_host_exec: bool,
    scope_host: tuple[str, ...],
    max_turns: int,
    validator_max_turns: int,
    validator_claude_max_turns: int,
    subagent_timeout: int,
    pentester_max_turns: int,
    subagent_deadline: int,
    max_subagents: int,
    container: bool,
    container_image: str,
    host_network: bool,
    burp_dir: Path | None,
    container_cpus: float,
    container_memory: str,
    remote_all_tools: bool,
    rate_limit: str,
) -> None:
    """Run an interactive pentest session."""
    from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig

    # No implicit auto-approve policy: every tool call (orchestrator's own and
    # its subagents') goes through the ApprovalGate and prompts unless the user
    # opts out with --auto-approve / --yolo. Use --yolo for unattended runs.
    approve = set(auto_approve)

    cfg = OrchestratorConfig(
        cwd=Path.cwd(),
        goal=goal,
        backend=backend,
        auto_approve=approve,
        yolo=yolo,
        i_understand=i_understand,
        allow_host_exec=allow_host_exec,
        scope_hosts=set(scope_host),
        max_turns=max_turns,
        validator_max_turns=validator_max_turns,
        validator_claude_max_turns=validator_claude_max_turns,
        subagent_timeout_seconds=subagent_timeout,
        pentester_max_turns=pentester_max_turns,
        subagent_deadline_seconds=subagent_deadline,
        max_concurrent_subagents=max_subagents,
        use_container=container,
        container_image=container_image,
        host_network=host_network,
        burp_dir=burp_dir,
        container_cpus=container_cpus,
        container_memory=container_memory,
        remote_all_tools=remote_all_tools,
        rate_limit=rate_limit,
        execution_mode=execution_mode,
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
    for sub in ("findings/confirmed", "findings/rejected", "findings/pending", "traffic"):
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    click.echo(f"scaffolded {pdir}/")


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
