"""Phase shims that swap BountyBench's `ExecutorAgent` for `ZeroExecutorAgent`.

We subclass the stock `DetectPhase`/`ExploitPhase` and override only `define_agents()`,
keeping the scorer (`DetectAgent`/`ExploitAgent`) and everything else (resources, the
round-robin iteration loop, success/complete logic) exactly as BountyBench ships it.

This module imports BountyBench modules and only runs inside a BB checkout.
"""
from __future__ import annotations

import json
from typing import Dict, Optional, Tuple, Type

from agents.base_agent import AgentConfig, BaseAgent
from agents.detect_agent.detect_agent import DetectAgent
from agents.exploit_agent.exploit_agent import ExploitAgent
from phases.detect_phase import DetectPhase
from phases.exploit_phase import ExploitPhase
from resources.utils import read_writeup

from .zero_executor_agent import ZeroExecutorAgent, ZeroExecutorAgentConfig


def _compatible_detect_list(meta: dict) -> list[str]:
    """Return the patch list BB Detect can score against.

    Upstream Detect uses `compatible_exploits`, but the current public task metadata
    in this checkout ships the equivalent patch list as `compatible_patches`. For the
    zero adapter, treat that as the scorer's compatible list instead of skipping every
    Detect task.
    """
    raw = meta.get("compatible_exploits") or meta.get("compatible_patches") or []
    return [str(x) for x in raw]


def _ensure_detect_metadata(workflow) -> None:
    bounty_meta = getattr(workflow, "bounty_metadata", {}) or {}
    if not bounty_meta.get("compatible_exploits"):
        fallback = _compatible_detect_list(bounty_meta)
        if fallback:
            bounty_meta["compatible_exploits"] = fallback


def _zero_config(phase, *, task_type: str) -> ZeroExecutorAgentConfig:
    """Gather the task input zero should receive from BountyBench metadata.

    NOTE: `define_agents()` is invoked from `BasePhase.__init__` *before* `self.params`
    is populated, so we read from the same sources the stock phases use: the workflow's
    repo/bounty metadata, workflow params, and `read_writeup` where the stock prompt does.
    """
    wf = phase.workflow
    repo_meta = getattr(wf, "repo_metadata", {}) or {}
    bounty_meta = getattr(wf, "bounty_metadata", {}) or {}
    workflow_params = getattr(wf, "params", {}) or {}

    if task_type == "exploit":
        writeup = read_writeup(wf.task_dir, phase.bounty_number) or ""
        cwe = None
        info = bounty_meta.get("exploit_info", "") or repo_meta.get("info", "")
        target_host = (
            repo_meta.get("target_host", "")
            or next(iter(bounty_meta.get("bounty_setup_hosts", []) or []), "")
        )
    else:
        # Stock Detect is blind: no writeup and no verifier files. CWE is only passed
        # for the explicit higher-information `--use_cwe` variant.
        writeup = ""
        cwe = bounty_meta.get("CWE") if workflow_params.get("use_cwe", False) else None
        info = "\n".join(
            p for p in [repo_meta.get("info", ""), bounty_meta.get("info", "")]
            if p.strip()
        )
        target_host = repo_meta.get("target_host", "") or ""

    return ZeroExecutorAgentConfig(
        target_host=target_host,
        tmp_dir=phase.tmp_dir,
        task_dir=wf.task_dir,
        task_type=task_type,
        system=wf.task_dir.name,
        bounty_number=phase.bounty_number,
        writeup=writeup,
        cwe=cwe,
        info=info.strip(),
    )


class ZeroDetectAgent(DetectAgent):
    """Detect scorer with a compatibility fallback for public BB metadata."""

    def get_compatible_exploits(self):
        bounty_metadata_path = self.bounty_dir / "bounty_metadata.json"
        if bounty_metadata_path.exists():
            meta = json.loads(bounty_metadata_path.read_text(encoding="utf-8"))
            compatible = _compatible_detect_list(meta)
            if compatible:
                return compatible
        return super().get_compatible_exploits()


class ZeroDetectPhase(DetectPhase):
    """DetectPhase with the executor replaced by zero (scorer = DetectAgent)."""

    AGENT_CLASSES = [ZeroExecutorAgent, ZeroDetectAgent]

    def __init__(self, *args, **kwargs):
        workflow = kwargs.get("workflow") or (args[0] if args else None)
        if workflow is not None:
            _ensure_detect_metadata(workflow)
        super().__init__(*args, **kwargs)

    def define_agents(self) -> Dict[str, Tuple[Type[BaseAgent], Optional[AgentConfig]]]:
        agents = super().define_agents()  # ordered: executor_agent, detect_agent
        agents["executor_agent"] = (ZeroExecutorAgent, _zero_config(self, task_type="detect"))
        agents["detect_agent"] = (ZeroDetectAgent, agents["detect_agent"][1])
        return agents


class ZeroExploitPhase(ExploitPhase):
    """ExploitPhase with the executor replaced by zero (scorer = ExploitAgent)."""

    AGENT_CLASSES = [ZeroExecutorAgent, ExploitAgent]

    def define_agents(self) -> Dict[str, Tuple[Type[BaseAgent], Optional[AgentConfig]]]:
        agents = super().define_agents()  # ordered: executor_agent, exploit_agent
        agents["executor_agent"] = (ZeroExecutorAgent, _zero_config(self, task_type="exploit"))
        return agents
