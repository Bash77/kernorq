"""
Conversational intent router — lightweight deterministic classifier.

Sits BEFORE autonomous execution so that wake/conversational utterances
never enter the planner/executor pipeline.

Design rules:
  - Small, deterministic pattern set (not a giant keyword system).
  - Conversational input NEVER creates an Execution.
  - Execution objectives pass through unchanged to the existing pipeline.
  - Responses are short, human, and spoken via the existing TTS path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Utterances that are clearly conversational/wake-style. Match on normalized
# text (lowercase, trimmed punctuation). Each entry is a whole-phrase pattern;
# anything that expresses an ACTION over project artifacts is NOT here and
# therefore falls through to the objective pipeline.
_WAKE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(hey|hi|hello|yo|ok|okay)\b[\s,!.?]*(kernorq|kernork|there)?\b[\s,!.?]*$", re.I),
    re.compile(r"\bwake up\b", re.I),
    re.compile(r"\b(are\s+you\s+(there|awake|listening|online|ready)|you\s+there)\b", re.I),
    re.compile(r"\bcan\s+you\s+hear\s+me\b", re.I),
    re.compile(r"\bis\s+(anyone|something)\s+(there|listening)\b", re.I),
    re.compile(r"\bgood\s+(morning|afternoon|evening)\b[\s,!.?]*$", re.I),
    re.compile(r"^\s*(test|testing)[\s,!.?]*(mic|microphone)?[\s,!.?]*$", re.I),
    re.compile(r"\b(kernorq|kernork)[\s?!.,]*\??$", re.I),
]

WAKE_RESPONSE = "I am awake and running. What task do you need help with?"


@dataclass
class IntentResult:
    mode: str  # "conversation" | "objective" | "chat"
    text: str  # original utterance
    response: str | None = None  # populated only for deterministic conversation


# Verbs that indicate work over project artifacts → autonomous execution.
_OBJECTIVE_HINTS = re.compile(
    r"\b(run|execute|inspect|analy[sz]e|check|test|build|deploy|fix|create|write|generate|"
    r"scan|audit|refactor|clean|install|update|compile|lint|review|report|find)\w*\b",
    re.I,
)


def classify_intent(text: str) -> IntentResult:
    """
    Deterministically classifies an utterance.

    conversation: greetings / wake phrases / presence checks → short spoken
                  response, no Execution created.
    objective:    expresses work over project artifacts → normal Gemini-planning
                  pipeline unchanged.
    chat:         ordinary questions ("what can you do?", "how are you?") →
                  dedicated conversational path; never enters the planner.

    The caller decides how to serve "chat" (Gemini Flash short answer or Live
    session); this classifier only guarantees it is NOT treated as execution.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return IntentResult(mode="chat", text=cleaned)

    for pattern in _WAKE_PATTERNS:
        if pattern.search(cleaned):
            return IntentResult(mode="conversation", text=cleaned, response=WAKE_RESPONSE)

    if _OBJECTIVE_HINTS.search(cleaned):
        return IntentResult(mode="objective", text=cleaned)

    return IntentResult(mode="chat", text=cleaned)


__all__ = ["IntentResult", "classify_intent", "WAKE_RESPONSE"]
