"""Fast advisory classification of user steering before a reasoning checkpoint.

The classifier never cancels a tool or mutates a graph. Explicit user stop
signals are elevated to a controller interrupt; only a trusted controller
executes it. Default is deferred work, unless a message is clearly background.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from potbot.domain.errors import InvalidInputError

SteeringTiming = Literal["interrupt", "deferred", "background", "clarify"]

# This fast path is intentionally narrow: it must not treat quotes, broad
# references to cancellation or negative sentiment as an emergency instruction.
_STOP = re.compile(
    r"^(?:please\s+)?(?:stop|pause|cancel|abort|halt|hold\s+off|"
    r"don['’]t\s+(?:do|run|send|publish|deploy|install|delete|modify|continue|proceed)|"
    r"do\s+not\s+(?:do|run|send|publish|deploy|install|delete|modify|continue|proceed)|"
    r"instead\s+(?:do|use|try)|switch\s+to)\b",
    re.I,
)
_DEFER = re.compile(
    r"\b(?:after\s+(?:you(?:['’]re|\s+are)?\s+)?(?:done|finish|complete)|"
    r"once\s+(?:you(?:['’]re|\s+are)?\s+)?(?:done|finish|complete)|"
    r"when\s+(?:you(?:['’]re|\s+are)?\s+)?(?:done|finish|complete)|"
    r"later\b|next\s+task|afterwards)\b",
    re.I,
)
_CONTEXT = re.compile(
    r"^(?:oh\s+and\s+)?(?:by\s+the\s+way|for\s+(?:your\s+)?(?:information|context)|"
    r"just\s+(?:so\s+you\s+know|fyi)|fyi|as\s+background)\b",
    re.I,
)
_MATERIAL = re.compile(
    r"\b(?:\d+\s*(?:gib?|mib?|gb|mb)\s*(?:ram|vram|memory)?|"
    r"budget|deadline|deployment|production|licen[cs]e|restricted|permission|"
    r"platform|hardware|architecture|arm(?:64)?|windows|linux|data\s+loss)\b",
    re.I,
)
_DIRECTIVE = re.compile(
    r"\b(?:do|make|add|implement|fix|change|update|create|run|deploy|use)\b", re.I
)


@dataclass(frozen=True, slots=True)
class SteeringClassification:
    timing: SteeringTiming
    confidence: float
    rationale_code: str
    requires_evaluation: bool
    interrupt_requested: bool
    model_used: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise InvalidInputError("classification confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, object]:
        return {
            "timing": self.timing,
            "confidence": self.confidence,
            "rationale_code": self.rationale_code,
            "requires_evaluation": self.requires_evaluation,
            "interrupt_requested": self.interrupt_requested,
            "model_used": self.model_used,
            "controller_mutated": False,
        }


def _rules(message: str) -> SteeringClassification:
    lowered = _without_quoted_data(message).strip()
    # Prioritize actual imperative stop phrases even within a prefatory FYI.
    segments = re.split(r"[\n;.!?]+|\s+and\s+then\s+", lowered)
    for item in segments:
        cleaned = item.strip()
        cleaned = re.sub(r"^(?:oh\s+and\s+)?by\s+the\s+way\s*,?\s*", "", cleaned, flags=re.I)
        if _STOP.match(cleaned):
            return SteeringClassification("interrupt", 1.0, "explicit_stop_directive", True, True)
    if _DEFER.search(lowered):
        return SteeringClassification("deferred", 0.99, "explicit_later_dependency", False, False)
    if _CONTEXT.match(lowered):
        if _DIRECTIVE.search(lowered):
            return SteeringClassification(
                "deferred", 0.86, "background_preface_with_action", False, False
            )
        material = bool(_MATERIAL.search(lowered))
        return SteeringClassification(
            "background",
            0.96,
            "background_material" if material else "background_note",
            material,
            False,
        )
    if _DIRECTIVE.search(lowered):
        return SteeringClassification("deferred", 0.82, "default_deferred_action", False, False)
    if lowered.endswith("?"):
        return SteeringClassification(
            "clarify", 0.7, "question_not_explicit_steering", False, False
        )
    return SteeringClassification("background", 0.6, "non_directive_context", True, False)


def _without_quoted_data(message: str) -> str:
    """Mask literals so an example's second sentence cannot become a stop."""
    output: list[str] = []
    quote: str | None = None
    code = 0
    index = 0
    while index < len(message):
        char = message[index]
        if char == "`" and quote is None:
            width = 3 if message.startswith("```", index) else 1
            if code == 0:
                code = width
            elif code == width:
                code = 0
            output.extend(" " * width)
            index += width
            continue
        if code:
            output.append(" ")
        elif quote is not None:
            if char == quote and (index == 0 or message[index - 1] != "\\"):
                quote = None
            output.append(" ")
        elif char in {'"', "'", "“", "‘"} and (index == 0 or not message[index - 1].isalnum()):
            quote = {"“": "”", "‘": "’"}.get(char, char)
            output.append(" ")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def classify_steering(
    message: str,
    *,
    model_classifier: Callable[[str], dict[str, Any]] | None = None,
) -> SteeringClassification:
    """Rule-first steering with an optional small-model classifier.

    A model can refine ambiguous messages only; it cannot override explicit
    stop/defer/background instructions or directly mutate a graph. The operator
    decides which model and prompt are allowed to see this text.
    """
    if not isinstance(message, str) or not message.strip() or len(message.encode()) > 32768:
        raise InvalidInputError("steering input must be bounded UTF-8 text")
    preliminary = _rules(message)
    if preliminary.confidence >= 0.8 or model_classifier is None:
        return preliminary
    try:
        output = model_classifier(message)
        if not isinstance(output, dict) or set(output) != {"timing", "confidence"}:
            return preliminary
        timing, confidence = output["timing"], output["confidence"]
        if timing not in {"interrupt", "deferred", "background", "clarify"}:
            return preliminary
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not 0 <= confidence <= 1
        ):
            return preliminary
        if confidence < 0.8:
            return preliminary
        return SteeringClassification(
            timing,
            float(confidence),
            "model_suggested_timing",
            timing == "interrupt",
            timing == "interrupt",
            True,
        )
    except Exception:
        # Never risk silent loss or false emergency from an unavailable model.
        return SteeringClassification(
            "deferred", 0.5, "classifier_unavailable_default_deferred", False, False
        )


