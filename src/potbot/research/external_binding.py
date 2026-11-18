"""Exact native consumer and operator authorization for external research."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Any

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash

RUN_SCHEMA = "potbot-external-research-run/v1"
BATCH_SCHEMA = "potbot-external-research-batch/v1"
AUTHORIZATION_SCHEMA = "potbot-external-research-authorization/v1"
REQUEST_SCHEMA = "potbot-external-research-request/v1"
RESPONSE_SCHEMA = "potbot-external-research-response/v1"
RECEIPT_SCHEMA = "potbot-external-research-receipt/v1"


def research_consumer_sha256() -> str:
    root = Path(__file__).resolve().parents[1]
    paths = (
        "research/external_binding.py",
        "research/external_contracts.py",
        "research/contracts.py",
        "research/source_policy.py",
        "research/providers.py",
        "research/specialist.py",
        "research/specialist_records.py",
        "research/extraction.py",
        "research/pdf_extraction.py",
        "research/external_transport.py",
        "research/external_cog.py",
        "controller/research_external.py",
        "controller/research_external_worker.py",
        "controller/research_worker.py",
        "controller/research_store.py",
        "controller/budgets.py",
        "controller/execution_events.py",
        "domain/execution_events.py",
        "domain/research_evidence.py",
        "controller/research_evidence.py",
        "controller/research_workbench.py",
        "cogs/contracts.py",
        "tools/supervisor.py",
        "tools/isolation.py",
        "tools/pinned_https.py",
        "tools/connect_policy.py",
        "tools/secrets.py",
        "credentials.py",
        "context/service.py",
        "persistence/artifacts.py",
        "persistence/evidence.py",
        "workspace_sandbox.py",
    )
    return content_hash(
        [
            {"path": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest()}
            for path in paths
        ]
    )


def authorization_mac(value: dict[str, Any], token: str) -> str:
    if not isinstance(token, str) or not token:
        raise ScopeDeniedError("Native research setup authority is unavailable")
    return hmac.new(
        token.encode("utf-8"),
        (AUTHORIZATION_SCHEMA + "\n" + canonical_json(value)).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def require_setup(supplied: object, expected: str | None) -> str:
    if (
        not isinstance(supplied, str)
        or not supplied
        or not isinstance(expected, str)
        or not expected
    ):
        raise ScopeDeniedError("An authenticated native research setup token is required")
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise ScopeDeniedError("Native research setup authority does not match")
    return expected


def bounded_key(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(c) < 33 for c in value):
        raise InvalidInputError("Research commands require a bounded literal idempotency key")
    return value
