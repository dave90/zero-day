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
from zero.ralph.dossier import append_evidence, read_dossier
from zero.ralph.prd import (
    PRD,
    Epic,
    load_prd,
    parse_critique,
    parse_epics,
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

            # Persist this pass's evidence into the task's durable dossier BEFORE the
            # judge runs and BEFORE the reset wipes .pentest/memory/. This is the whole
            # fix: the orchestrator's actual work (finalize summary + the notes it wrote
            # to its memory scratchpad) now survives to the validator and to the next
            # iteration, instead of only a bare claim in progress.txt.
            append_evidence(
                state.dossier_path(self.cwd, top.id),
                iteration=i,
                summary=summary,
                memory_notes=self._orchestrator_memory_notes(),
            )

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
        # Phase 1a: agree a set of epics that partition the attack surface (skipped
        # when --no-ralph-epics). The task phase then decomposes each into atomic
        # tasks, so no surface area silently drops out of the backlog.
        epics = await self._build_epics(goal)
        if self.cfg.ralph_epics and not epics:
            await self._banner(
                "analysis", 0,
                "Epic phase produced no epics — proceeding without epic structure"
                " (task coverage will not be epic-checked this run).",
            )
        # Phase 1b: decompose the epics into atomic tasks.
        prd = await self._build_prd(goal, epics)
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
            f"PRD ready: {len(prd.tasks)} task(s)"
            + (f" across {len(prd.epics)} epic(s)" if prd.epics else "") + ".\n"
            + self._prd_outline(prd),
        )
        if self.cfg.ralph_approve_prd:
            if not await self._checkpoint(prd):
                await self._banner("analysis", 0, "PRD rejected by operator — aborting Ralph run.")
                return None
            prd = load_prd(state.prd_path(self.cwd)) or prd
        return prd

    async def _build_epics(self, goal: str) -> list[Epic]:
        """Phase 1a: the analyzer proposes epics that partition the attack surface;
        the analyzer_validator re-enumerates the surface and rejects any set that
        leaves a component unclaimed. Iterate up to ralph_discussion_rounds. Returns
        [] when disabled or nothing parseable was produced (caller falls back to
        epic-free task planning). Uses its OWN sessions so the epic-phase turn budget
        and wrap-up state never bleed into the task phase."""
        if not self.cfg.ralph_epics:
            return []
        orch = self.orch
        recon = orch._recon_catalog()
        analyzer = build_analyzer_loop(
            orch.backend, recon, project_context=self.project_ctx,
            max_turns=self.cfg.max_turns, session=orch._make_session("analyzer_epics"),
        )
        analyzer.add_user_message(self._epic_brief(goal))
        text = await orch._drive_agent_to_final(
            analyzer, agent_label="analyzer", session_id="analyzer_epics"
        )
        epics = parse_epics(text, goal=goal)

        validator = build_analyzer_validator_loop(
            orch.backend, recon, project_context=self.project_ctx,
            max_turns=self.cfg.analyzer_validator_max_turns,
            session=orch._make_session("analyzer_validator_epics"),
        )
        for rnd in range(1, self.cfg.ralph_discussion_rounds + 1):
            self._reset_loop(validator)
            validator.add_user_message(self._epic_critique_brief(goal, epics))
            crit = parse_critique(await orch._drive_agent_to_final(
                validator, agent_label="analyzer_validator",
                session_id="analyzer_validator_epics",
            ))
            await self._banner(
                "discussion", 0,
                f"Epic review round {rnd}: "
                f"{'APPROVED' if crit.approved else 'changes requested'}"
                f" ({len(epics)} epic(s))."
                + (f"\n  {crit.feedback.strip()[:300]}" if crit.feedback.strip() else ""),
            )
            if crit.approved and epics:
                break
            if rnd == self.cfg.ralph_discussion_rounds:
                await self._banner(
                    "discussion", 0,
                    "Epic discussion did not converge — proceeding with the latest epics.",
                )
                break
            self._reset_loop(analyzer)
            analyzer.add_user_message(self._epic_revise_brief(crit))
            revised = parse_epics(await orch._drive_agent_to_final(
                analyzer, agent_label="analyzer", session_id="analyzer_epics",
            ), goal=goal)
            if revised:
                epics = revised
        return epics

    async def _build_prd(self, goal: str, epics: list[Epic]) -> PRD:
        """Phase 1b: analyzer decomposes the approved epics into atomic tasks (each
        tagged with its epic_id); analyzer_validator reviews; iterate up to
        ralph_discussion_rounds until approved (else proceed with the best PRD). When
        `epics` is empty (epic phase off/failed) this is the legacy single-phase build."""
        orch = self.orch
        recon = orch._recon_catalog()
        analyzer = build_analyzer_loop(
            orch.backend, recon, project_context=self.project_ctx,
            max_turns=self.cfg.max_turns, session=orch._make_session("analyzer"),
        )
        analyzer.add_user_message(self._analyzer_brief(goal, epics))
        prd_text = await orch._drive_agent_to_final(
            analyzer, agent_label="analyzer", session_id="analyzer"
        )
        prd = parse_prd(prd_text, goal=goal)
        prd.epics = epics

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
                revised.epics = epics
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
                max_turns=self.cfg.max_turns,
                session=orch._make_session("analyzer"),
            )
            analyzer.add_user_message(self._revise_brief_from_text(prd, msg))
            revised_text = await orch._drive_agent_to_final(
                analyzer, agent_label="analyzer", session_id="analyzer"
            )
            revised = parse_prd(revised_text, goal=self.cfg.goal or "")
            if revised.tasks:
                revised.epics = prd.epics  # operator revises tasks; keep approved epics
                prd = revised
                save_prd(state.prd_path(self.cwd), prd)
            await self._banner(
                "analysis", 0,
                "Revised PRD:\n" + self._prd_outline(prd),
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
            "Decide, for EACH task below, whether it is adequately completed. Each task"
            " has an EVIDENCE DOSSIER — the accumulated testing the orchestrator recorded"
            " across all iterations that worked it. Judge from the dossier, NOT from the"
            " one-line summary: does the recorded evidence actually satisfy the task's"
            " acceptance criteria (or show it was thoroughly attempted and exhausted —"
            " rejected findings count as coverage)?\n\n"
            "Do NOT trust the dossier's conclusions — VERIFY them. Use your own tools"
            " (`read_file`/`grep`/`web_fetch`) to spot-check the concrete claims (that the"
            " cited code really behaves as stated, that the tests described were possible)"
            " before you sign off. A confident-sounding but unverifiable dossier is a"
            " REJECT.\n\n"
            "When a task is NOT complete, make `reason` a precise, actionable statement of"
            " the SPECIFIC evidence still missing (what to test or read next) — it is fed"
            " back verbatim to the next iteration as its directive. Do not ask it to"
            " 'reproduce a table'; ask for the missing test/analysis.\n\n"
            "## Incomplete tasks\n" + incomplete_json + "\n\n"
            "## Evidence dossiers (per task)\n" + self._dossiers_context(prd) + "\n\n"
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

    def _epic_brief(self, goal: str) -> str:
        return (
            "JOB: produce the EPICS. Recon the target to build an EXHAUSTIVE surface"
            " inventory — every endpoint/controller/route, every component and trust"
            " boundary, every ingest/upload and admin/config path — then group that"
            " whole inventory into a small set of EPICS (attack-surface areas). The"
            " epics must PARTITION the surface: every enumerated item belongs to exactly"
            " one epic, and nothing is dropped. Each epic's acceptanceCriteria is a prose"
            " statement of what thorough coverage of that area means. Do NOT write tasks"
            " yet. Emit a final whose answer is ONLY JSON"
            ' {"goal":"...","epics":[{"id":"e1","priority":1,"title":"...",'
            '"description":"...","acceptanceCriteria":"..."}]}.\n\n'
            f"ENGAGEMENT GOAL: {goal or '(no explicit goal — infer from pentest.md)'}"
        )

    def _epic_critique_brief(self, goal: str, epics: list[Epic]) -> str:
        return (
            "JOB: review this EPIC list. Independently re-enumerate the attack surface"
            " (endpoints/controllers/components/trust boundaries) and verify the epics"
            " form a COMPLETE PARTITION of it: reject if ANY enumerated endpoint or"
            " component is not claimed by some epic, or if epics overlap/duplicate. A"
            " plan that merely 'sounds comprehensive' is NOT enough — check the actual"
            " inventory against the epics and name every uncovered area in `missing`."
            " Also reject epics that are too coarse to be a distinct area or too narrow"
            " to be more than a single task. Emit a final whose answer is ONLY JSON"
            ' {"approved":bool,"feedback":"...","missing":[...]}.\n\n'
            f"ENGAGEMENT GOAL: {goal}\n\n## Epics under review\n"
            + json.dumps([e.model_dump() for e in epics], indent=2)
        )

    def _epic_revise_brief(self, crit) -> str:
        missing = "\n".join(f"- {m}" for m in crit.missing) or "(none listed)"
        return (
            "The reviewer did NOT approve the epics. Revise them to address this"
            " feedback (add epics for the uncovered areas; split/merge as directed),"
            " then emit the FULL updated epic JSON again (same schema).\n\n"
            f"FEEDBACK: {crit.feedback}\n\nUNCOVERED / MISSING:\n{missing}"
        )

    def _analyzer_brief(self, goal: str, epics: list[Epic]) -> str:
        if not epics:
            return (
                "Plan this engagement. Recon the target to build a concrete surface"
                " inventory, then emit the PRD as your final (JSON only). Decompose the"
                " surface into ATOMIC tasks — one concrete testable hypothesis per single"
                " endpoint/parameter/component, NOT broad buckets — because the loop works"
                " one task per iteration and skips the hard part of a coarse task. Produce"
                " as many small tasks as the surface warrants.\n\n"
                f"ENGAGEMENT GOAL: {goal or '(no explicit goal — infer from pentest.md)'}"
            )
        return (
            "JOB: produce the TASKS. You are given the APPROVED epics (attack-surface"
            " areas). Decompose EACH epic into ATOMIC tasks — one concrete testable"
            " hypothesis per single endpoint/parameter/component, NOT broad buckets"
            " (the loop works one task per iteration and skips the hard part of a coarse"
            " task). Rules: every task MUST set `epic_id` to one of the epic ids below;"
            " every epic MUST receive at least one task; do NOT invent new epics or leave"
            " any epic empty. Produce as many small tasks per epic as its surface"
            " warrants. Emit the PRD as your final (JSON only):"
            ' {"goal":"...","tasks":[{"id":"t1","priority":1,"epic_id":"e1","title":"...",'
            '"description":"...","acceptanceCriteria":"...","status":"pending"}]}.\n\n'
            f"ENGAGEMENT GOAL: {goal or '(no explicit goal — infer from pentest.md)'}\n\n"
            "## Approved epics\n" + json.dumps([e.model_dump() for e in epics], indent=2)
        )

    def _critique_brief(self, goal: str, prd: PRD) -> str:
        epic_check = ""
        if prd.epics:
            epic_check = (
                " (3) every epic in the PRD has AT LEAST ONE task whose `epic_id` matches"
                " it, and the tasks under each epic adequately cover that epic's"
                " acceptanceCriteria — REJECT if any epic is unserved or thinly covered."
            )
        return (
            "JOB: review this PRD. Check: (1) does it cover ALL plausible attacks"
            " needed to satisfy the goal AND the actual attack surface; (2) is every"
            " task ATOMIC — one concrete surface/hypothesis each (a specific"
            " endpoint+method, parameter, or component), not a broad bucket like 'test"
            " the web app' or 'review the mixnet' — REJECT coarse buckets and require"
            " them split into per-surface tasks (the loop works one task per iteration,"
            " so a coarse task gets only its easy part done)." + epic_check + " Emit a"
            ' final whose answer is ONLY JSON {"approved":bool,"feedback":"...","missing":[...]}.\n\n'
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

    def _prd_outline(self, prd: PRD) -> str:
        """Human banner: tasks grouped under their epic (with completed/total), or a
        flat priority-ordered list when the PRD has no epics."""
        if not prd.epics:
            return "\n".join(f"  [{t.priority}] {t.id} {t.title}" for t in prd.incomplete())
        lines: list[str] = []
        for e in sorted(prd.epics, key=lambda e: (e.priority, e.id)):
            done, total = prd.epic_progress(e.id)
            lines.append(f"  [{e.priority}] {e.id} {e.title} — {done}/{total} task(s)")
            for t in sorted(prd.tasks_for_epic(e.id), key=lambda t: (t.priority, t.id)):
                lines.append(f"      [{t.priority}] {t.id} {t.title} — {t.status}")
        orphans = [t for t in prd.tasks if t.epic_id not in {e.id for e in prd.epics}]
        for t in sorted(orphans, key=lambda t: (t.priority, t.id)):
            lines.append(f"  (no epic) [{t.priority}] {t.id} {t.title} — {t.status}")
        return "\n".join(lines)

    @staticmethod
    def _trunc(text: str, n: int) -> str:
        text = (text or "").strip()
        return text if len(text) <= n else text[:n].rstrip() + " …"

    def _dossiers_context(self, prd: PRD) -> str:
        """The accumulated evidence dossier for each still-incomplete task, so the judge
        evaluates completion against the real work (not the one-line summary). Bounded
        per task by `read_dossier`. Tasks with no dossier yet are listed as untouched."""
        blocks: list[str] = []
        for t in prd.incomplete():
            dossier = read_dossier(state.dossier_path(self.cwd, t.id)).strip()
            if dossier:
                blocks.append(f"### {t.id} — {t.title}\n{dossier}")
            else:
                blocks.append(f"### {t.id} — {t.title}\n(no evidence recorded yet — not started.)")
        return "\n\n".join(blocks) if blocks else "(no incomplete tasks.)"

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

    def _orchestrator_memory_notes(self) -> str:
        """The working notes the orchestrator wrote to its memory scratchpad this pass.
        Keyed by the `session_id` the orchestrator injects for its own tools — always
        the literal "orchestrator" (see `Orchestrator._invoke_tool`) — so the file is
        deterministically `.pentest/memory/orchestrator.md`. Read here, before the reset
        recreates `memory/` empty. Missing/unreadable → empty (best-effort capture must
        never crash the loop)."""
        path = state.pentest_dir(self.cwd) / "memory" / "orchestrator.md"
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

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
            "Your finalize summary, plus the working notes you leave in `memory_write`, are"
            " saved to that task's DURABLE evidence dossier and handed to an independent"
            " validator who will VERIFY your claims against the code with its own tools —"
            " so record concrete, checkable evidence (endpoints, payloads, observed"
            " responses, the guard/behavior you found, subagent verdicts), not bare"
            " conclusions. A claim the validator cannot verify is rejected.\n\n"
            + self._resume_directive(prd)
            + "Do NOT re-investigate completed tasks. Reuse the confirmed findings below"
            " (their reproducers/creds) to chain further, skip the rejected dead-ends,"
            " and pick up pending leads — and use the progress journal too.\n\n"
            "## PRD — incomplete tasks (highest priority first)\n" + incomplete_json + "\n\n"
            + self._findings_context() + "\n\n"
            "## progress.txt (what earlier iterations did)\n"
            + (progress_text.strip() or "(empty — this is the first iteration)")
        )

    def _resume_directive(self, prd: PRD) -> str:
        """If the top task already has accumulated evidence (a prior iteration worked it
        and the validator sent it back), put that dossier + the validator's 'what's
        missing' note front-and-centre as this iteration's directive — so the fresh
        session BUILDS ON real evidence and closes the specific gap, instead of starting
        blank and re-asserting what the journal claims (the old livelock)."""
        incomplete = prd.incomplete()
        if not incomplete:
            return ""
        top = incomplete[0]
        dossier = read_dossier(state.dossier_path(self.cwd, top.id))
        if not dossier.strip():
            return ""
        note = top.note.strip() or "(no specific gap recorded — review the evidence for weaknesses)"
        return (
            f"## RESUME: task {top.id} already has evidence — build on it, do not restate\n"
            f"The validator reviewed prior work on {top.id} and did NOT accept it. Its"
            " reason / what is still missing:\n"
            f"> {note}\n\n"
            "Your job THIS iteration: produce exactly the missing evidence above and add"
            " it to the dossier. Read the accumulated evidence, find the gap, close it"
            " with real testing — do not merely repeat prior conclusions.\n\n"
            f"### Accumulated evidence for {top.id}\n{dossier.strip()}\n\n"
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
