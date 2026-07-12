"""PRD model + tolerant parsers for Ralph mode.

The PRD is the front-loaded spec — a prioritized backlog of pentest tasks the
`analyzer` produces and the `analyzer_validator` signs off on. It is the durable
task DB the loop reads each iteration (status/note updated as tasks complete).
There is NO `branchName` field — we are not operating in git.

All parsers are deliberately tolerant (mirrors `validator.parse_validation` /
`orchestrator_agent.parse_pentester_result`): an agent on a `--resume` session can
drift to prose or a slightly-off JSON shape, and a non-conforming reply must never
crash the loop.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


TaskStatus = Literal["pending", "completed"]


class Epic(BaseModel):
    """One high-level attack-surface area. The planning phase agrees a small set of
    epics that *partition the whole enumerated surface*, then decomposes each into
    atomic tasks (every task carries the `epic_id` of its epic). Fields mirror
    `PRDTask`; an epic has no stored status — its progress is derived from its tasks.
    `priority` 1 = highest."""

    id: str
    priority: int = 3
    title: str = ""
    description: str = ""
    # Prose "definition of done" for the area — what thorough coverage of this
    # surface means. Coverage is the analyzer_validator's judgement, not a checklist.
    acceptanceCriteria: str = ""


class PRDTask(BaseModel):
    """One pentest task / user story. `priority` 1 = highest."""

    id: str
    priority: int = 3
    title: str = ""
    description: str = ""
    # Coverage-based: what counts as the task being adequately tested/exhausted —
    # NOT merely "a bug was found" (a pentest task is open-ended).
    acceptanceCriteria: str = ""
    status: TaskStatus = "pending"
    # Free-form carry-over for the next iteration: why it's not complete yet / where
    # to resume. Written by the analyzer_validator's completion check.
    note: str = ""
    # How many iterations have worked this task (Ralph's "passes" — a stuck task
    # that keeps getting picked is visible here).
    passes: int = 0
    # The epic (attack-surface area) this task decomposes. Empty when the two-phase
    # epic planning is disabled or on a pre-feature PRD (backward-compatible).
    epic_id: str = ""


class PRD(BaseModel):
    goal: str = ""
    # The agreed attack-surface areas. Empty on a pre-feature PRD or when the epic
    # phase is off — every existing path treats an epic-less PRD exactly as before.
    epics: list[Epic] = Field(default_factory=list)
    tasks: list[PRDTask] = Field(default_factory=list)

    def incomplete(self) -> list[PRDTask]:
        """Incomplete tasks, highest priority (lowest number) first, stable by id."""
        return sorted(
            (t for t in self.tasks if t.status != "completed"),
            key=lambda t: (t.priority, t.id),
        )

    def all_done(self) -> bool:
        return bool(self.tasks) and all(t.status == "completed" for t in self.tasks)

    def by_id(self, task_id: str) -> PRDTask | None:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    def tasks_for_epic(self, epic_id: str) -> list[PRDTask]:
        return [t for t in self.tasks if t.epic_id == epic_id]

    def epic_progress(self, epic_id: str) -> tuple[int, int]:
        """(completed, total) tasks under an epic — derived, for banners/reports."""
        ts = self.tasks_for_epic(epic_id)
        return sum(t.status == "completed" for t in ts), len(ts)


class TaskVerdict(BaseModel):
    """The analyzer_validator's per-task completion judgement."""

    task_id: str
    completed: bool = False
    reason: str = ""


class PRDCritique(BaseModel):
    """The analyzer_validator's review of a whole PRD during the discussion."""

    approved: bool = False
    feedback: str = ""
    missing: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------- helpers


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
        t = t.strip("`\n")
    return t.strip()


