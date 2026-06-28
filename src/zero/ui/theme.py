"""Color theme + glyphs + a print-time colorizer for the inline shell.

WHY a separate styling layer instead of coloring the renderers directly:
`render.py` turns UIEvents into PLAIN text lines, and a pile of unit tests assert
on that text *byte-for-byte* (e.g. `list(render_event(...)) == ["  ↳ Mapping…"]`).
So we must never change what the renderers return. Instead we colorize each
finished line here, at print time, keyed off the stable leading glyph/marker that
`render.py` already emits. `stylize()` is text-preserving by contract — the
`tests/unit/test_theme.py` invariant `fragment_list_to_text(stylize(x)) == x`
locks that down — so it adds color without ever mutating a character.

The vibe is Violet/Claude-like for the live session; the startup banner is a
deliberate black-&-white homage to Zerocalcare (the cartoonist this tool's name
nods to) — his Armadillo, hand-lettered, deadpan.
"""
from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.styles import Style

from zero.ui.render import fmt_context, fmt_tokens

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from zero.orchestrator.orchestrator import OrchestratorConfig


# ----------------------------------------------------------------- palette

# Violet/Claude-like accent for the live session; semantic colors stay
# conventional (green ok/confirmed, red fail/rejected, amber pending/warn). Hex
# values let prompt_toolkit pick truecolor and degrade to 256/16 automatically.
THEME = Style.from_dict(
    {
        "accent": "#a855f7",
        "accent.b": "bold #a855f7",
        "dim": "#7d7d7d",
        "ok": "#22c55e",
        "fail": "#ef4444",
        "warn": "#f59e0b",
        "finding.confirmed": "bold #22c55e",
        "finding.rejected": "#ef4444",
        "finding.pending": "#f59e0b",
        "finding.duplicate": "#06b6d4",
        # risk badges, cool→hot
        "risk.safe": "#22c55e",
        "risk.active": "#06b6d4",
        "risk.exploit": "#f97316",
        "risk.destructive": "bold #ef4444",
        # CVSS severity
        "sev.crit": "bold #ef4444",
        "sev.high": "#f97316",
        "sev.med": "#f59e0b",
        "sev.low": "#22c55e",
        # banner (B&W Zerocalcare homage — no violet here on purpose)
        "banner.brand": "bold #f5f5f5",
        "banner.ink": "#cfcfcf",
        "banner.faint": "#7d7d7d",
        # toolbar / status
        "spinner": "#a855f7",
        "toolbar.sel": "bold #a855f7",
        "bar.ok": "#22c55e",
        "bar.warn": "#f59e0b",
        "bar.hot": "#ef4444",
        "bar.empty": "#4b4b4b",
        "prompt": "bold #a855f7",
        "prompt.ask": "bold #06b6d4",
        "prompt.approve": "bold #f59e0b",
        "rule": "#a855f7",            # the bars bracketing the input line
        # Markdown rendering of the agent's prose replies.
        "md.h1": "bold #a855f7",
        "md.h2": "bold #a855f7",
        "md.h3": "bold #cfcfcf",
        "md.bold": "bold #f5f5f5",
        "md.italic": "italic #cfcfcf",
        "md.code": "#06b6d4",
        "md.bullet": "bold #a855f7",
        # keep the bottom toolbar inline (no reverse-video bar), let our classes show
        "bottom-toolbar": "noreverse bg:default",
    }
)


# ------------------------------------------------------------------ glyphs

# Centralized so the renderer and this colorizer agree. Values MUST equal what
# render.py already emits (the render tests pin the exact characters).
ARR_CALL = "→"
ARR_OBS = "←"
REASON = "↳"
STAR = "★"
CROSS = "✗"
CHECK = "✓"
RUN = "⟳"
HOURGLASS = "⏳"

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BAR_FILL = "▰"
BAR_EMPTY = "▱"


# --------------------------------------------------------------- colorizer

