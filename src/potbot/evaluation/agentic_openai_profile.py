"""An explicit versioned, low-cost OpenAI benchmark configuration, not routing.

Only the declared revision marker opts in. Existing hosted configurations and
their frozen plan hashes keep their previous provider defaults.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from potbot.domain.errors import InvalidInputError

if TYPE_CHECKING:
    from potbot.evaluation.agentic_model_bench import ModelTarget

PREFIX = "openai-cheap-v1:"


def profile_for_target(target: ModelTarget) -> dict[str, Any] | None:
    if not target.revision.startswith(PREFIX):
        return None
    efforts = {
        "gpt-5-nano-2025-08-07": "minimal",
        "gpt-6-luna": "none",
        "gpt-4o-mini-2024-07-18": None,
        "gpt-5.6-luna": "none",
    }
    if (
        target.backend != "hosted"
        or target.provider != "openai"
        or target.model not in efforts
        or target.revision != PREFIX + target.model
    ):
        raise InvalidInputError("Unknown versioned OpenAI benchmark configuration")
    return {
        "schema": "potbot.openai-cheap-benchmark/1",
        "model": target.model,
        "protocol": "responses",
        "reasoning_effort": efforts[target.model],
        "temperature": 0 if target.model.startswith("gpt-4o-mini-") else None,
        "store": False,
        "tools": [],
        "automatic_retry": False,
        "complete_token_limit_is_measured": True,
        "stop_after_unresolved_transport": True,
    }