def _first_json(text: str):
    """Best-effort: parse the whole thing, else the first {...}/[...] block."""
    t = _strip_fence(text)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    m = re.search(r"(\{.*\}|\[.*\])", t, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
    return None


# Keys under which the analyzer may put the task list, and the wrapper keys it may
# nest the whole PRD under. The model sometimes emits `{"prd": {"tasks": [...]}}` or
# uses `targets`/`user_stories` instead of `tasks` — so we search for a list-of-dicts
# under any of these, descending into wrappers, rather than only checking the top level.
_TASK_LIST_KEYS = ("tasks", "targets", "user_stories", "stories", "backlog", "items")
# Keys under which the analyzer may put the epic list (Job A of the two-phase plan).
_EPIC_LIST_KEYS = ("epics", "areas", "themes", "components", "surfaces")
# Per-task field aliases → our schema fields (the model varies its wording).
_DESC_KEYS = ("description", "approach", "hypothesis", "summary", "where", "details")
_AC_KEYS = ("acceptanceCriteria", "acceptance_criteria", "success", "done_when", "acceptance")
# The epic a task belongs to (Job B tags each task with its epic's id).
_EPIC_ID_KEYS = ("epic_id", "epicId", "epic", "area")
_SEVERITY = {"critical": 1, "high": 2, "medium": 3, "med": 3, "low": 4, "info": 5, "informational": 5}


def _is_task_list(v) -> bool:
    """A task list is a non-empty list that contains at least one object — so a
    `priority_order: ["recon", "xss"]` (list of strings) is never mistaken for it."""
    return isinstance(v, list) and any(isinstance(x, dict) for x in v)


def _find_tasks(obj) -> list:
    """Recursively locate the task list, preferring the known keys at each level and
    descending into wrapper dicts/lists (e.g. a top-level `prd` object)."""
    if _is_task_list(obj):
        return obj
    if isinstance(obj, dict):
        for key in _TASK_LIST_KEYS:
            if _is_task_list(obj.get(key)):
                return obj[key]
        for v in obj.values():
            found = _find_tasks(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_tasks(v)
            if found:
                return found
    return []


def _find_epics(obj) -> list:
    """Recursively locate the epic list — like `_find_tasks` but preferring
    `_EPIC_LIST_KEYS`, descending into wrapper dicts (e.g. `{"plan": {"epics": [...]}}`).
    Prefers a keyed epic list over a bare list of dicts, so `{"epics": [...]}` never
    resolves to some other list-of-dicts under the same object. Reuses `_is_task_list`
    so a bare string list is never mistaken for epics. Only ever called on epic-phase
    output, so a bare list of dicts IS the epics."""
    if isinstance(obj, dict):
        for key in _EPIC_LIST_KEYS:
            if _is_task_list(obj.get(key)):
                return obj[key]
        for v in obj.values():
            found = _find_epics(v)
            if found:
                return found
        return []
    if _is_task_list(obj):
        return obj
    if isinstance(obj, list):
        for v in obj:
            found = _find_epics(v)
            if found:
                return found
    return []


def _find_goal(obj) -> str:
    """Recursively pull a goal/engagement string out of the (possibly wrapped) PRD."""
    if isinstance(obj, dict):
        for key in ("goal", "engagement", "objective"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in obj.values():
            g = _find_goal(v)
            if g:
                return g
    elif isinstance(obj, list):
        for v in obj:
            g = _find_goal(v)
            if g:
                return g
    return ""


def _coerce_priority(pr) -> int:
    """Priority → int (lower = higher priority). Handles ints, `P0`/`P1`, severity
    words (critical/high/…), and bare digits; anything unknown defaults to 3."""
    if isinstance(pr, bool):
        return 3
    if isinstance(pr, (int, float)):
        return int(pr)
    if isinstance(pr, str):
        s = pr.strip().lower()
        m = re.fullmatch(r"p(\d+)", s)
        if m:
            return int(m.group(1))
        if s in _SEVERITY:
            return _SEVERITY[s]
        if s.isdigit():
            return int(s)
    return 3


def _first_str(data: dict, keys) -> str:
    """First non-empty value among `keys`, coerced to a string. The analyzer very
    commonly emits `acceptanceCriteria` (and sometimes `description`) as a LIST of
    bullet strings; without list-handling those tasks fail `PRDTask` construction (the
    field is `str`) and get silently dropped, yielding an empty PRD that the validator
    rejects forever — a phase-1 discussion loop. So flatten a list/tuple into a
    newline bullet list here."""
    for k in keys:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (list, tuple)):
            parts = [str(x).strip() for x in v if str(x).strip()]
            if parts:
                return "\n".join(f"- {p}" for p in parts)
    return ""


def parse_prd(text: str, *, goal: str = "") -> PRD:
    """Tolerantly parse an analyzer final into a PRD.

    Handles the shapes the analyzer actually emits: a bare `{"goal":..., "tasks":[...]}`,
    a bare task list `[...]`, a wrapped `{"prd": {"tasks": [...]}}`, and alternate task
    keys (`targets`, `user_stories`, …). Priorities may be ints, `P0`-style, or severity
    words; task bodies may use alias field names. Auto-assigns ids (`t1`, `t2`, …) when
    missing. A completely unparseable answer yields an empty PRD (the caller decides —
    usually retry / proceed with a warning)."""
    obj = _first_json(text)
    raw_tasks = _find_tasks(obj)
    prd_goal = _find_goal(obj) or goal

    tasks: list[PRDTask] = []
    for i, raw in enumerate(raw_tasks, start=1):
        if not isinstance(raw, dict):
            continue
        data = dict(raw)
        data["id"] = str(data.get("id") or f"t{i}")
        data["priority"] = _coerce_priority(data.get("priority", 3))
        # Fill body fields from the model's varied wording (keeps tasks non-blank).
        desc = _first_str(data, _DESC_KEYS)
        if desc:
            data["description"] = desc
        ac = _first_str(data, _AC_KEYS)
        if ac:
            data["acceptanceCriteria"] = ac
        st = str(data.get("status", "pending")).strip().lower()
        data["status"] = "completed" if st in ("completed", "complete", "done") else "pending"
        eid = _first_str(data, _EPIC_ID_KEYS)
        if eid:
            data["epic_id"] = eid
        try:
            tasks.append(PRDTask(**{k: data[k] for k in PRDTask.model_fields if k in data}))
        except Exception:  # noqa: BLE001 — skip a malformed task, keep the rest
            continue
    return PRD(goal=prd_goal, tasks=tasks)


def parse_epics(text: str, *, goal: str = "") -> list[Epic]:
    """Tolerantly parse an analyzer Job-A final into a list of epics.

    Mirrors `parse_prd`'s tolerance: handles a bare `{"goal":..., "epics":[...]}`, a
    bare epic list `[...]`, wrapped `{"plan": {"epics": [...]}}`, and alias list keys
    (`areas`, `themes`, …). Priorities may be ints/`P0`/severity words; bodies may use
    alias field names; a list-valued `acceptanceCriteria` is flattened (not dropped).
    Auto-assigns ids (`e1`, `e2`, …). A completely unparseable answer yields `[]` (the
    caller falls back to epic-free task planning)."""
    obj = _first_json(text)
    raw_epics = _find_epics(obj)

    epics: list[Epic] = []
    for i, raw in enumerate(raw_epics, start=1):
        if not isinstance(raw, dict):
            continue
        data = dict(raw)
        data["id"] = str(data.get("id") or f"e{i}")
        data["priority"] = _coerce_priority(data.get("priority", 3))
        desc = _first_str(data, _DESC_KEYS)
        if desc:
            data["description"] = desc
        ac = _first_str(data, _AC_KEYS)
        if ac:
            data["acceptanceCriteria"] = ac
        try:
            epics.append(Epic(**{k: data[k] for k in Epic.model_fields if k in data}))
        except Exception:  # noqa: BLE001 — skip a malformed epic, keep the rest
            continue
    return epics


def parse_critique(text: str) -> PRDCritique:
    """Parse an analyzer_validator PRD review. Strict JSON first, then prose
    salvage: an explicit "APPROVED" verdict counts as approval, anything else as
    not-approved with the prose kept as feedback."""
    obj = _first_json(text)
    if isinstance(obj, dict):
        try:
            return PRDCritique(
                approved=bool(obj.get("approved", False)),
                feedback=str(obj.get("feedback", "")),
                missing=[str(m) for m in (obj.get("missing") or [])],
            )
        except Exception:  # noqa: BLE001
            pass
    low = text.lower()
    approved = bool(re.search(r"\bapproved?\b", low)) and not re.search(
        r"\bnot approved\b|\breject", low
    )
    return PRDCritique(approved=approved, feedback=text.strip()[:2000])


def parse_task_verdicts(text: str) -> list[TaskVerdict]:
    """Parse an analyzer_validator completion check into per-task verdicts.

    Accepts `{"verdicts":[...]}` or a bare list. Each entry needs a `task_id`;
    `completed` is coerced from bool/str. Unparseable → empty list (no task is
    marked done, so the loop simply retries them next iteration — safe default)."""
    obj = _first_json(text)
    raw: list = []
    if isinstance(obj, dict):
        for key in ("verdicts", "tasks", "results"):
            if isinstance(obj.get(key), list):
                raw = obj[key]
                break
    elif isinstance(obj, list):
        raw = obj
    out: list[TaskVerdict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        tid = entry.get("task_id") or entry.get("id")
        if not tid:
            continue
        comp = entry.get("completed", entry.get("complete", False))
        if isinstance(comp, str):
            comp = comp.strip().lower() in ("true", "yes", "completed", "complete", "done")
        out.append(TaskVerdict(task_id=str(tid), completed=bool(comp), reason=str(entry.get("reason", ""))))
    return out


# --------------------------------------------------------------------- disk io


def save_prd(prd_path: Path, prd: PRD) -> None:
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(json.dumps(prd.model_dump(), indent=2), encoding="utf-8")


def load_prd(prd_path: Path) -> PRD | None:
    if not prd_path.exists():
        return None
    try:
        return PRD(**json.loads(prd_path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return None
