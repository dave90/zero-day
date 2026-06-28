"""`zero_detect_workflow` — DetectWorkflow that drives zero as the executor.

Identical to BountyBench's `DetectWorkflow` except it registers `ZeroDetectPhase`
(executor = zero) instead of `DetectPhase`. `_create_phases` is reimplemented (rather
than calling super) because the stock method hard-codes the phase class; the phase_kwargs
block is kept in lockstep with `workflows/detect_workflow.py`.

Runs inside a BB checkout only.
"""
from __future__ import annotations

from prompts.prompts import STOP_TOKEN
from resources.utils import read_writeup
from workflows.detect_workflow import DetectWorkflow

from .zero_phases import ZeroDetectPhase


class ZeroDetectWorkflow(DetectWorkflow):
    """DetectWorkflow whose executor is zero. Run BB with `--use_mock_model`."""

    def _create_phases(self):
        resolved_host = self.repo_metadata["target_host"]
        phase_kwargs = {
            "use_mock_model": self.params.get("use_mock_model"),
            "model": self.params.get("model"),
            "helm": self.params.get("use_helm"),
            "vulnerability_type": self.params.get("vulnerability_type"),
            "task_dir": self.task_dir,
            "bounty_number": self.bounty_number,
            "task_description": read_writeup(self.task_dir, self.bounty_number),
            "resolved_host": resolved_host,
            "stop_token": STOP_TOKEN,
            "target_host": self.repo_metadata["target_host"],
            "initial_prompt": self.initial_prompt,
            "info": self.repo_metadata.get("info", "")
            + "\n"
            + self.bounty_metadata.get("info", ""),
            "max_input_tokens": self.params.get("max_input_tokens"),
            "max_output_tokens": self.params.get("max_output_tokens"),
            "interactive": self.interactive,
            "max_iterations": self.params.get("phase_iterations"),
            "submit": not self.params.get("disable_submit", False),
        }
        if self.params.get("use_cwe", False):
            phase_kwargs["cwe"] = self.bounty_metadata["CWE"]

        detect_phase = ZeroDetectPhase(workflow=self, **phase_kwargs)
        self._register_root_phase(detect_phase)
