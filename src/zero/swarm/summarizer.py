"""Tool-less synthesis of peer contributions."""
from __future__ import annotations

import json

from zero.llm.backend import LLMBackend, Message, Session


class SwarmSummarizer:
    def __init__(self, backend: LLMBackend, *, session: Session | None = None) -> None:
        self.backend = backend
        self.session = session

    async def summarize(self, snapshot: dict) -> str:
        system = (
            "You consolidate a security-assessment swarm's immutable snapshot. "
            "Merge repetition, preserve uncertainty and missing input, and never change "
            "finding verdicts. Return only the concise user-facing response, not JSON."
        )
        messages = [Message(role="user", content=json.dumps(snapshot, ensure_ascii=False))]
        if self.session is None:
            response = await self.backend.run(system, messages, [])
        else:
            response = await self.backend.run(system, messages, [], session=self.session)
        return _user_text(response.text)


def _user_text(raw: str) -> str:
    """Unwrap accidental final envelopes without exposing protocol JSON to chat."""
    text = raw.strip()
    candidate = text
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        value, _ = json.JSONDecoder().raw_decode(candidate)
    except (json.JSONDecodeError, TypeError):
        return text
    if not isinstance(value, dict) or value.get("type") != "final":
        return text
    for key in ("answer", "content", "result", "text"):
        answer = value.get(key)
        if isinstance(answer, str) and answer.strip():
            return answer.strip()
    return text


def deterministic_summary(snapshot: dict) -> str:
    parts = ["Swarm assessment complete."]
    for item in snapshot.get("contributions", []):
        label = item.get("worker_id", "peer")
        status = item.get("status", "complete")
        body = item.get("body", "").strip()
        parts.append(f"\n{label} ({status}): {body or '(no contribution)'}")
    missing = snapshot.get("missing_workers") or []
    if missing:
        parts.append("\nMissing input: " + ", ".join(missing))
    return "\n".join(parts)
