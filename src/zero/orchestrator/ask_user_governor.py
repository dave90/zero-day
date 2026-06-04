"""Governs ask_user: per-agent cap + near-duplicate suppression + yolo policy.

- Max questions per agent per session (10 Pentest / 3 Validator by default).
- Near-duplicate detection by a normalized question hash — if the same
  question (or near-identical) was already asked in this session, the
  governor short-circuits and returns the prior answer.
- `--yolo` auto-answers non-blocking questions with an empty string and
  still defers blocking ones to the user.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from zero.llm.envelope import AskUserEnvelope


DEFAULT_LIMITS: dict[str, int] = {"pentest": 10, "validator": 3}


@dataclass
class _AgentState:
    count: int = 0
    seen: dict[str, str] = field(default_factory=dict)  # qhash → previous answer


@dataclass
class GovernorDecision:
    """One of three outcomes."""
    answer: str | None = None        # if set: skip the user, feed this back
    proceed: bool = False            # ask the user normally
    refusal: str | None = None       # don't ask; tell the agent why


class AskUserGovernor:
    def __init__(self, *, yolo: bool = False, limits: dict[str, int] | None = None) -> None:
        self.yolo = yolo
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self._state: dict[str, _AgentState] = {}

    def evaluate(self, agent: str, env: AskUserEnvelope) -> GovernorDecision:
        st = self._state.setdefault(agent, _AgentState())
        cap = self.limits.get(agent, 5)
        if st.count >= cap:
            return GovernorDecision(
                refusal=(
                    f"ask_user limit reached for agent={agent} ({cap}/session)."
                    " Try a tool call instead."
                )
            )
        qh = _question_hash(env.question)
        if qh in st.seen:
            return GovernorDecision(answer=st.seen[qh])
        if self.yolo and not env.blocking:
            st.count += 1
            st.seen[qh] = ""
            return GovernorDecision(answer="")
        return GovernorDecision(proceed=True)

    def record_answer(self, agent: str, env: AskUserEnvelope, answer: str) -> None:
        st = self._state.setdefault(agent, _AgentState())
        st.count += 1
        st.seen[_question_hash(env.question)] = answer


_STOPWORDS = {
    "the", "a", "an", "is", "are", "do", "does", "you", "your", "i", "to",
    "of", "for", "and", "or", "in", "on", "at", "by", "as", "be", "this",
    "that", "with", "from", "should", "would", "can", "could", "?", "!",
}


def _question_hash(q: str) -> str:
    """Cheap normalized hash: lowercase, drop punctuation, drop stopwords."""
    q = q.lower()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    toks = [t for t in q.split() if t and t not in _STOPWORDS]
    norm = " ".join(sorted(toks))  # word order not significant
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