_RISK_CLASS = {
    "safe": "risk.safe",
    "active": "risk.active",
    "exploit": "risk.exploit",
    "destructive": "risk.destructive",
}

_RE_TOOL_CALL = re.compile(r"^(\s*→\s+)(\S+)(\s+\[)(\w+)(\]\s*)(.*)$")
_RE_TOOL_OBS = re.compile(r"^(\s*←\s+)(\S+)(\s+)(ok|FAIL)(.*)$")
_RE_CVSS = re.compile(r"^(\s*CVSS 4\.0:\s*)([\d.]+)(.*)$")


def risk_class(level: str) -> str:
    """Style class for a tool RiskLevel badge (safe→green … destructive→red)."""
    return _RISK_CLASS.get(level, "dim")


def _sev_class(score: float) -> str:
    if score >= 9.0:
        return "sev.crit"
    if score >= 7.0:
        return "sev.high"
    if score >= 4.0:
        return "sev.med"
    return "sev.low"


def stylize(line: str) -> FormattedText:
    """Map ONE finished plain line to colored fragments by its leading marker.

    Contract: the concatenation of all fragment texts equals `line` exactly —
    we only attach styles, never edit text. Unmatched lines pass through plain.
    """
    stripped = line.lstrip()

    # Tool proposal:  "  → nmap  [active]  target=…"
    m = _RE_TOOL_CALL.match(line)
    if m:
        pre, tool, lb, risk, rb, rest = m.groups()
        risk_cls = _RISK_CLASS.get(risk, "dim")
        return FormattedText(
            [
                ("class:accent", pre + tool),
                (f"class:{risk_cls}", lb + risk + rb),
                ("class:dim", rest),
            ]
        )

    # Tool observation:  "  ← nmap ok  [call abcd]"  /  "  ← x FAIL (truncated)  [call …]"
    m = _RE_TOOL_OBS.match(line)
    if m:
        pre, tool, gap, status, rest = m.groups()
        status_cls = "ok" if status == "ok" else "fail"
        return FormattedText(
            [
                ("class:accent", pre + tool),
                (f"class:{status_cls}", gap + status),
                ("class:dim", rest),
            ]
        )

    # CVSS line in a finding card — color the score by severity.
    m = _RE_CVSS.match(line)
    if m:
        label, score, rest = m.groups()
        try:
            cls = _sev_class(float(score))
        except ValueError:
            cls = "dim"
        return FormattedText([("class:dim", label), (f"class:{cls}", score), ("class:dim", rest)])

    # Reasoning headline:  "  ↳ Mapping the login flow"
    if stripped.startswith(REASON + " "):
        idx = line.index(REASON)
        return FormattedText(
            [("class:accent", line[: idx + 1]), ("", line[idx + 1 :])]
        )

    # Finding labels & summary items, by their leading glyph.
    if stripped.startswith(STAR):
        return _whole("finding.confirmed", line)
    if stripped.startswith(CROSS):
        return _whole("finding.rejected", line)
    if stripped.startswith("⤴"):
        return _whole("finding.duplicate", line)
    if stripped.startswith("… ("):  # observation truncation hint
        return _whole("dim", line)
    if stripped.startswith("…"):  # pending label / pending summary item
        return _whole("finding.pending", line)

    # Subagent lifecycle.
    if stripped.startswith(CHECK):
        return _whole("ok", line)
    if stripped.startswith(RUN):
        return _whole("accent", line)
    if stripped.startswith("⨯"):  # turn cancelled
        return _whole("fail", line)
    if stripped.startswith("⚠"):  # warnings (e.g. sandbox fell back to host)
        return _whole("warn", line)
    if stripped.startswith("?"):  # ask_user
        return _whole("warn", line)
    if stripped.startswith("⋯"):  # turn-start "thinking…"
        return _whole("dim", line)
    if stripped.startswith("▸"):  # deterministic activity line
        return _whole("dim", line)

    # Box drawing (goal banner, plan checklist) → accent; heavy rules → dim.
    if stripped[:1] in "╭╮╰╯│┌└├":
        return _whole("accent", line)
    if stripped[:1] == "═":
        return _whole("dim", line)

    # Report header + the effective-config startup line read as quiet subtitles.
    if stripped.startswith("ENGAGEMENT REPORT"):
        return _whole("accent.b", line)
    if stripped.startswith("zero · mode"):
        return _whole("dim", line)

    return FormattedText([("", line)])


