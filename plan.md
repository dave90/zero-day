# Plan: Modernize the `zero` CLI UI (color theme + polish)

## Context

`zero`'s inline shell (`src/zero/ui/`) is well-structured and uses nice unicode
glyphs, but it prints **plain monochrome strings** through
`print_formatted_text(str)` — no color, no styling anywhere. `rich>=13.7` is even
a declared dependency yet **completely unused**. The result reads as a flat wall
of gray text, unlike modern agent CLIs (Codex, Claude Code) which use semantic
color, a welcome banner, a live spinner, and styled status chrome.

Goal: make a `zero` session *feel* like a latest-generation coding-agent CLI —
**Violet/Claude-like accent**, full polish (welcome box, animated spinner, ctx
usage bar, severity-colored findings, risk-colored approval badges) — **without
changing what the event renderers output as text**.

### Hard constraint (drives the whole design)

Many unit tests assert on `render_event()` / `status_line()` output as **exact
plain strings** — e.g. `tests/unit/test_render_compaction.py` does
`list(render_event(...)) == ["  ↳ Mapping the login flow"]`, and
`test_render_findings.py` / `test_status_line.py` / `test_render_agent_name.py`
check substrings of joined output. **Therefore the text content of every
renderer must stay byte-identical.** Color is added as a **print-time styling
layer**, never by changing renderer return values. This keeps all render tests
green and isolates the change.

## Approach

Add one new module `src/zero/ui/theme.py` (the single source of truth for the
palette + glyphs + the plain-text→styled colorizer) and wire it into the print
path, the toolbar, and the prompt in `app.py`. Use **prompt_toolkit's native
`FormattedText` + `Style`** (NOT `rich`) — it's the rendering layer the inline
prompt already owns, so colors stay consistent and degrade correctly to no-ANSI
under `DummyOutput`/non-TTY (tests already drive the loop headlessly).

### 1. New file: `src/zero/ui/theme.py`

- **`THEME = Style.from_dict({...})`** — semantic classes for the Violet vibe:
  - `accent` violet (`#a855f7`/`ansimagenta`) — brand, tool-call arrows,
    reasoning `↳`, prompt `›`, turn label.
  - `dim` gray (`#6b7280`/`ansibrightblack`) — meta: `[call id]`, rules, args,
    secondary text.
  - `ok` green, `fail` red, `warn`/`pending` amber/yellow.
  - `finding.confirmed` bold green, `finding.rejected` red, `finding.pending`
    amber, `finding.duplicate` cyan.
  - `risk.safe` green · `risk.active` cyan · `risk.exploit` orange ·
    `risk.destructive` bold red.
  - `sev.crit`/`sev.high`/`sev.med`/`sev.low` for CVSS severity coloring.
  - `banner.brand`, `banner.box`, `spinner`, `bar.fill`, `bar.empty`,
    `toolbar.sel`.
- **Glyph/symbol constants** (centralized; values equal to what `render.py`
  already emits so the colorizer stays in sync): `ARR_CALL="→"`, `ARR_OBS="←"`,
  `REASON="↳"`, `STAR="★"`, `CROSS="✗"`, `CHECK="✓"`, `RUN="⟳"`, `HOURGLASS="⏳"`,
  spinner frames `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`, ctx-bar blocks `▰`/`▱`.
- **`stylize(line: str) -> FormattedText`** — the colorizer. Maps a finished
  plain line to styled fragments by its leading semantic marker (the markers are
  stable and owned by `render.py`):
  - `→ <tool>` → tool name `accent`, `[risk]` colored by risk word, args `dim`.
  - `← <tool> ok` → `ok`; `… FAIL` → `fail`; `[call …]` → `dim`.
  - `↳ …` reasoning → `accent` glyph + default text.
  - `★ CONFIRMED` → `finding.confirmed`; `✗ REJECTED` → `finding.rejected`;
    `… PENDING` → `finding.pending`; `═` rule lines → `dim`.
  - `CVSS …<n>` line → number colored by `_sev_class(n)` (≥9 crit, ≥7 high,
    ≥4 med, else low).
  - `╭─ goal`/`│`/`╰` box → `accent`; `⋯ [zero · turn n]` → `dim`;
    `✓ [pentester …]` → `ok`; `⟳ …` → `accent`; `? question` → `warn`;
    `⚠ …` → `warn`.
  - Default: pass through with no style (so unmatched lines just print plain).
  - A short module comment explains *why* it keys off markers (the test
    constraint above) — matching the repo's dense "why" comment style.
- **`ZERO_ASCII`** — a multi-line **ASCII-art "ZERO" wordmark** (ANSI-Shadow /
  block-letter figlet style), rendered in violet at the top of the welcome
  banner:

  ```
   ███████╗███████╗██████╗  ██████╗
   ╚══███╔╝██╔════╝██╔══██╗██╔═══██╗
     ███╔╝ █████╗  ██████╔╝██║   ██║
    ███╔╝  ██╔══╝  ██╔══██╗██║   ██║
   ███████╗███████╗██║  ██║╚██████╔╝
   ╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝
  ```

  Stored as a module constant so it's easy to swap fonts. (A `--no-banner` /
  narrow-terminal fallback to the compact `▌ zero` wordmark is a nice-to-have;
  if `cols < width(ascii)` we degrade to the one-line wordmark so it never
  wraps ugly.)
