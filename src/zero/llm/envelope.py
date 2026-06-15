"""Parse the three envelope shapes the agent is allowed to emit.

The agent must reply with exactly one of:

  {"type": "action",   "tool": "...", "args": {...}, "thought": "..."}
  {"type": "actions",  "thought": "...", "calls": [{"tool": "...", "args": {...}}, ...]}
  {"type": "ask_user", "question": "...", "options": [...], "free_form": bool, "blocking": bool}
  {"type": "final",    "answer": "..."}

`actions` is the batch form: several INDEPENDENT tool calls run in one turn (one
LLM round-trip) instead of one-per-turn. `action` (singular) is still valid and
unchanged.

Anything else (extra fields, missing required fields, prose around the JSON)
is a parse error. The caller is expected to issue one-shot retry with a
"format only as envelope" reminder.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


class EnvelopeError(ValueError):
    """The model output was not a valid envelope."""


@dataclass
class ActionEnvelope:
    tool: str
    args: dict[str, Any]
    thought: str = ""


@dataclass
class ActionsEnvelope:
    """A batch of tool calls to run in ONE turn (one LLM round-trip). The caller
    runs them (independent ones concurrently) and appends one observation per call."""

    calls: list[ActionEnvelope]
    thought: str = ""


@dataclass
class AskUserEnvelope:
    question: str
    options: list[str]
    free_form: bool
    blocking: bool


@dataclass
class FinalEnvelope:
    answer: str


Envelope = ActionEnvelope | ActionsEnvelope | AskUserEnvelope | FinalEnvelope


# Cap on tool calls per `actions` batch — a backstop against a runaway batch, not a
# tuning knob. The concurrency cap is enforced separately by the executor.
MAX_BATCH_CALLS = 8

_VALID_TYPES = {"action", "actions", "ask_user", "final"}
_ACTION_FIELDS = {"type", "tool", "args", "thought"}
_ACTIONS_FIELDS = {"type", "calls", "thought"}
_ASK_FIELDS = {"type", "question", "options", "free_form", "blocking"}
_FINAL_FIELDS = {"type", "answer"}


def parse_envelope(text: str) -> Envelope:
    """Parse a single envelope from `text`. Raises EnvelopeError on failure.

    Every failure embeds the model's literal output (`| raw: …`) so the audit log
    shows WHAT the model actually emitted, not just a parser column number — that
    is the only way to debug a model that stops emitting valid envelopes.

    Tolerant of TRAILING DATA after the first JSON object: models sometimes emit
    a valid envelope followed by extra content (a second object, stray prose, a
    concatenated payload). We decode the first JSON value and ignore the rest
    rather than failing the whole turn with a "Extra data" JSONDecodeError."""
    try:
        return _parse_envelope_inner(text)
    except EnvelopeError as e:
        msg = str(e)
        if "| raw:" in msg:
            raise
        raise EnvelopeError(f"{msg} | raw: {text[:500]!r}") from e


def _parse_envelope_inner(text: str) -> Envelope:
    raw = _extract_json(text)
    try:
        obj, _end = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError:
        # One light repair pass for the most common LLM JSON slips (a trailing
        # comma before } or ], Python literals) before giving up — a single
        # stray comma shouldn't kill the whole turn.
        repaired = _repair_json(raw)
        try:
            obj, _end = json.JSONDecoder().raw_decode(repaired)
        except json.JSONDecodeError as e:
            raise EnvelopeError(f"not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise EnvelopeError("envelope must be a JSON object")
    t = obj.get("type")
    if t not in _VALID_TYPES:
        raise EnvelopeError(f"unknown envelope type: {t!r}")

    if t == "action":
        _strict_fields(obj, _ACTION_FIELDS, required={"type", "tool", "args"})
        tool = obj["tool"]
        args = obj["args"]
        if not isinstance(tool, str) or not tool:
            raise EnvelopeError("'tool' must be a non-empty string")
        if not isinstance(args, dict):
            raise EnvelopeError("'args' must be an object")
        return ActionEnvelope(tool=tool, args=args, thought=obj.get("thought", ""))

    if t == "actions":
        _strict_fields(obj, _ACTIONS_FIELDS, required={"type", "calls"})
        calls = obj["calls"]
        if not isinstance(calls, list) or not calls:
            raise EnvelopeError("'calls' must be a non-empty list")
        if len(calls) > MAX_BATCH_CALLS:
            raise EnvelopeError(
                f"too many calls in one batch ({len(calls)} > {MAX_BATCH_CALLS}) — "
                "split into separate turns"
            )
        parsed_calls: list[ActionEnvelope] = []
        for i, c in enumerate(calls):
            if not isinstance(c, dict):
                raise EnvelopeError(f"calls[{i}] must be an object")
            ctool = c.get("tool")
            cargs = c.get("args", {})
            if not isinstance(ctool, str) or not ctool:
                raise EnvelopeError(f"calls[{i}].tool must be a non-empty string")
            if not isinstance(cargs, dict):
                raise EnvelopeError(f"calls[{i}].args must be an object")
            parsed_calls.append(
                ActionEnvelope(tool=ctool, args=cargs, thought=c.get("thought", ""))
            )
        return ActionsEnvelope(calls=parsed_calls, thought=obj.get("thought", ""))

    if t == "ask_user":
        _strict_fields(obj, _ASK_FIELDS, required={"type", "question"})
        q = obj["question"]
        if not isinstance(q, str) or not q.strip():
            raise EnvelopeError("'question' must be a non-empty string")
        opts = obj.get("options", [])
        if not isinstance(opts, list) or not all(isinstance(o, str) for o in opts):
            raise EnvelopeError("'options' must be a list of strings")
        return AskUserEnvelope(
            question=q,
            options=list(opts),
            free_form=bool(obj.get("free_form", True)),
            blocking=bool(obj.get("blocking", True)),
        )

    # final
    # The final envelope is tolerant: models sometimes attach the
    # structured payload as a sibling key (e.g. {"type":"final","validation":{…}})
    # instead of inside "answer". We absorb the common variations rather
    # than fail — the agent loop's one-shot retry can't always recover
    # from this and the run gets parked as pending for no good reason.
    if "answer" not in obj:
        # Try to synthesize an `answer` from any structured sibling.
        for sibling_key in ("validation", "result", "payload", "data"):
            if sibling_key in obj:
                obj = {**obj, "answer": json.dumps(obj[sibling_key])}
                break
        else:
            raise EnvelopeError("missing required fields: ['answer']")
    # Check required fields, but DO NOT reject unknown extras (model
    # commentary, the original sibling key we just absorbed, etc.).
    missing = {"type", "answer"} - set(obj)
    if missing:
        raise EnvelopeError(f"missing required fields: {sorted(missing)}")
    ans = obj["answer"]
    if not isinstance(ans, str):
        # Some models emit answer as an object directly — stringify it.
        ans = json.dumps(ans)
    return FinalEnvelope(answer=ans)


def _strict_fields(obj: dict, allowed: set[str], required: set[str]) -> None:
    extras = set(obj) - allowed
    if extras:
        raise EnvelopeError(f"unexpected fields: {sorted(extras)}")
    missing = required - set(obj)
    if missing:
        raise EnvelopeError(f"missing required fields: {sorted(missing)}")


_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_PY_LITERAL_RE = re.compile(r"\b(True|False|None)\b")
_PY_LITERALS = {"True": "true", "False": "false", "None": "null"}


def _repair_json(raw: str) -> str:
    """Best-effort repair of the most common, unambiguous LLM JSON mistakes:
    a trailing comma before a closing `}`/`]` (the usual cause of "Expecting
    property name enclosed in double quotes"), and Python literals used in place
    of JSON ones. Conservative: only touches text OUTSIDE of string literals, so
    apostrophes / the words inside strings are never rewritten."""
    # Rebuild the text, applying the regex repairs only to runs OUTSIDE string
    # literals (tracked by a tiny scanner that respects `\"` escapes).
    result: list[str] = []
    buf: list[str] = []
    in_str = False
    esc = False

    def _flush_outside() -> None:
        seg = "".join(buf)
        seg = _TRAILING_COMMA_RE.sub(r"\1", seg)
        seg = _PY_LITERAL_RE.sub(lambda m: _PY_LITERALS[m.group(1)], seg)
        result.append(seg)
        buf.clear()

    for ch in raw:
        if in_str:
            result.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            _flush_outside()
            result.append(ch)
            in_str = True
        else:
            buf.append(ch)
    _flush_outside()
    return "".join(result)


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> str:
    """Pull the first JSON object out of `text`, allowing for a code fence."""
    text = text.strip()
    if not text:
        raise EnvelopeError("empty response")
    # exact JSON object
    if text.startswith("{") and text.endswith("}"):
        return text
    # fenced
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1)
    # first top-level { ... }
    start = text.find("{")
    if start == -1:
        raise EnvelopeError("no JSON object found")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise EnvelopeError("unterminated JSON object")


RETRY_REMINDER = (
    "Your previous response was not a valid envelope. Reply with EXACTLY ONE JSON object and"
    " NOTHING ELSE — no prose before or after, no markdown fence, no comments, no trailing"
    " comma. It must be one of these shapes:\n"
    '{"type": "action", "tool": "<tool name>", "args": {<json args>}, "thought": "<why>"}\n'
    '{"type": "actions", "thought": "<why>", "calls": [{"tool": "<name>", "args": {<json>}}, ...]}\n'
    '{"type": "ask_user", "question": "<question>"}\n'
    '{"type": "final", "answer": "<answer>"}\n'
    "Output the JSON object now, starting with the character '{'."
)
