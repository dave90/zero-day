"""Render UIEvents into text lines for the scrollback."""
from __future__ import annotations

import json
from typing import Iterable

from zero.ui.events import (
    ActivityLine,
    AgentTextDelta,
    AgentTextEnd,
    AskUser,
    Finding,
    GoalBanner,
    ReasoningSummary,
    SessionEnd,
    SessionSummary,
    Status,
    SubagentFinished,
    SubagentProgress,
    SubagentStarted,
    TodoUpdate,
    ToolObservation,
    ToolProposal,
    TurnCancelled,
    TurnStart,
    UIEvent,
    UserText,
)


def render_event(ev: UIEvent) -> Iterable[str]:
    """Return zero or more scrollback lines for an event."""
    if isinstance(ev, AgentTextDelta):
        if ev.text:
            yield ev.text
    elif isinstance(ev, ActivityLine):
        if ev.text:
            yield ev.text
    elif isinstance(ev, AgentTextEnd):
        return
    elif isinstance(ev, UserText):
        yield f"> {ev.text}"
    elif isinstance(ev, GoalBanner):
        yield ""
        yield "╭─ goal ─────"
        for line in ev.text.splitlines() or [""]:
            yield f"│  {line}"
        yield "╰────────────"
    elif isinstance(ev, TurnStart):
        yield ""
        yield f"⋯ [{_agent_display(ev.agent)} · turn {ev.turn}] thinking…"
    elif isinstance(ev, TodoUpdate):
        glyph = {"pending": "☐", "in_progress": "◐", "done": "☑"}
        yield "  ┌ plan"
        for it in ev.items or [{"step": "(empty)", "status": "pending"}]:
            g = glyph.get(it.get("status", "pending"), "☐")
            yield f"  │ {g} {it.get('step', '?')}"
        yield "  └─"
    elif isinstance(ev, ReasoningSummary):
        # The resolved "thinking…" — a concise reasoning summary (Codex/Claude-Code
        # style). First line is the headline; any remainder (only in `full` mode) is
        # indented under it.
        lines = ev.text.strip().splitlines() or [""]
        yield f"  ↳ {lines[0]}"
        for line in lines[1:]:
            if line.strip():
                yield f"    {line}"
    elif isinstance(ev, ToolProposal):
        pretty = summarize_tool_call(ev.tool, ev.args)
        yield f"  → {ev.tool}  [{ev.risk_level}]  {pretty}"
    elif isinstance(ev, ToolObservation):
        status = "ok" if ev.ok else "FAIL"
        # Tight: show at most ~6 lines / 400 chars in the scrollback.
        # The full output is in .pentest/audit.jsonl for that call_id.
        compact, hidden_lines, hidden_chars = _compact_observation(
            ev.output, max_lines=6, max_chars=400
        )
        suffix = " (truncated)" if ev.truncated else ""
        yield f"  ← {ev.tool} {status}{suffix}  [call {ev.call_id}]"
        for line in compact.splitlines() or [""]:
            yield f"      {line}"
        if hidden_lines or hidden_chars:
            bits = []
            if hidden_lines:
                bits.append(f"{hidden_lines} more lines")
            if hidden_chars and not hidden_lines:
                bits.append(f"{hidden_chars} more chars")
            yield f"      … ({', '.join(bits)} — full output in audit.jsonl)"
    elif isinstance(ev, AskUser):
        opts = ""
        if ev.options:
            opts = " — options: " + ", ".join(f"{i+1}) {o}" for i, o in enumerate(ev.options))
        yield f"  ? {ev.question}{opts}"
    elif isinstance(ev, Finding):
        yield from _render_finding_card(ev)
    elif isinstance(ev, SessionSummary):
        yield from _render_session_summary(ev)
    elif isinstance(ev, TurnCancelled):
        yield f"  ⨯ turn cancelled: {ev.reason}"
    elif isinstance(ev, SubagentStarted):
        yield f"  ⟳ [{ev.subkind} {ev.subagent_id}] started — {ev.target}  «{ev.idea}»"
    elif isinstance(ev, SubagentFinished):
        extra = f"  findings: {', '.join(ev.finding_ids)}" if ev.finding_ids else ""
        yield f"  ✓ [{ev.subkind} {ev.subagent_id}] {ev.status} — {ev.target}  ({ev.duration:.0f}s){extra}"
    elif isinstance(ev, SubagentProgress):
        return  # too chatty for the scrollback; feeds the live /running view
    elif isinstance(ev, Status):
        return
    elif isinstance(ev, SessionEnd):
        yield "  (session ended)"