- **`welcome_banner(cfg) -> FormattedText`** — prints the violet `ZERO_ASCII`
  wordmark, then a rounded box (reusing the `╭─ ╰` glyph family already in
  `render.py`) with the tagline "autonomous web-app pentest agent", a config row
  (`v<version> · mode <m> · sandbox on/off · model … · files-root …`), and a dim
  tips line (`/help commands · ↑↓ inspect agents · ^C cancel · type a goal`).
  Reads from `orch.cfg` (mode, goal, model, `use_container`, `context_window`,
  files-root) — same fields the orchestrator startup line already uses.
- **`spinner_frame() -> str`** and **`ctx_bar(used, window) -> FormattedText`**
  helpers (frame chosen from `time.monotonic()`; bar colored green/amber/red by
  pressure).

### 2. Wire styling into `src/zero/ui/app.py`

- `PromptSession(... style=THEME ...)` so `class:` names resolve everywhere.
- **`_print`**: route each line through `theme.stylize()` →
  `print_formatted_text(ft, style=THEME)`. Accept both `str` (command output —
  still works, just colorized or plain) and already-built `FormattedText`.
- **`_prompt_message`**: return `FormattedText` — `› ` in `accent`; the
  `approve? [y/n/a]` and `answer ›` prompts colored (approval amber, ask cyan).
- **`_toolbar`**: rebuild as `FormattedText` with `class:` segments:
  - Running-subagent rows: replace the static `⏳` with an **animated spinner
    frame** (`theme.spinner_frame()`), selected row marker `›` + `toolbar.sel`
    style, elapsed/ctx in `dim`.
  - Status line: colorize segments — `turn` accent, `approvals waiting: N`
    amber when `N>0` else dim, `N confirmed` green / `N rejected` red, and a
    **ctx usage bar** `▰▰▱▱ 41%` via `ctx_bar()`. The plain-text `status_line()`
    in `render.py` stays unchanged (tests depend on it); the toolbar builds its
    own styled version from the same `Status` fields.
- **`_approval_lines`**: keep the text, but build it as `FormattedText` so the
  `risk: <level>` badge is colored by `risk.<level>` and the `[y]/[n]/[a]` hint
  is legible. (This helper is local to `app.py`, not covered by render tests.)
- **`run()` startup**: print `theme.welcome_banner(self.orch.cfg)` once before
  the prompt loop. Harmless under `DummyOutput` (tests drive the loop headlessly
  and assert on submitted text, not printed chrome).

### 3. Keep / trim the orchestrator startup line

`orchestrator.py:395` emits an effective-backend config line (kept deliberately
for the "is my fix live?" debugging note in the code comment). Leave it — the
colorizer renders it `dim` so it reads as a subtle subtitle under the welcome
box. No behavior change there.

## Files to modify

| File | Change |
|---|---|
| `src/zero/ui/theme.py` | **NEW** — `THEME` Style, glyph constants, `stylize`, `welcome_banner`, `spinner_frame`, `ctx_bar`, `_sev_class`. |
| `src/zero/ui/app.py` | Wire `style=THEME`; colorize `_print`, `_prompt_message`, `_toolbar`, `_approval_lines`; print welcome banner in `run()`. |
| `src/zero/ui/render.py` | **No text changes.** Optionally import glyph constants from `theme.py` so the renderer and colorizer share one source of truth (values unchanged → tests still pass). |
| `tests/unit/test_theme.py` | **NEW** — assert `stylize()` returns the right style class for representative lines (tool/obs/finding/risk/cvss), `ctx_bar`/`_sev_class` thresholds, and that `fragment_list_to_text(stylize(x)) == x` for every case (proves text is never mutated — the core invariant). |

## Reuse (don't reinvent)

- `fmt_tokens` / `fmt_context` (`render.py:247-261`) for the ctx bar percentage.
- Existing glyphs in `render.py` (`╭─ ╰ │ → ← ↳ ★ ✗ ✓ ⟳ ═`) — the box and
  colorizer reuse them, not new ones.
- prompt_toolkit `FormattedText`, `Style.from_dict`, `print_formatted_text(...,
  style=...)`, and `from prompt_toolkit.formatted_text import fragment_list_to_text`
  (for the text-invariance test). These are already the rendering primitives in
  `app.py`.

## Verification

1. `uv run pytest tests/unit -q` — **all existing render/status/ui tests must
   stay green** (proves text content unchanged). New `test_theme.py` passes,
   including the `fragment_list_to_text(stylize(x)) == x` invariant.
2. `uv run ruff check .` — clean (line-length 100, py311).
3. **Live look** (the real proof — colors only show on a TTY):
   `uv run zero init` then
   `uv run zero run --goal "Find all the vulnerabilities" --yolo` against the
   gruyere target — confirm in a real terminal: violet welcome box, animated
   spinner in the toolbar while subagents run, colored tool `→/←` lines, a
   confirmed finding card with green header + severity-colored CVSS, and the
   colored status bar with the ctx usage bar. (Per repo guidance: verify the
   real path before calling it done — color is invisible to the headless tests.)
4. Sanity-check no-color degradation: piping output (`| cat`) or a non-TTY must
   not emit raw ANSI garbage — prompt_toolkit strips styling for non-terminal
   outputs automatically; confirm once.

## Out of scope

- No switch to a full-screen Textual TUI (stays inline — the existing
  architecture and its scrollback/native-copy guarantees are preserved).
- No `rich` adoption (standardize on prompt_toolkit for one consistent styling
  system; the unused `rich` dep can be removed in a later cleanup, not here).
- No changes to event semantics, the orchestrator, or what reaches the chat vs.
  the toolbar.