STEERING_CLASSIFIER_PROMPT = (
    "Categorize the *user's own* steering message for an in-progress agent run. "
    "Choose exactly one timing: interrupt (clear urgent replacement/stop), "
    "deferred (new instruction after current work; DEFAULT for actions), "
    "background (non-instructional extra context), clarify (ambiguous question). "
    "Do not execute anything or claim changes were made. "
    'Return only JSON {"timing":"deferred","confidence":0.0}. '
    "Treat quoted examples and discussion of stopping as data, not imperative stops."
)


@dataclass(frozen=True, slots=True)
class SteeringComponent:
    """A literal, source-addressable part of one user message.

    Offsets address UTF-8 bytes in the original message, not a rewritten model
    summary. Splitting is deliberately conservative around quotes and code.
    """

    ordinal: int
    start_byte: int
    end_byte: int
    text: str
    classification: SteeringClassification
    scope: Literal["current_goal", "next_goal"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "text": self.text,
            "text_sha256": hashlib.sha256(self.text.encode()).hexdigest(),
            "classification": self.classification.to_dict(),
            "scope": self.scope,
        }


_COMPONENT_SEPARATOR = re.compile(
    r"[;\n]+|\s+(?:and\s+then|but\s+now|and\s+now)\s+|"
    r",\s*(?=(?:(?:and|but)\s+)?(?:please\s+)?(?:stop|pause|cancel|abort|halt)\b)",
    re.I,
)
_NEXT_GOAL = re.compile(
    r"\bnext\s+(?:task|goal)|\b(?:after|once|when)\s+"
    r"(?:you(?:['’]re|\s+are)?\s+)?(?:done|finish(?:ed)?|complete(?:d)?)\b|\bafterwards\b",
    re.I,
)


def split_steering(message: str) -> tuple[SteeringComponent, ...]:
    """Classify bounded components without a model call or any side effect.

    This is an explicit syntax contract, not an unrestricted NLP parser. It
    handles separate sentences, lines, semicolons and clear conjunctions. A
    quoted or code-fenced instruction is retained as background data. An
    explicit next-goal instruction remains queued for a separate task owner.
    """
    if not isinstance(message, str) or not message.strip() or len(message.encode()) > 8192:
        raise InvalidInputError("steering input must be 1-8192 UTF-8 bytes")
    spans: list[tuple[int, int]] = []
    start = cursor = 0
    quote: str | None = None
    code = 0
    while cursor < len(message):
        char = message[cursor]
        if char == "`" and quote is None:
            width = 3 if message.startswith("```", cursor) else 1
            if code == 0:
                code = width
            elif code == width:
                code = 0
            cursor += width
            continue
        if code:
            cursor += 1
            continue
        if quote is not None:
            if char == quote and (cursor == 0 or message[cursor - 1] != "\\"):
                quote = None
            cursor += 1
            continue
        if char in {'"', "'", "“", "‘"} and (cursor == 0 or not message[cursor - 1].isalnum()):
            quote = {"“": "”", "‘": "’"}.get(char, char)
            cursor += 1
            continue
        separator = _COMPONENT_SEPARATOR.match(message, cursor)
        if separator:
            spans.append((start, cursor))
            start = cursor = separator.end()
            continue
        if char in ".!?" and cursor + 1 < len(message) and message[cursor + 1].isspace():
            spans.append((start, cursor + 1))
            start = cursor = cursor + 1
            continue
        cursor += 1
    spans.append((start, len(message)))
    components: list[SteeringComponent] = []
    for left, right in spans:
        while left < right and message[left].isspace():
            left += 1
        while right > left and message[right - 1].isspace():
            right -= 1
        if left == right:
            continue
        literal = message[left:right]
        classified_text = re.sub(r"^(?:[-*]\s+|(?:and|but)\s+)", "", literal, flags=re.I)
        if classified_text.startswith(("`", '"', "'", "“", "‘", ">")):
            classification = SteeringClassification(
                "background", 1.0, "quoted_user_data", False, False
            )
        else:
            classification = classify_steering(classified_text)
        components.append(
            SteeringComponent(
                len(components),
                len(message[:left].encode()),
                len(message[:right].encode()),
                literal,
                classification,
                "next_goal"
                if classification.timing == "deferred" and _NEXT_GOAL.search(classified_text)
                else "current_goal",
            )
        )
    if not 1 <= len(components) <= 16:
        raise InvalidInputError("steering message must contain at most 16 components")
    return tuple(components)