def _whole(cls: str, line: str) -> FormattedText:
    return FormattedText([(f"class:{cls}", line)])


# ------------------------------------------------------------- live toolbar

def rule(width: int) -> tuple[str, str]:
    """One violet full-width bar fragment — the Claude-style rule that brackets
    the input line (above via the prompt message, below via the toolbar)."""
    return ("class:rule", "─" * max(0, width))


def spinner_frame() -> str:
    """A Braille spinner frame chosen from the wall clock, so successive toolbar
    refreshes (every 0.5 s) visibly rotate it while agents are working."""
    return SPINNER_FRAMES[int(time.monotonic() * 8) % len(SPINNER_FRAMES)]


def ctx_bar(used: int, window: int, *, width: int = 8) -> FormattedText:
    """`▰▰▱▱▱▱▱▱ ctx 48k/200k (24%)` — a compact usage bar, colored by pressure
    (green <70%, amber <90%, red ≥90%). Empty when no window is configured."""
    if window <= 0:
        return FormattedText([])
    pct = min(100, round(100 * used / window))
    fill = round(width * pct / 100)
    cls = "bar.ok" if pct < 70 else ("bar.warn" if pct < 90 else "bar.hot")
    label = f" {fmt_context(used, window)}"  # "ctx 48k/200k (24%)"
    return FormattedText(
        [
            (f"class:{cls}", BAR_FILL * fill),
            ("class:bar.empty", BAR_EMPTY * (width - fill)),
            ("class:dim", label),
        ]
    )


# ----------------------------------------------------------- welcome banner

# ANSI-Shadow-ish block wordmark, rendered bold-white (the B&W ink).
_ZERO_WORDMARK = r"""
 ███████╗███████╗██████╗  ██████╗
 ╚══███╔╝██╔════╝██╔══██╗██╔═══██╗
   ███╔╝ █████╗  ██████╔╝██║   ██║
  ███╔╝  ██╔══╝  ██╔══██╗██║   ██║
 ███████╗███████╗██║  ██║╚██████╔╝
 ╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝
"""

_COMPACT_WORDMARK = "▌ zero"


def welcome_banner(cfg: "OrchestratorConfig", *, width: int = 80) -> FormattedText:
    """Startup chrome: the ZERO wordmark + a config row + a tips line. Degrades
    to a one-line wordmark on narrow terminals so the art never wraps."""
    frags: list[tuple[str, str]] = []

    def line(cls: str, text: str = "") -> None:
        frags.append((f"class:{cls}", text + "\n"))

    # Breathing room above the wordmark so it doesn't sit flush against the
    # shell prompt / prior output.
    line("")
    line("")

    narrow = width < 40
    if narrow:
        line("banner.brand", f"{_COMPACT_WORDMARK}  ")
    else:
        for row in _ZERO_WORDMARK.strip("\n").splitlines():
            line("banner.brand", row)

    line("")
    line("banner.ink", "  autonomous web-app pentest agent")

    sandbox = "sandbox on" if getattr(cfg, "use_container", True) else "host exec"
    backend = getattr(cfg, "backend", "claude")
    model = getattr(cfg, "model", None) or "account-default"
    if backend == "codex":
        # The shared --model default is a Claude id the Codex backend ignores (Codex
        # rejects it) — show the model Codex will actually use, not the bare default.
        from zero.llm.codex_cli import codex_model

        model = codex_model(getattr(cfg, "model", None)) or "codex-default"
    ctxw = fmt_tokens(getattr(cfg, "context_window", 0) or 0)
    froot = getattr(cfg, "files_root", "zero_files")
    config = (
        f"  mode {getattr(cfg, 'execution_mode', 'c')} · {backend} · {sandbox} · "
        f"{model} · ctx {ctxw} · files {froot}"
    )
    line("banner.faint", config)
    line("banner.faint", "  /help · ↑↓ inspect agents · ^C cancel (×2 quit) · type a goal ↵")
    line("")
    return FormattedText(frags)


