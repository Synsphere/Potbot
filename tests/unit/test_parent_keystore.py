from __future__ import annotations

import hashlib
import hmac
import json

from potbot.syn_release import parent_keystore_client as client
from potbot.syn_release import secret_service_proxy as proxy


def test_parent_keystore_request_is_scoped_hmac_and_replay_nonce() -> None:
    token = bytes.fromhex("11" * 32)
    session = "SynHostCustody-" + "a" * 32
    first = client.build_request_for(session, token, "syn-authenticator", {"op": "health"})
    second = client.build_request_for(session, token, "syn-authenticator", {"op": "health"})
    assert first["request_id"] != second["request_id"]
    unsigned = {key: first[key] for key in ("profile", "session", "request_id", "surface", "body")}
    expected = hmac.new(
        token,
        json.dumps(unsigned, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        ),
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(first["mac"], expected)
    assert first["surface"] == "syn-authenticator"


def test_secret_service_item_identity_is_deterministic_and_parent_scoped() -> None:
    path = proxy._item_path("DanceFlow.Potbot", "default:postgres.application")
    assert path.startswith(proxy.COLLECTION_PATH + "/")
    assert path == proxy._item_path("DanceFlow.Potbot", "default:postgres.application")
    assert proxy.DEFAULT_ALIAS_PATH == "/org/freedesktop/secrets/aliases/default"
