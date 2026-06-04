"""Slash-command parser & dispatcher.

A handler returns either:
  None             — no output, command consumed
  str              — single line of output to append to the scrollback
  list[str]        — multiple lines

If the handler raises SystemExit, the UI quits.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


HandlerReturn = None | str | list[str]


@dataclass
class Command:
    name: str
    description: str
    handler: Callable[[list[str]], HandlerReturn]


class CommandRegistry:
    def __init__(self) -> None:
        self._cmds: dict[str, Command] = {}

    def register(self, cmd: Command) -> None:
        self._cmds[cmd.name] = cmd

    def names(self) -> list[str]:
        return sorted(self._cmds)

    def get(self, name: str) -> Command | None:
        return self._cmds.get(name)

    def dispatch(self, line: str) -> tuple[bool, HandlerReturn]:
        """Return (was_command, output). If not a slash command, was_command=False."""
        if not line.startswith("/"):
            return (False, None)
        parts = line[1:].strip().split()
        if not parts:
            return (True, None)
        name = parts[0]
        args = parts[1:]
        cmd = self._cmds.get(name)
        if cmd is None:
            return (True, f"unknown command: /{name}. Try /help.")
        return (True, cmd.handler(args))


def build_default_registry(
    *,
    findings_dir: Path,
    clear_scrollback: Callable[[], None],
    request_exit: Callable[[], None],
    running_snapshot: Callable[[], list[str]] | None = None,
) -> CommandRegistry:
    reg = CommandRegistry()

    def help_cmd(_args: list[str]) -> list[str]:
        return [
            "available commands:",
            *[f"  /{c.name:<14} {c.description}" for c in _all(reg)],
        ]

    def exit_cmd(_args: list[str]) -> None:
        request_exit()
        return None

    def clear_cmd(_args: list[str]) -> None:
        clear_scrollback()
        return None

    def scope_cmd(_args: list[str]) -> str:
        p = Path.cwd() / "pentest.md"
        if not p.exists():
            return "no pentest.md in cwd"
        return p.read_text(encoding="utf-8")

    def findings_cmd(_args: list[str]) -> list[str]:
        confirmed = findings_dir / "confirmed"
        if not confirmed.exists():
            return ["(no findings dir yet — run init)"]
        files = sorted(confirmed.glob("*.md"))
        if not files:
            return ["(no confirmed findings)"]
        return [f"- {f.name}" for f in files]

    def show_rejected_cmd(_args: list[str]) -> list[str]:
        rejected = findings_dir / "rejected"
        if not rejected.exists():
            return ["(no findings dir yet)"]
        files = sorted(rejected.glob("*.md"))
        if not files:
            return ["(no rejected findings)"]
        return [f"- {f.name}" for f in files]

    def replay_cmd(_args: list[str]) -> str:
        return "/replay is implemented as the `zero replay` CLI subcommand (M7)."

    def mode_cmd(_args: list[str]) -> str:
        return (
            "Execution mode is chosen at launch with --execution-mode {a,b,c}; "
            "default c (orchestrated: a senior-pentester orchestrator dispatches "
            "concurrent pentester/validator subagents). a=single agent, b=MCP delegated."
        )

    def running_cmd(_args: list[str]) -> list[str]:
        if running_snapshot is None:
            return ["(no live-subagent view available)"]
        return running_snapshot()

    def keys_cmd(_args: list[str]) -> list[str]:
        return [
            "keyboard shortcuts:",
            "",
            "  input:",
            "    Enter             submit",
            "    Ctrl+C            cancel current turn (twice within 2 s → exit)",
            "",
            "  scrollback:",
            "    rendered inline — use your terminal's NATIVE scroll",
            "    (mouse wheel / trackpad) to scroll through history.",
            "",
            "  approval / ask_user (shown inline):",
            "    y / n / a    approve / deny / always-this-tool",
            "    1..9         pick numbered ask_user option",
            "    Esc          deny a pending approval",
            "    …or type the answer and press Enter.",
            "",
            "  copy / paste:",
            "    select with mouse, then Cmd+C (Mac) or Ctrl+Shift+C (Linux).",
        ]

    reg.register(Command("help", "show this help", help_cmd))
    reg.register(Command("keys", "show keyboard shortcuts", keys_cmd))
    reg.register(Command("exit", "exit the program", exit_cmd))
    reg.register(Command("quit", "exit the program", exit_cmd))
    reg.register(Command("clear", "clear the scrollback", clear_cmd))
    reg.register(Command("scope", "print pentest.md", scope_cmd))
    reg.register(Command("findings", "list confirmed findings", findings_cmd))
    reg.register(Command("show-rejected", "list rejected findings", show_rejected_cmd))
    reg.register(Command("running", "list running subagents (orchestrated mode)", running_cmd))
    reg.register(Command("replay", "info about replay (use the CLI)", replay_cmd))
    reg.register(Command("mode", "show the execution mode", mode_cmd))
    return reg


def _all(reg: CommandRegistry) -> Iterable[Command]:
    for name in reg.names():
        c = reg.get(name)
        if c is not None:
            yield c