# ---------------------------------------------------- markdown (agent replies)

# The agent answers in Markdown. Printing it raw shows literal `##`, `**` and
# backticks, which is hard to read — so we render it to styled terminal text.
# This is a focused renderer for the constructs agents actually use (headers,
# bold/italic, inline code, bullet + numbered lists, block quotes, fences); it
# stays in the prompt_toolkit styling system (no `rich`) so the agent's prose
# matches the rest of the violet theme.

# Cheap "is this worth rendering" probe: bold, inline code, a code fence, a
# table delimiter row, or a line that starts with a header / list / quote
# marker. Plain agent lines (startup config, "ready —", error tags) have none of
# these and keep their existing styling.
_MD_HINT = re.compile(
    r"\*\*.+?\*\*"           # bold
    r"|`[^`]+`"              # inline code
    r"|^\s{0,3}#{1,6}\s"     # header
    r"|^\s*[-*+]\s"          # bullet
    r"|^\s*\d+\.\s"          # numbered
    r"|^\s*>\s"              # block quote
    r"|^\s*```"              # code fence
    r"|^\s*\|.*-{3,}.*\|",   # table delimiter row (|---|---|)
    re.M,
)

_MD_INLINE = re.compile(
    r"(?P<code>`[^`]+`)"
    r"|(?P<bold>\*\*[^*]+\*\*|__[^_]+__)"
    r"|(?P<ital>\*[^*\s][^*]*\*|_[^_\s][^_]*_)"
)

_MD_TAG = re.compile(r"\[[^\]]+\]\s")          # a leading "[final] " / "[turn error] " tag
_MD_HR = re.compile(r"-{3,}|={3,}|\*{3,}")


def has_markdown(text: str) -> bool:
    return bool(_MD_HINT.search(text))


def _is_table_delim(line: str) -> bool:
    """True for a GitHub table delimiter row like `|---|:--:|`."""
    s = line.strip()
    return bool(s) and "-" in s and "|" in s and set(s) <= set("|-: ")


def _table_cells(row: str) -> list[str]:
    """Split a `| a | b |` row into trimmed cell strings (outer pipes dropped)."""
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _md_inline(text: str) -> list[tuple[str, str]]:
    """Split one line of text into (style, run) fragments on **bold**, *italic*,
    and `code`. Non-nested — which is all agent prose needs."""
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _MD_INLINE.finditer(text):
        if m.start() > pos:
            out.append(("", text[pos:m.start()]))
        if m.group("code"):
            out.append(("class:md.code", m.group("code")[1:-1]))
        elif m.group("bold"):
            out.append(("class:md.bold", m.group("bold")[2:-2]))
        else:
            out.append(("class:md.italic", m.group("ital")[1:-1]))
        pos = m.end()
    if pos < len(text):
        out.append(("", text[pos:]))
    return out or [("", text)]


def _plain_len(cell: str) -> int:
    """Visual width of a cell once inline markdown markers are stripped."""
    return sum(len(t) for _, t in _md_inline(cell))


