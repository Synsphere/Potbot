"""Admission bounds and signed cosine do not require a downloaded model."""

import math
from uuid import uuid4

import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError
from potbot.memory.embeddings import EmbeddingService, _cosine_micros, _transient_inputs


@pytest.mark.parametrize(
    ("query", "candidates"),
    [
        ("", (("a" * 64, "document"),)),
        (" " * 500, (("a" * 64, "document"),)),
        ("q" * 501, (("a" * 64, "document"),)),
        ("\ud800", (("a" * 64, "document"),)),
        ("query", ()),
        ("query", tuple((f"{i:064x}", "document") for i in range(17))),
        ("query", [("a" * 64, "document")]),
        ("query", (("a" * 64, "document", "unknown"),)),
        ("query", (("A" * 64, "document"),)),
        ("query", (("a" * 63, "document"),)),
        ("query", (("a" * 64, "first"), ("a" * 64, "second"))),
        ("query", (("a" * 64, "  "),)),
        ("query", (("a" * 64, "d" * 4097),)),
        ("query", (("a" * 64, "\ud800"),)),
        ("q", tuple((f"{i:064x}", "d" * 4096) for i in range(16))),
        ("q", tuple((f"{i:064x}", "😀" * 4096) for i in range(4))),
    ],
)
def test_untrusted_transient_inputs_are_bounded_before_admission(query, candidates) -> None:
    with pytest.raises(InvalidInputError):
        _transient_inputs(query, candidates)


def test_exact_utf8_limit_and_input_identity_are_preserved() -> None:
    documents = tuple((f"{i:064x}", "d" * (4095 if i == 0 else 4096)) for i in reversed(range(16)))
    ordered = _transient_inputs("q", documents)
    assert ordered == tuple(sorted(documents))
    assert sum(len(text.encode()) for _, text in ordered) + 1 == 65_536
    assert _transient_inputs(" q ", (("a" * 64, " exact \n bytes "),)) == (
        ("a" * 64, " exact \n bytes "),
    )


def test_cosine_is_signed_normalized_and_handles_allowed_norm_drift() -> None:
    query = (1.00001, *([0.0] * 1023))
    positive = (0.99999, *([0.0] * 1023))
    negative = (-1.0, *([0.0] * 1023))
    orthogonal = (0.0, 1.0, *([0.0] * 1022))
    diagonal = (math.sqrt(0.5), math.sqrt(0.5), *([0.0] * 1022))
    assert _cosine_micros(query, positive) == 1_000_000
    assert _cosine_micros(query, negative) == -1_000_000
    assert _cosine_micros(query, orthogonal) == 0
    assert _cosine_micros(query, diagonal) == 707_107


@pytest.mark.parametrize("vector", [(0.0,) * 1024, (float("nan"),) + (0.0,) * 1023])
def test_cosine_refuses_invalid_provider_vectors(vector) -> None:
    with pytest.raises(InvalidInputError):
        _cosine_micros((1.0,) + (0.0,) * 1023, vector)


@pytest.mark.parametrize("binding", [1, True, "", "A" * 64, "a" * 63, "a" * 65, [], {}])
def test_invalid_request_binding_fails_without_database_or_provider(binding) -> None:
    service = EmbeddingService(None, None, None)
    with pytest.raises(InvalidInputError, match="request binding"):
        service.rank_transient(
            WorkspaceContext(uuid4(), uuid4()),
            uuid4(),
            uuid4(),
            "query",
            (("a" * 64, "document"),),
            request_binding_sha256=binding,
        )
