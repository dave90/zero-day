"""Parse the three envelope shapes the agent is allowed to emit.

The agent must reply with exactly one of:

  {"type": "action",   "tool": "...", "args": {...}, "thought": "..."}
  {"type": "ask_user", "question": "...", "options": [...], "free_form": bool, "blocking": bool}
  {"type": "final",    "answer": "..."}

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
class AskUserEnvelope:
    question: str
    options: list[str]
    free_form: bool
    blocking: bool


@dataclass
class FinalEnvelope:
    answer: str


Envelope = ActionEnvelope | AskUserEnvelope | FinalEnvelope


_VALID_TYPES = {"action", "ask_user", "final"}
_ACTION_FIELDS = {"type", "tool", "args", "thought"}
_ASK_FIELDS = {"type", "question", "options", "free_form", "blocking"}
_FINAL_FIELDS = {"type", "answer"}


def parse_envelope(text: str) -> Envelope:
    """Parse a single envelope from `text`. Raises EnvelopeError on failure.

    Tolerant of TRAILING DATA after the first JSON object: models sometimes emit
    a valid envelope followed by extra content (a second object, stray prose, a
    concatenated payload). We decode the first JSON value and ignore the rest
    rather than failing the whole turn with a "Extra data" JSONDecodeError — that
    failure used to kill a subagent and lose its findings."""
    raw = _extract_json(text)
    try:
        obj, _end = json.JSONDecoder().raw_decode(raw)
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
    "Your previous response was not a valid envelope. Reply ONLY with one JSON object of"
    " type 'action', 'ask_user', or 'final'. No prose, no markdown fence."
)