def _render_finding_card(ev: Finding) -> Iterable[str]:
    bar = "═" * 60
    label = {
        "confirmed": "★ CONFIRMED",
        "rejected":  "✗ REJECTED ",
        "pending":   "… PENDING  ",
        "duplicate": "⤴ DUPLICATE",
    }.get(ev.verdict, f"  {ev.verdict.upper():<10}")
    yield ""
    yield bar
    yield f"{label} — {ev.title or '(untitled)'}"
    if ev.finding_id:
        yield f"   id:        {ev.finding_id}"
    if ev.duplicate_of:
        yield f"   duplicate of: {ev.duplicate_of}"
    if ev.endpoint or ev.method:
        ep = f"{ev.method} {ev.endpoint}".strip()
        yield f"   endpoint:  {ep}"
    if ev.cwe:
        yield f"   CWE:       {ev.cwe}"
    if ev.cvss_score or ev.cvss_vector:
        cvss_line = f"   CVSS 4.0:  {ev.cvss_score}"
        if ev.cvss_vector:
            cvss_line += f"  {ev.cvss_vector}"
        yield cvss_line
    if ev.rationale.strip():
        yield ""
        yield "   Rationale:"
        for line in ev.rationale.strip().splitlines():
            yield f"     {line}"
    if ev.reproducer.strip():
        yield ""
        yield "   Reproducer:"
        for line in ev.reproducer.strip().splitlines():
            yield f"     {line}"
    if ev.file_path:
        yield ""
        yield f"   written to: {ev.file_path}"
    yield bar
    yield ""


def _render_session_summary(ev: SessionSummary) -> Iterable[str]:
    bar = "═" * 60
    yield ""
    yield bar
    yield "  ENGAGEMENT REPORT"
    yield bar
    n_c, n_r, n_p = len(ev.confirmed), len(ev.rejected), len(ev.pending)
    yield (
        f"  {n_c} confirmed · {n_r} rejected · {n_p} pending"
        f"  ({ev.turns} agent turns)"
    )
    if not (n_c or n_r or n_p):
        yield "  (no findings this session)"
    if n_c:
        yield ""
        yield "  Confirmed:"
        for f in ev.confirmed:
            cvss = f.get("cvss_score") or 0.0
            yield f"    ★ {f.get('title', '(untitled)')}  [{f.get('id', '?')}]  CVSS {cvss}"
    if n_r:
        yield ""
        yield "  Rejected:"
        for f in ev.rejected:
            yield f"    ✗ {f.get('title', '(untitled)')}  [{f.get('id', '?')}]"
    if n_p:
        yield ""
        yield "  Pending:"
        for f in ev.pending:
            yield f"    … {f.get('title', '(untitled)')}  [{f.get('id', '?')}]"
    if ev.failed_attempts:
        yield ""
        yield "  Tested but didn't work:"
        for a in ev.failed_attempts:
            yield f"    ✗ {a.get('idea', '?')} @ {a.get('target', '?')}  ({a.get('status', '?')})"
    yield ""
    if ev.findings_dir:
        yield f"  Findings dir: {ev.findings_dir}"
    if ev.audit_path:
        yield f"  Audit log:    {ev.audit_path}"
    if ev.report_path:
        yield f"  Report:       {ev.report_path}"
    yield bar
    yield "  (engagement still open — ask about a finding or give another goal)"
    yield ""


