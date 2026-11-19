"""Authenticated WSL client for the parent node's Windows-backed keystore API.

This module is deliberately stdlib-only.  The same client is used by the Syn
host-custody compatibility executable and by the Secret Service façade.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sys
from typing import Any

REQUEST_PROFILE = "potbot.parent-keystore-request/1"
RESPONSE_PROFILE = "potbot.parent-keystore-response/1"
MAX_MESSAGE = 160_000
_ENV = {
    "host": "POTBOT_PARENT_KEYSTORE_HOST",
    "port": "POTBOT_PARENT_KEYSTORE_PORT",
    "session": "POTBOT_PARENT_KEYSTORE_SESSION",
    "token": "POTBOT_PARENT_KEYSTORE_TOKEN",
}
_SESSION = re.compile(r"SynHostCustody-[a-f0-9]{32}\Z")
_HEX64 = re.compile(r"[a-f0-9]{64}\Z")


class ParentKeystoreError(RuntimeError):
    """The parent keystore transport or authorization boundary refused a request."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _configuration() -> tuple[str, int, str, bytes]:
    host = os.environ.get(_ENV["host"], "")
    port_text = os.environ.get(_ENV["port"], "")
    session = os.environ.get(_ENV["session"], "")
    token_text = os.environ.get(_ENV["token"], "")
    try:
        port = int(port_text)
        token = bytes.fromhex(token_text)
    except ValueError:
        raise ParentKeystoreError("Parent keystore session configuration is invalid") from None
    try:
        socket.inet_aton(host)
    except OSError:
        raise ParentKeystoreError("Parent keystore host address is invalid") from None
    if (
        not 1 <= port <= 65535
        or _SESSION.fullmatch(session) is None
        or _HEX64.fullmatch(token_text) is None
        or len(token) != 32
    ):
        raise ParentKeystoreError("Parent keystore session configuration is invalid")
    return host, port, session, token


def _mac(token: bytes, value: dict[str, Any]) -> str:
    return hmac.new(token, _canonical(value), hashlib.sha256).hexdigest()


