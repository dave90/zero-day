"""`ZeroExecutorAgent` — a drop-in replacement for BountyBench's `ExecutorAgent`.

In BountyBench a phase round-robins `ExecutorAgent` (the LLM agent that produces an
`exploit.sh`) and then the `Detect`/`Exploit` agent (the fixed scorer that runs that
script against patched/unpatched commits). This agent replaces the *executor*: its
single `run()` drives an entire `zero` engagement against the bounty target, then drops
the resulting `exploit.sh` into `tmp_dir/exploit_files/` and signals `submission=True`,
so BountyBench's unchanged scorer grades it authoritatively.

It does NOT use BountyBench's MODEL resource — `zero` calls its own backend (claude/codex),
so the workflow is run with `--use_mock_model`. The model under test and run budget are
read from `ZERO_BB_*` environment variables (set by `run_pilot.py`).

This module imports BountyBench's own modules and only runs *inside* a BB checkout.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from agents.base_agent import AgentConfig, BaseAgent
from messages.agent_messages.agent_message import AgentMessage
from messages.agent_messages.executor_agent_message import ExecutorAgentMessage
from resources.resource_type import ResourceType
from utils.logger import get_main_logger

from .zero_driver import (
    build_bountybench_goal,
    run_zero_engagement,
    synthesize_exploit_from_finding,
)

logger = get_main_logger(__name__)


@dataclass
class ZeroExecutorAgentConfig(AgentConfig):
    """Everything `zero` needs, gathered by the phase shim from the workflow."""

    target_host: str
    tmp_dir: Path
    task_dir: Path
    task_type: str = "exploit"     # "exploit" | "detect"
    system: str = "target"
    bounty_number: int = 0
    writeup: str = ""
    cwe: Optional[str] = None
    info: str = ""

    def __str__(self) -> str:  # keep logs tidy
        return f"ZeroExecutorAgentConfig(system={self.system}, bounty={self.bounty_number})"


def _zero_settings_from_env() -> dict:
    """Read the zero backend/budget knobs from the environment (set by run_pilot)."""
    def _int(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, "") or default)
        except ValueError:
            return default

    use_container = os.environ.get("ZERO_BB_NO_CONTAINER", "").lower() not in ("1", "true", "yes")
    return {
        "backend": os.environ.get("ZERO_BB_BACKEND", "claude"),
        "model": os.environ.get("ZERO_BB_MODEL") or None,
        "deadline_seconds": _int("ZERO_BB_DEADLINE", 1200),
        "max_turns": _int("ZERO_BB_MAX_TURNS", 30),
        "use_container": use_container,
        "container_network": os.environ.get("ZERO_BB_NETWORK", "shared_net") or None,
    }


def _normalize_target(host: str) -> str:
    """BB's target_host is sometimes a bare host:port — give zero a URL."""
    host = (host or "").strip()
    if host and "://" not in host:
        return "http://" + host
    return host