def _render_table(out: list[tuple[str, str]], block: list[str], sep) -> None:
    """Render a GitHub table (header + delimiter + rows) as aligned columns. A
    header-less table (empty `| | |` header, common in agent finding tables) is
    rendered as a bold-label key/value grid."""
    header = _table_cells(block[0])
    rows = [_table_cells(r) for r in block[2:]]
    has_header = any(c.strip() for c in header)
    body = ([header] if has_header else []) + rows
    ncols = max((len(r) for r in body), default=0)
    if ncols == 0:
        return
    widths = [0] * ncols
    for r in body:
        for k in range(ncols):
            widths[k] = max(widths[k], _plain_len(r[k]) if k < len(r) else 0)

    def emit_row(cells: list[str], label_style: str = "") -> None:
        sep()
        out.append(("", "  "))                       # small left indent
        for k in range(ncols):
            cell = cells[k] if k < len(cells) else ""
            style0 = label_style if k == 0 else ""
            for st, txt in _md_inline(cell):
                out.append((st or style0, txt))
            if k < ncols - 1:                        # pad all but the last column
                out.append(("", " " * (widths[k] - _plain_len(cell)) + "  "))

    if has_header:
        emit_row(header, label_style="class:md.bold")
        sep()
        out.append(("class:dim", "  " + "─" * (sum(widths) + 2 * (ncols - 1))))
        for r in rows:
            emit_row(r)
    else:
        for r in rows:
            emit_row(r, label_style="class:md.bold")


def render_markdown(text: str, width: int = 80) -> FormattedText:
    """Render an agent's Markdown reply to styled FormattedText. A leading
    `[final]`/`[turn error]`-style tag (added by the orchestrator) is peeled off
    and dimmed so the prose underneath renders cleanly."""
    text = text.strip("\n")
    out: list[tuple[str, str]] = [("", "\n")]
    mtag = _MD_TAG.match(text)
    if mtag:
        out.append(("class:dim", mtag.group(0)))
        text = text[mtag.end():]

    lines = text.split("\n")
    emitted = False

    def sep() -> None:
        """Emit the newline separating blocks (none before the first block)."""
        nonlocal emitted
        if emitted:
            out.append(("", "\n"))
        emitted = True

    i = 0
    in_code = False
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        if stripped.startswith("```"):          # fence toggles a code block —
            in_code = not in_code               # markers themselves are hidden
            i += 1
            continue
        if in_code:                             # code content: indented, cyan
            sep()
            out.append(("class:dim", "  │ "))
            out.append(("class:md.code", raw))
            i += 1
            continue

        # A table: this line has a pipe and the NEXT line is a delimiter row.
        if "|" in raw and i + 1 < len(lines) and _is_table_delim(lines[i + 1]):
            block = [raw, lines[i + 1]]
            j = i + 2
            while j < len(lines) and lines[j].strip() and "|" in lines[j]:
                block.append(lines[j])
                j += 1
            _render_table(out, block, sep)
            i = j
            continue

        if stripped and _MD_HR.fullmatch(stripped):
            sep()
            out.append(("class:dim", "─" * min(max(10, width), 60)))
            i += 1
            continue

        mh = re.match(r"(#{1,6})\s+(.*)", raw)
        if mh:
            sep()
            level = len(mh.group(1))
            cls = "md.h1" if level == 1 else ("md.h2" if level == 2 else "md.h3")
            for st, txt in _md_inline(mh.group(2)):
                out.append((f"class:{cls}" if not st else st, txt))
            i += 1
            continue

        mb = re.match(r"(\s*)[-*+]\s+(.*)", raw)
        if mb:
            sep()
            out.append(("", mb.group(1)))
            out.append(("class:md.bullet", "• "))
            out.extend(_md_inline(mb.group(2)))
            i += 1
            continue

        mn = re.match(r"(\s*)(\d+)\.\s+(.*)", raw)
        if mn:
            sep()
            out.append(("", mn.group(1)))
            out.append(("class:md.bullet", f"{mn.group(2)}. "))
            out.extend(_md_inline(mn.group(3)))
            i += 1
            continue

        mq = re.match(r">\s?(.*)", raw)
        if mq:
            sep()
            out.append(("class:dim", "▏ "))
            for st, txt in _md_inline(mq.group(1)):
                out.append((st or "class:dim", txt))
            i += 1
            continue

        sep()
        out.extend(_md_inline(raw))
        i += 1

    return FormattedText(out)