def build_request_for(
    session: str, token: bytes, surface: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Create one replay-resistant, HMAC-authenticated request envelope."""
    if _SESSION.fullmatch(session) is None or len(token) != 32:
        raise ParentKeystoreError("Parent keystore session configuration is invalid")
    if surface not in {"secret-service", "syn-authenticator"} or not isinstance(body, dict):
        raise ParentKeystoreError("Parent keystore request surface is invalid")
    unsigned: dict[str, Any] = {
        "profile": REQUEST_PROFILE,
        "session": session,
        "request_id": secrets.token_hex(32),
        "surface": surface,
        "body": body,
    }
    envelope = {**unsigned, "mac": _mac(token, unsigned)}
    raw = _canonical(envelope) + b"\n"
    if len(raw) > MAX_MESSAGE:
        raise ParentKeystoreError("Parent keystore request exceeds size limit")
    return envelope


def build_request(surface: str, body: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    _host, _port, session, token = _configuration()
    return build_request_for(session, token, surface, body), token


def request_with_config(
    host: str,
    port: int,
    session: str,
    token: bytes,
    body: dict[str, Any],
    *,
    surface: str,
    timeout: float = 30.0,
) -> Any:
    envelope = build_request_for(session, token, surface, body)
    request_id = envelope["request_id"]
    try:
        with socket.create_connection((host, port), timeout=timeout) as channel:
            channel.settimeout(timeout)
            channel.sendall(_canonical(envelope) + b"\n")
            stream = channel.makefile("rb", buffering=0)
            raw = stream.readline(MAX_MESSAGE + 1)
    except (OSError, TimeoutError) as error:
        raise ParentKeystoreError("Parent keystore transport is unavailable") from error
    if not raw or len(raw) > MAX_MESSAGE or not raw.endswith(b"\n"):
        raise ParentKeystoreError("Parent keystore response is invalid")
    try:
        response = json.loads(raw)
    except (ValueError, TypeError):
        raise ParentKeystoreError("Parent keystore response is malformed") from None
    if not isinstance(response, dict) or set(response) != {
        "profile",
        "request_id",
        "response",
        "mac",
    }:
        raise ParentKeystoreError("Parent keystore response has unknown fields")
    unsigned = {
        "profile": response["profile"],
        "request_id": response["request_id"],
        "response": response["response"],
    }
    if (
        response["profile"] != RESPONSE_PROFILE
        or response["request_id"] != request_id
        or not isinstance(response["mac"], str)
        or not hmac.compare_digest(response["mac"], _mac(token, unsigned))
    ):
        raise ParentKeystoreError("Parent keystore response authentication failed")
    result = response["response"]
    if not isinstance(result, dict) or set(result) not in ({"ok", "result"}, {"ok", "error"}):
        raise ParentKeystoreError("Parent keystore result is invalid")
    if result["ok"] is not True:
        raise ParentKeystoreError(
            "Parent keystore denied request: " + str(result.get("error", "unknown"))[:200]
        )
    return result["result"]


def request(body: dict[str, Any], *, surface: str, timeout: float = 30.0) -> Any:
    host, port, session, token = _configuration()
    return request_with_config(
        host, port, session, token, body, surface=surface, timeout=timeout
    )


def secret_get(service: str, account: str) -> str | None:
    result = request(
        {"op": "get", "service": service, "account": "slot", "slot": account},
        surface="secret-service",
    )
    if result is not None and not isinstance(result, str):
        raise ParentKeystoreError("Parent secret result must be text or null")
    return result


def secret_set(service: str, account: str, value: str) -> None:
    result = request(
        {
            "op": "set",
            "service": service,
            "account": "slot",
            "slot": account,
            "value": value,
        },
        surface="secret-service",
    )
    if result is not True:
        raise ParentKeystoreError("Parent secret write was not acknowledged")


def secret_delete(service: str, account: str) -> bool:
    result = request(
        {"op": "delete", "service": service, "account": "slot", "slot": account},
        surface="secret-service",
    )
    if type(result) is not bool:
        raise ParentKeystoreError("Parent secret deletion response is invalid")
    return result


def secret_search(service: str, account: str | None = None) -> list[str]:
    body: dict[str, Any] = {"op": "search", "service": service, "account": "slot"}
    if account is not None:
        body["slot"] = account
    result = request(body, surface="secret-service")
    if not isinstance(result, list) or any(not isinstance(item, str) for item in result):
        raise ParentKeystoreError("Parent secret search response is invalid")
    return result


def _legacy_surface(body: dict[str, Any]) -> str:
    return "secret-service" if body.get("op") in {"get", "set"} else "syn-authenticator"


def _legacy_failure(label: str) -> int:
    print("SYN_HOST_CUSTODY_FAILURE:" + label, file=sys.stderr)
    return 4


def _legacy_main() -> int:
    # Syn 0.11 invokes this compatibility executable with PowerShell-shaped
    # arguments. They are deliberately ignored except that the retained pipe
    #/session identifier must agree with the authenticated parent session.
    try:
        if "-Pipe" in sys.argv:
            index = sys.argv.index("-Pipe")
            supplied = sys.argv[index + 1]
            if supplied != os.environ.get("POTBOT_PARENT_KEYSTORE_SESSION"):
                return _legacy_failure("input-invalid")
        raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
        if not raw or len(raw) > MAX_MESSAGE:
            return _legacy_failure("input-invalid")
        body = json.loads(raw)
        if not isinstance(body, dict):
            return _legacy_failure("input-invalid")
        result = request(body, surface=_legacy_surface(body), timeout=310.0)
        sys.stdout.write(
            json.dumps({"ok": True, "result": result}, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
        return 0
    except ParentKeystoreError:
        return _legacy_failure("connect-failed")
    except (ValueError, TypeError, IndexError):
        return _legacy_failure("input-invalid")


def main() -> int:
    if "--secret-service-daemon" in sys.argv[1:]:
        from potbot.syn_release.secret_service_proxy import main as secret_service_main

        return secret_service_main()
    return _legacy_main()


if __name__ == "__main__":
    raise SystemExit(main())