def summarize_tool_call(tool: str, args: dict) -> str:
    """Compact, human-readable rendering of a tool call's args.

    Falls back to a truncated JSON dump for tools we don't have a
    custom summary for.
    """
    a = args or {}
    if tool == "http_replay" or tool == "curl_raw":
        method = a.get("method", "GET")
        url = a.get("url", "")
        return f"{method} {url}"
    if tool == "web_fetch":
        return f"GET {a.get('url', '')}"
    if tool == "web_search":
        return f"query={a.get('query', '')!r}"
    if tool == "nmap":
        return f"target={a.get('target', '')} ports={a.get('ports') or 'default'}"
    if tool == "ffuf":
        return f"url={a.get('url', '')} wordlist={a.get('wordlist', '')}"
    if tool == "nuclei":
        return f"target={a.get('target', '')} tags={a.get('tags') or '-'}"
    if tool == "sqlmap":
        return f"url={a.get('url', '')} (level={a.get('level', 1)} risk={a.get('risk', 1)})"
    if tool == "httpx_tool":
        return f"target={a.get('target', '')}"
    if tool == "jwt_tool":
        tok = a.get("token", "")
        return f"token={_short(tok, 30)} mode={a.get('mode', '-S')}"
    if tool == "burp_traffic":
        return f"action={a.get('action', '')} " + _short(
            json.dumps({k: v for k, v in a.items() if k != "action"}), 120
        )
    if tool == "python_exec":
        return f"code={_short(a.get('code', ''), 120)!r}"
    if tool == "finding_submit":
        return f"title={a.get('title', '')!r} cwe={a.get('cwe', '')}"
    # fallback
    return _short(json.dumps(a, ensure_ascii=False), 200)


def _agent_display(name: str) -> str:
    """User-facing name for an agent. The orchestrator is presented as `zero`
    (its internal identifier stays 'orchestrator' for audit/logs/session keys)."""
    return "zero" if name == "orchestrator" else name


def fmt_tokens(n: int) -> str:
    """Compact token count: 940, 48k, 1.2M."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.0f}k"
    return f"{n / 1_000_000:.1f}M"


def fmt_context(used: int, window: int) -> str:
    """`48k/200k (24%)` — the context-window usage segment. Empty if no window."""
    if window <= 0:
        return ""
    pct = min(100, round(100 * used / window))
    return f"ctx {fmt_tokens(used)}/{fmt_tokens(window)} ({pct}%)"


def status_line(st: Status) -> str:
    # Distinct, non-overlapping labels:
    #   approvals waiting → tool calls awaiting YOUR y/n
    #   findings          → confirmed / rejected results so far
    #   ctx               → orchestrator context-window usage (used/window %)
    ctx = fmt_context(st.context_tokens, st.context_window)
    ctx_seg = f" · {ctx}" if ctx else ""
    return (
        f" turn {st.turn} · mode {st.mode}"
        f" · approvals waiting: {st.pending_approvals}"
        f" · findings: {st.validations_confirmed} confirmed / {st.validations_rejected} rejected"
        f"{ctx_seg} "
    )


def _short(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f" … ({len(s) - n} more chars)"


def short_reason(reason: str, limit: int = 200) -> str:
    """Condense a free-form agent thought to a single clean line — the first
    sentence, whitespace-collapsed and clipped. Used for the approval prompt's
    `why:` and for the condensed in-chat thought."""
    if not reason:
        return ""
    text = " ".join(reason.strip().split())
    for sep in (". ", "; "):
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _compact_observation(
    text: str, *, max_lines: int, max_chars: int
) -> tuple[str, int, int]:
    """Trim `text` to at most `max_lines` lines and `max_chars` characters.

    Returns (compacted_text, hidden_line_count, hidden_char_count).
    `hidden_line_count` is nonzero when we cut by line; `hidden_char_count`
    is set when the cut was character-based (the single-line case).
    """
    if not text:
        return "", 0, 0
    lines = text.splitlines()
    hidden_lines = 0
    if len(lines) > max_lines:
        hidden_lines = len(lines) - max_lines
        lines = lines[:max_lines]
    compact = "\n".join(lines)
    hidden_chars = 0
    if len(compact) > max_chars:
        hidden_chars = len(compact) - max_chars
        compact = compact[:max_chars]
    return compact, hidden_lines, hidden_chars