class ZeroExecutorAgent(BaseAgent):
    # We pass everything zero needs via the config, so only require INIT_FILES
    # (which scaffolds tmp_dir/exploit_files and checks out the codebase). All
    # phase resources — target server (REPO/BOUNTY setup), Kali env, docker —
    # are initialized by the phase regardless; we just need the target up.
    REQUIRED_RESOURCES = [ResourceType.INIT_FILES]
    OPTIONAL_RESOURCES = [ResourceType.REPO_SETUP, ResourceType.BOUNTY_SETUP]
    ACCESSIBLE_RESOURCES = [ResourceType.INIT_FILES]

    def __init__(self, agent_id: str, agent_config: ZeroExecutorAgentConfig):
        self.cfg = agent_config
        self.tmp_dir = Path(agent_config.tmp_dir)
        self.exploit_files_dir = self.tmp_dir / "exploit_files"
        # zero runs its engagement in a scratch dir under the bounty dir (NOT under
        # tmp_dir, which BB wipes after scoring) so its .pentest/ audit + zero_files/
        # survive the run for inspection and token accounting. ZERO_BB_KEEP can point
        # it elsewhere.
        keep_root = os.environ.get("ZERO_BB_WORKDIR")
        base = Path(keep_root) if keep_root else (Path(agent_config.task_dir)
                / "bounties" / f"bounty_{agent_config.bounty_number}" / "zero_runs")
        # MUST be absolute: zero bind-mounts cwd into its sandbox, and Docker rejects
        # a relative `-v` source as an invalid volume name. BB passes a relative
        # --task_dir (e.g. bountytasks/lunary), so resolve against the process cwd.
        self.engagement_dir = (base / f"{agent_config.task_type}_{self.tmp_dir.name}").resolve()
        super().__init__(agent_id, agent_config)

    async def run(self, messages: List[AgentMessage]) -> ExecutorAgentMessage:
        prev = messages[0] if messages else None
        msg = ExecutorAgentMessage(agent_id=self.agent_id, prev=prev)

        target = _normalize_target(self.cfg.target_host)
        settings = _zero_settings_from_env()
        goal = build_bountybench_goal(self.cfg.task_type, target)

        logger.info(
            f"[zero] starting engagement: system={self.cfg.system} "
            f"bounty={self.cfg.bounty_number} task={self.cfg.task_type} "
            f"target={target} backend={settings['backend']} model={settings['model']}"
        )

        verify_files = (
            self._read_verify_files() if self.cfg.task_type == "exploit" else None
        )

        result = await run_zero_engagement(
            cwd=self.engagement_dir,
            target_host=target,
            writeup=self.cfg.writeup,
            goal=goal,
            system=self.cfg.system,
            bounty_number=self.cfg.bounty_number,
            cwe=self.cfg.cwe,
            info=self.cfg.info,
            verify_files=verify_files,
            **settings,
        )

        wrote = self._stage_exploit(result)
        confirmed = len(result.confirmed)
        if wrote:
            msg.set_submission(True)
            msg.set_message(
                f"zero engagement complete: confirmed_findings={confirmed}, "
                f"exploit.sh staged. error={result.error or 'none'}"
            )
            logger.status("zero produced an exploit.sh", True)
        else:
            msg.set_message(
                f"zero engagement produced no exploit.sh "
                f"(confirmed_findings={confirmed}, error={result.error or 'none'})"
            )
            logger.status("zero produced no exploit.sh", False)
        return msg

    # Exploit-only verifier files. Stock BB exposes these to Exploit agents and tells
    # them to inspect them; stock Detect does not receive verifier files.
    _VERIFY_MAX_BYTES = 64 * 1024  # skip anything implausibly large/binary

    def _read_verify_files(self) -> dict[str, str]:
        bounty_verify = (Path(self.cfg.task_dir) / "bounties"
                         / f"bounty_{self.cfg.bounty_number}" / "verify_files")
        for vdir in (bounty_verify, self.tmp_dir / "verify_files"):
            if not vdir.is_dir():
                continue
            out: dict[str, str] = {}
            for f in sorted(vdir.iterdir()):
                if not f.is_file():
                    continue
                try:
                    if f.stat().st_size > self._VERIFY_MAX_BYTES:
                        continue
                    out[f.name] = f.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue  # skip binaries / unreadable companions
            if out:
                logger.info(f"[zero] surfaced verify scripts to engagement: {sorted(out)}")
                return out
        logger.info("[zero] no verify_files found to surface (none staged for this bounty)")
        return {}

    def _stage_exploit(self, result) -> bool:
        """Copy zero's exploit.sh into BB's exploit_files/, or synthesize one from a
        confirmed finding as a fallback. Returns True if a script was staged."""
        self.exploit_files_dir.mkdir(parents=True, exist_ok=True)
        dest = self.exploit_files_dir / "exploit.sh"

        if result.has_exploit:
            shutil.copy(result.exploit_script, dest)
        elif result.found:
            script = synthesize_exploit_from_finding(result.best_finding())
            if not script:
                return False
            dest.write_text(script, encoding="utf-8")
        else:
            return False

        dest.chmod(dest.stat().st_mode | 0o755)
        return True

    def to_dict(self) -> dict:
        return {"agent_id": self.agent_id, "system": self.cfg.system,
                "bounty_number": self.cfg.bounty_number, "task_type": self.cfg.task_type}
