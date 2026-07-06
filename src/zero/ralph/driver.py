"""RalphDriver: the outer loop that runs zero in a Ralph-style iteration.

Three phases (see plan.md):
  1. Analysis & PRD — the `analyzer` recons + drafts a PRD, refined in a bounded
     discussion with the `analyzer_validator`; optional operator checkpoint.
  2. Iteration loop — each pass seeds a fresh-session orchestrator with the PRD +
     progress journal ("work the top-priority incomplete task"), runs ONE bounded
     orchestrated pass, records progress, has the validator judge task completion,
     then snapshots + resets `.pentest/` (findings carry forward). Stops at N
     iterations or when all tasks are complete.
  3. Aggregate — writes a cumulative report from the carried-forward findings.

The driver owns NO new agent machinery: it composes the Orchestrator's existing
primitives (`_drive_agent_to_final`, `_run_orchestrated_pass`, `findings_store`,
`_emit`). Mode a/b/c are untouched.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from zero.agents.analyzer import build_analyzer_loop
from zero.agents.analyzer_validator import build_analyzer_validator_loop
from zero.findings.report import write_report
from zero.llm.llm_trace import reset_llm_loggers
from zero.ralph import state
from zero.ralph.prd import (
    PRD,
    load_prd,
    parse_critique,
    parse_prd,
    parse_task_verdicts,
    save_prd,
)
from zero.ralph.progress import append_iteration, read_progress
from zero.ui.events import AgentTextDelta, AgentTextEnd, RalphIteration, SessionSummary

if TYPE_CHECKING:
    from zero.orchestrator.orchestrator import Orchestrator


class RalphDriver:
    def __init__(self, orch: "Orchestrator", project_ctx: str | None) -> None:
        self.orch = orch
        self.cfg = orch.cfg
        self.cwd = orch.cfg.cwd
        self.project_ctx = project_ctx
        # The bounded orchestrated pass reads project context off the orchestrator.
        orch._project_ctx = project_ctx

    # ----------------------------------------------------------------- entry

    async def run(self) -> None:
        state.ensure_dirs(self.cwd)
        goal = self.cfg.goal or ""

        # Phase 1 — load the PRD. On --ralph-resume, reuse the prior `.ralph/prd.json`
        # (keeping each task's completed/pending state) and skip the analyzer
        # discussion + approval; otherwise plan it fresh.
        prd = await self._load_or_build_prd(goal)
        if prd is None or not prd.tasks:
            return

        # ------------------------------------------------------------- loop
        # Resume continues iteration numbering from the last snapshot so backups
        # don't collide and the journal reads continuously; a fresh run starts at 1.
        start = (state.last_iteration_index(self.cwd) + 1) if self.cfg.ralph_resume else 1
        total = start - 1 + self.cfg.ralph_iterations
        last_iter = start - 1
        for offset in range(self.cfg.ralph_iterations):
            i = start + offset
            prd = load_prd(state.prd_path(self.cwd)) or prd
            incomplete = prd.incomplete()
            if not incomplete:
                await self._banner(
                    "complete", last_iter, "All PRD tasks complete — stopping early.", total=total
                )
                break
            last_iter = i
            top = incomplete[0]
            await self._banner(
                "iteration", i,
                f"Working highest-priority task: {top.id} — {top.title}",
                total=total,
            )

            progress_text = read_progress(state.progress_path(self.cwd))
            summary = await self.orch._run_orchestrated_pass(
                seed_messages=[self._iteration_seed(i, prd, progress_text)],
                max_turns=self.cfg.max_turns,
                session_label=f"orchestrator_iter_{i}",
            )

            confirmed = self.orch.findings_store.all_records("confirmed")
            confirmed_titles = [
                f"{r.finding.title} ({r.finding.endpoint})" for r in confirmed
            ]

            # The top task was the one actually worked this pass — count the pass.
            top.passes += 1

            verdicts = await self._judge_completion(i, prd, progress_text, summary)
            notes: list[str] = []
            for v in verdicts:
                t = prd.by_id(v.task_id)
                if t is None:
                    continue
                t.note = v.reason
                if v.completed:
                    t.status = "completed"
                notes.append(
                    f"{t.id} [{'completed' if v.completed else 'pending'}]: {v.reason}"
                )
            save_prd(state.prd_path(self.cwd), prd)
            append_iteration(
                state.progress_path(self.cwd),
                iteration=i,
                summary=summary,
                confirmed_titles=confirmed_titles,
                task_notes=notes,
            )

            # Snapshot + reset .pentest/ for the next pass (findings carry forward).
            dest = state.backup_and_reset(self.cwd, i)
            # The reset recreated .pentest/llm/ empty; drop the cached per-actor trace
            # loggers so the next iteration opens fresh handlers against it (and stale
            # fds to the snapshotted-away logs don't accumulate).
            reset_llm_loggers()
            self.orch.audit.write(
                "ralph_iteration_done", iteration=i,
                confirmed=len(confirmed_titles), backup=str(dest),
                completed=[v.task_id for v in verdicts if v.completed],
            )

        await self._final_report(last_iter)

    # --------------------------------------------------------- phase 1: PRD

    async def _load_or_build_prd(self, goal: str) -> PRD | None:
        """Resume from `.ralph/prd.json` (if --ralph-resume and one exists), else run
        the analyzer↔validator discussion + optional checkpoint to build a fresh PRD.
        Returns None when the run should abort (no tasks / operator rejected)."""
        if self.cfg.ralph_resume:
            prd = load_prd(state.prd_path(self.cwd))
            if prd is not None and prd.tasks:
                done = sum(1 for t in prd.tasks if t.status == "completed")
                await self._banner(
                    "analysis", 0,
                    f"Resuming prior Ralph run from .ralph/ — {done}/{len(prd.tasks)} task(s)"
                    " already complete; continuing the backlog.\n"
                    + "\n".join(
                        f"  [{t.priority}] {t.id} {t.title} — {t.status}" for t in prd.tasks
                    ),
                )
                return prd
            await self._banner(
                "analysis", 0,
                "--ralph-resume set but no usable .ralph/prd.json was found — planning fresh.",
            )

        await self._banner("analysis", 0, "Building the PRD from the goal + target recon.")
        prd = await self._build_prd(goal)
        save_prd(state.prd_path(self.cwd), prd)
        if not prd.tasks:
            await self._banner(
                "analysis", 0,
                "Could not derive any tasks for this goal — stopping. Refine the goal /"
                " pentest.md and retry.",
            )
            return None
        await self._banner(
            "analysis", 0,
            f"PRD ready: {len(prd.tasks)} task(s).\n"
            + "\n".join(f"  [{t.priority}] {t.id} {t.title}" for t in prd.incomplete()),
        )
        if self.cfg.ralph_approve_prd:
            if not await self._checkpoint(prd):
                await self._banner("analysis", 0, "PRD rejected by operator — aborting Ralph run.")
                return None
            prd = load_prd(state.prd_path(self.cwd)) or prd
        return prd

    async def _build_prd(self, goal: str) -> PRD:
        """Analyzer drafts a PRD; analyzer_validator reviews; iterate up to
        ralph_discussion_rounds until approved (else proceed with the best PRD)."""
        orch = self.orch
        recon = orch._recon_catalog()
        analyzer = build_analyzer_loop(
            orch.backend, recon, project_context=self.project_ctx,
            max_turns=self.cfg.analyzer_max_turns, session=orch._make_session("analyzer"),
        )
        analyzer.add_user_message(self._analyzer_brief(goal))
        prd_text = await orch._drive_agent_to_final(
            analyzer, agent_label="analyzer", session_id="analyzer"
        )
        prd = parse_prd(prd_text, goal=goal)

        validator = build_analyzer_validator_loop(
            orch.backend, recon, project_context=self.project_ctx,
            max_turns=self.cfg.analyzer_validator_max_turns,
            session=orch._make_session("analyzer_validator"),
        )
        for rnd in range(1, self.cfg.ralph_discussion_rounds + 1):
            self._reset_loop(validator)
            validator.add_user_message(self._critique_brief(goal, prd))
            crit_text = await orch._drive_agent_to_final(
                validator, agent_label="analyzer_validator", session_id="analyzer_validator"
            )
            crit = parse_critique(crit_text)
            await self._banner(
                "discussion", 0,
                f"Review round {rnd}: {'APPROVED' if crit.approved else 'changes requested'}."
                + (f"\n  {crit.feedback.strip()[:300]}" if crit.feedback.strip() else ""),
            )
            if crit.approved and prd.tasks:
                break
            if rnd == self.cfg.ralph_discussion_rounds:
                await self._banner(
                    "discussion", 0,
                    "Discussion did not converge — proceeding with the latest PRD.",
                )
                break
            # Revise: feed the critique back to the analyzer and re-draft.
            self._reset_loop(analyzer)
            analyzer.add_user_message(self._revise_brief(crit))
            revised_text = await orch._drive_agent_to_final(
                analyzer, agent_label="analyzer", session_id="analyzer"
            )
            revised = parse_prd(revised_text, goal=goal)
            if revised.tasks:
                prd = revised
        return prd

    async def _checkpoint(self, prd: PRD) -> bool:
        """Block for operator approval of the PRD via the chat input. 'go' starts the
        loop, 'abort' cancels, anything else is treated as revision feedback (one
        more analyzer pass, then re-prompt)."""
        orch = self.orch
        while True:
            await self._emit_text(
                "\nPRD checkpoint — reply 'go' to start the loop, 'abort' to cancel,"
                " or type feedback to revise the PRD.\n"
            )
            msg = (await orch.user_queue.get()).strip()
            low = msg.lower()
            if low in ("go", "start", "approve", "yes", "y", "ok"):
                return True
            if low in ("abort", "stop", "no", "n", "quit", "exit"):
                return False
            # Treat as revision feedback.
            recon = orch._recon_catalog()
            analyzer = build_analyzer_loop(
                orch.backend, recon, project_context=self.project_ctx,
                max_turns=self.cfg.analyzer_max_turns,
                session=orch._make_session("analyzer"),
            )
            analyzer.add_user_message(self._revise_brief_from_text(prd, msg))
            revised_text = await orch._drive_agent_to_final(
                analyzer, agent_label="analyzer", session_id="analyzer"
            )
            revised = parse_prd(revised_text, goal=self.cfg.goal or "")
            if revised.tasks:
                prd = revised
                save_prd(state.prd_path(self.cwd), prd)
            await self._banner(
                "analysis", 0,
                "Revised PRD:\n"
                + "\n".join(f"  [{t.priority}] {t.id} {t.title}" for t in prd.incomplete()),
            )

    # ----------------------------------------------- phase 2: completion check

    async def _judge_completion(self, iteration, prd, progress_text, summary):
        orch = self.orch
        recon = orch._recon_catalog()
        judge = build_analyzer_validator_loop(
            orch.backend, recon, project_context=self.project_ctx,
            max_turns=self.cfg.analyzer_validator_max_turns,
            session=orch._make_session(f"analyzer_validator_iter_{iteration}"),
        )
        incomplete_json = json.dumps(
            [t.model_dump() for t in prd.incomplete()], indent=2
        )
        judge.add_user_message(
            f"JOB: judge task completion (iteration {iteration}).\n\n"
            "Decide, for EACH task below, whether it is adequately completed given the"
            " orchestrator's summary, the progress journal, and the findings so far."
            " Completion = acceptance criteria met OR the task was thoroughly attempted"
            " and exhausted (rejected findings count as coverage). If not complete, say"
            " briefly why / what remains.\n\n"
            "## Incomplete tasks\n" + incomplete_json + "\n\n"
            "## This iteration's orchestrator summary\n" + (summary.strip() or "(none)") + "\n\n"
            "## progress.txt (prior iterations)\n" + (progress_text.strip() or "(empty)") + "\n\n"
            + self._findings_context() + "\n\n"
            'Emit a final whose answer is ONLY JSON:'
            ' {"verdicts":[{"task_id":"...","completed":true|false,"reason":"..."}]}'
        )
        text = await orch._drive_agent_to_final(
            judge, agent_label="analyzer_validator", session_id=f"analyzer_validator_iter_{iteration}"
        )
        return parse_task_verdicts(text)

    # ------------------------------------------------------- phase 3: report

    async def _final_report(self, iterations_run: int) -> None:
        store = self.orch.findings_store
        report_path = ""
        try:
            p = write_report(store, state.final_report_path(self.cwd))
            report_path = str(p)
        except Exception as e:  # noqa: BLE001
            self.orch.audit.write("ralph_report_error", error=str(e))

        confirmed = store.all_records("confirmed")
        rejected = store.all_records("rejected")
        pending = store.all_records("pending")
        await self._banner(
            "complete", iterations_run,
            f"Ralph finished after {iterations_run} iteration(s): "
            f"{len(confirmed)} confirmed · {len(rejected)} rejected · {len(pending)} pending.",
            total=iterations_run,
        )
        self.orch.audit.write(
            "ralph_done", iterations=iterations_run, confirmed=len(confirmed),
            report_path=report_path,
        )
        await self.orch._emit(
            SessionSummary(
                confirmed=[self._fmt_rec(r) for r in confirmed],
                rejected=[self._fmt_rec(r) for r in rejected],
                pending=[self._fmt_rec(r) for r in pending],
                findings_dir=str(store.root),
                audit_path=str(self.orch.audit.path),
                turns=0,
                report_path=report_path,
            )
        )

    @staticmethod
    def _fmt_rec(rec) -> dict:
        return {
            "id": rec.id,
            "title": rec.finding.title,
            "endpoint": rec.finding.endpoint,
            "cvss_score": rec.validation.cvss40_score if rec.validation else 0.0,
        }

    # ----------------------------------------------------------- prompt briefs

    def _analyzer_brief(self, goal: str) -> str:
        return (
            "Plan this engagement. Recon the target, then emit the PRD as your final"
            " (JSON only).\n\n"
            f"ENGAGEMENT GOAL: {goal or '(no explicit goal — infer from pentest.md)'}"
        )

    def _critique_brief(self, goal: str, prd: PRD) -> str:
        return (
            "JOB: review this PRD. Does it cover ALL plausible attacks needed to"
            " satisfy the goal? Emit a final whose answer is ONLY JSON"
            ' {"approved":bool,"feedback":"...","missing":[...]}.\n\n'
            f"ENGAGEMENT GOAL: {goal}\n\n## PRD under review\n"
            + json.dumps(prd.model_dump(), indent=2)
        )

    def _revise_brief(self, crit) -> str:
        missing = "\n".join(f"- {m}" for m in crit.missing) or "(none listed)"
        return (
            "The reviewer did NOT approve the PRD. Revise it to address this feedback,"
            " then emit the FULL updated PRD JSON again (same schema).\n\n"
            f"FEEDBACK: {crit.feedback}\n\nMISSING:\n{missing}"
        )

    def _revise_brief_from_text(self, prd: PRD, feedback: str) -> str:
        return (
            "The operator gave feedback on the PRD. Revise it accordingly and emit the"
            " FULL updated PRD JSON again (same schema).\n\n"
            f"OPERATOR FEEDBACK: {feedback}\n\n## Current PRD\n"
            + json.dumps(prd.model_dump(), indent=2)
        )

    @staticmethod
    def _trunc(text: str, n: int) -> str:
        text = (text or "").strip()
        return text if len(text) <= n else text[:n].rstrip() + " …"

    def _findings_context(self) -> str:
        """All findings carried across iterations (confirmed / rejected / pending),
        formatted so the next pass can REUSE confirmed work (reproducers/payloads),
        skip REJECTED dead-ends, and continue PENDING leads instead of redoing them.
        Read straight from the on-disk store (which the reset preserves)."""
        store = self.orch.findings_store
        confirmed = store.all_records("confirmed")
        rejected = store.all_records("rejected")
        pending = store.all_records("pending")
        if not (confirmed or rejected or pending):
            return "## Findings so far\n(none recorded yet — this is the first pass.)"
        lines = [
            "## Findings so far (carried across iterations — do NOT re-report or re-test these)",
            f"{len(confirmed)} confirmed · {len(rejected)} rejected · {len(pending)} pending",
        ]
        if confirmed:
            lines.append("\n### Confirmed (PROVEN — reuse / chain these; never re-validate)")
            for r in confirmed:
                f = r.finding
                lines.append(f"- [{f.cwe}] {f.title} @ {f.method} {f.endpoint}")
                repro = r.validation.reproducer if r.validation else ""
                if repro.strip():
                    lines.append(f"    reproducer: {self._trunc(repro, 600)}")
        if rejected:
            lines.append("\n### Rejected (TESTED and refuted — do NOT retry the same way)")
            for r in rejected:
                why = (r.validation.rationale if r.validation else "") or r.finding.notes
                lines.append(
                    f"- {r.finding.title} @ {r.finding.endpoint} — why not: {self._trunc(why, 300)}"
                )
        if pending:
            lines.append("\n### Pending (attempted, NOT yet confirmed — continue if relevant)")
            for r in pending:
                why = (r.validation.rationale if r.validation else "") or r.finding.notes
                lines.append(
                    f"- {r.finding.title} @ {r.finding.endpoint} — gap: {self._trunc(why, 300)}"
                )
        return "\n".join(lines)

    def _iteration_seed(self, iteration: int, prd: PRD, progress_text: str) -> str:
        incomplete_json = json.dumps([t.model_dump() for t in prd.incomplete()], indent=2)
        return (
            f"You are running in RALPH MODE — iteration {iteration} of"
            f" {self.cfg.ralph_iterations}. A PRD backlog of pentest tasks drives this"
            " engagement.\n\n"
            "Pick the SINGLE highest-priority task whose status is not 'completed' and"
            " work ONLY on it this iteration — go deep. Recon, dispatch pentesters, chain"
            " results, and confirm any finding with a Validator (your own checks do not"
            " count). When the task is adequately covered (confirmed OR thoroughly"
            " exhausted), call `finalize` with a summary that NAMES the task id you worked"
            " and states what you established and what (if anything) remains.\n\n"
            "Do NOT re-investigate completed tasks. Reuse the confirmed findings below"
            " (their reproducers/creds) to chain further, skip the rejected dead-ends,"
            " and pick up pending leads — and use the progress journal too.\n\n"
            "## PRD — incomplete tasks (highest priority first)\n" + incomplete_json + "\n\n"
            + self._findings_context() + "\n\n"
            "## progress.txt (what earlier iterations did)\n"
            + (progress_text.strip() or "(empty — this is the first iteration)")
        )

    # ----------------------------------------------------------------- ui

    def _reset_loop(self, loop) -> None:
        """Let a reused planning loop take a fresh batch of turns for the next
        discussion segment (it accumulates `_turn` across drives)."""
        loop._turn = 0
        loop._wrapped_up = False

    async def _banner(self, phase: str, iteration: int, text: str, *, total: int = 0) -> None:
        await self.orch._emit(
            RalphIteration(phase=phase, iteration=iteration, total=total, text=text)
        )

    async def _emit_text(self, text: str) -> None:
        await self.orch._emit(AgentTextDelta(text=text))
        await self.orch._emit(AgentTextEnd())
