"""UIEvent types pushed from the orchestrator onto the UI queue.

The orchestrator never touches the screen directly; it emits events and the
UI thread renders them. This keeps the agent loop independent of any
particular front end and makes a future Textual swap (plan §8 open point 9)
a one-file change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class AgentTextDelta:
    """A chunk of streamed agent text."""

    kind: Literal["agent_text_delta"] = "agent_text_delta"
    agent: str = "pentest"
    text: str = ""


@dataclass
class AgentTextEnd:
    """Marks the end of a streamed agent message."""

    kind: Literal["agent_text_end"] = "agent_text_end"
    agent: str = "pentest"


@dataclass
class TodoUpdate:
    """The agent updated its task plan (Claude-Code TodoWrite). Rendered as a
    checklist. `label` is set for subagents so the UI keeps them out of the main
    chat (like the other labelled events); empty for the orchestrator."""

    kind: Literal["todo_update"] = "todo_update"
    items: list[dict] = field(default_factory=list)
    label: str = ""


@dataclass
class ReasoningSummary:
    """A concise summary of the agent's reasoning for ONE step — the Codex /
    Claude-Code style 'thinking' headline shown live in the chat. How much shows
    is controlled by `--thought-display` (condensed = the headline; full = the
    whole thought); the complete thought always goes to thinking.log.

    `label` is set for subagents so the UI keeps them out of the main chat (like
    the other labelled events); empty for the orchestrator / top-level agent."""

    kind: Literal["reasoning_summary"] = "reasoning_summary"
    agent: str = "pentest"
    turn: int = 0
    text: str = ""
    label: str = ""


@dataclass
class UserText:
    """User input submitted from the input pane."""

    kind: Literal["user_text"] = "user_text"
    text: str = ""


@dataclass
class ToolProposal:
    """The agent proposes a tool call. UI may show an approval overlay."""

    kind: Literal["tool_proposal"] = "tool_proposal"
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "safe"
    call_id: str = ""
    # Non-empty when this call belongs to a subagent (e.g. "pentester[#3 /x]").
    # The UI hides labelled (subagent) tool traffic from the chat.
    label: str = ""


@dataclass
class ApprovalPrompt:
    """The orchestrator needs the user to approve a tool call."""

    kind: Literal["approval_prompt"] = "approval_prompt"
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "safe"
    call_id: str = ""
    # Attribution: which subagent (and path) is requesting, e.g.
    # "pentester[#3 /api/orders]". Empty for the top-level agent.
    label: str = ""
    # Short "why" for the call (the agent's thought), shown in the prompt.
    reason: str = ""


@dataclass
class ToolRunning:
    """A tool call was approved and is now EXECUTING (emitted just before the tool
    runs, paired with the later ToolObservation by `call_id`). The UI keeps a live
    'running' indicator with elapsed time in the toolbar until the observation lands,
    so a long-running tool (e.g. a 300s ffuf) doesn't make the session look hung."""

    kind: Literal["tool_running"] = "tool_running"
    tool: str = ""
    call_id: str = ""
    label: str = ""              # subagent attribution (empty for the top-level agent)
    timeout_seconds: int = 0     # the tool's wall-clock cap, if known (for "12s/300s")


@dataclass
class ToolObservation:
    """Result of a tool call."""

    kind: Literal["tool_observation"] = "tool_observation"
    tool: str = ""
    call_id: str = ""
    ok: bool = True
    output: str = ""
    truncated: bool = False
    label: str = ""   # subagent attribution; UI hides labelled traffic from chat


@dataclass
class AskUser:
    """Agent is asking the user a question."""

    kind: Literal["ask_user"] = "ask_user"
    question: str = ""
    options: list[str] = field(default_factory=list)
    free_form: bool = True
    blocking: bool = True
    ask_id: str = ""


@dataclass
class Finding:
    """A confirmed/rejected/pending finding — rendered as a full card."""

    kind: Literal["finding"] = "finding"
    verdict: str = "pending"
    finding_id: str = ""
    title: str = ""
    endpoint: str = ""
    method: str = ""
    cwe: str = ""
    cvss_score: float = 0.0
    cvss_vector: str = ""
    rationale: str = ""
    reproducer: str = ""
    file_path: str = ""           # where it landed on disk
    duplicate_of: str | None = None


@dataclass
class SessionSummary:
    """All findings produced in this session, emitted at /exit."""

    kind: Literal["session_summary"] = "session_summary"
    confirmed: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    # Attack ideas that were dispatched but produced no confirmed finding
    # (NO_FINDING / ERROR). Each: {subagent_id, target, idea, status}.
    failed_attempts: list[dict[str, Any]] = field(default_factory=list)
    findings_dir: str = ""
    audit_path: str = ""
    turns: int = 0
    report_path: str = ""


@dataclass
class Status:
    """Footer/status update."""

    kind: Literal["status"] = "status"
    agent: str = "pentest"
    turn: int = 0
    # Orchestrator context-window usage: tokens in the window as of its last call,
    # and the window size (for the % display). 0/0 ⇒ not shown.
    context_tokens: int = 0
    context_window: int = 0
    mode: str = "a"
    pending_approvals: int = 0
    # Background validation counters (M6 async validation).
    validations_pending: int = 0
    validations_confirmed: int = 0
    validations_rejected: int = 0
    # Orchestrated-mode live counts (M8 multi-agent).
    running_pentesters: int = 0
    running_validators: int = 0


@dataclass
class TurnStart:
    """Emitted before each LLM call so the UI shows progress during the wait."""

    kind: Literal["turn_start"] = "turn_start"
    agent: str = "pentest"
    turn: int = 0


@dataclass
class ActivityLine:
    """A clean, one-line description of what the orchestrator is doing right now
    (e.g. dispatching a pentester, awaiting subagents, finalizing). Deterministic
    — built from the action, not from the model's free-form thought — so the user
    can follow along even when the chat would otherwise be silent."""

    kind: Literal["activity_line"] = "activity_line"
    text: str = ""
    # Non-empty when the line belongs to a subagent; the UI hides labelled
    # activity from the chat (consistent with labelled tool traffic).
    label: str = ""


@dataclass
class GoalBanner:
    """The current top-level goal, emitted once at session start."""

    kind: Literal["goal_banner"] = "goal_banner"
    text: str = ""


@dataclass
class TurnCancelled:
    """The current agent turn was cancelled (Ctrl+C once)."""

    kind: Literal["turn_cancelled"] = "turn_cancelled"
    reason: str = "interrupted_by_user"


@dataclass
class SubagentStarted:
    """A pentester/validator subagent began running (M8 orchestrated mode)."""

    kind: Literal["subagent_started"] = "subagent_started"
    subagent_id: str = ""          # e.g. "#3"
    subkind: str = "pentester"     # "pentester" | "validator"
    target: str = ""               # the path/URL it focuses on
    idea: str = ""                 # the attack idea / finding title


@dataclass
class SubagentProgress:
    """A subagent advanced a turn. Consumed by the live /running view, not
    printed to the scrollback (too chatty)."""

    kind: Literal["subagent_progress"] = "subagent_progress"
    subagent_id: str = ""
    status_text: str = ""
    turn: int = 0


@dataclass
class SubagentFinished:
    """A subagent completed (M8 orchestrated mode)."""

    kind: Literal["subagent_finished"] = "subagent_finished"
    subagent_id: str = ""
    subkind: str = "pentester"
    target: str = ""
    status: str = ""               # EXPLOITED|NO_FINDING|INCOMPLETE|ERROR or a verdict
    finding_ids: list[str] = field(default_factory=list)
    duration: float = 0.0


@dataclass
class RalphIteration:
    """A Ralph-mode phase/iteration boundary (planning, each loop iteration,
    completion). Rendered as a banner so the operator can follow the loop."""

    kind: Literal["ralph_iteration"] = "ralph_iteration"
    phase: str = "iteration"   # "analysis" | "discussion" | "iteration" | "complete"
    iteration: int = 0         # 0 for non-loop phases
    total: int = 0
    text: str = ""


@dataclass
class SwarmPhase:
    """A visible swarm lifecycle boundary in the session scrollback.

    Swarm coordination is otherwise mostly database/audit traffic.  These
    deliberately sparse banners make it obvious that the CLI is running a
    peer swarm without dumping every peer message into the operator chat.
    """

    kind: Literal["swarm_phase"] = "swarm_phase"
    phase: str = "bootstrap"
    size: int = 0
    text: str = ""


@dataclass
class SessionEnd:
    """The orchestrator is shutting down; UI should exit."""

    kind: Literal["session_end"] = "session_end"


UIEvent = (
    AgentTextDelta
    | AgentTextEnd
    | UserText
    | ToolProposal
    | ApprovalPrompt
    | ToolRunning
    | ToolObservation
    | AskUser
    | Finding
    | SessionSummary
    | Status
    | TurnStart
    | ActivityLine
    | GoalBanner
    | TurnCancelled
    | SubagentStarted
    | SubagentProgress
    | SubagentFinished
    | RalphIteration
    | SwarmPhase
    | SessionEnd
)
