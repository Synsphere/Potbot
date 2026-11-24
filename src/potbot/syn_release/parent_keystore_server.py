"""Windows parent-node keystore RPC server for the managed WSL runtime.

Executed with Syn's pinned host-custody Python environment. The server owns the
DPAPI vault and all authorization decisions. WSL receives only an authenticated
request/response channel; root/private material is never exported.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any, cast

# The host-custody project is intentionally `tool.uv.package = false`, so its
# verified source package is not installed into the venv.  Potbot launches this
# server with `python -I` to ignore ambient/user Python paths; explicitly restore
# only the server's own staged, hash-verified project root before importing Syn.
_SOURCE_ROOT = Path(__file__).resolve(strict=True).parent
if Path(__file__).is_symlink() or _SOURCE_ROOT.is_symlink():
    raise RuntimeError("Parent keystore source root is redirected")
sys.path.insert(0, str(_SOURCE_ROOT))

from syn_host_custody.broker import VaultInUseError, WindowsVault, host_approve  # noqa: E402
from syn_host_custody.core import Authority, root_slot, safe_ref, safe_slot  # noqa: E402
from syn_host_custody.owner import OwnerProcess  # noqa: E402

REQUEST_PROFILE = "potbot.parent-keystore-request/1"
RESPONSE_PROFILE = "potbot.parent-keystore-response/1"
MAX_MESSAGE = 160_000
_SESSION = re.compile(r"SynHostCustody-[a-f0-9]{32}\Z")
_HEX64 = re.compile(r"[a-f0-9]{64}\Z")
_AUTH_OPS = {
    "health",
    "create",
    "identity",
    "sign",
    "candidates",
    "device_commitment",
    "group_key",
    "peer_record",
    "dag_anchor",
    "dag_witness_anchor",
}
_SECRET_OPS = {"get", "set", "delete", "search"}


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def mac(token: bytes, value: dict[str, Any]) -> str:
    return hmac.new(token, canonical(value), hashlib.sha256).hexdigest()


def _secret_identity(body: dict[str, Any]) -> tuple[str, str | None]:
    service = body.get("service")
    slot = body.get("slot")
    if not isinstance(service, str) or not safe_ref(service) or body.get("account") != "slot":
        raise ValueError("Parent secret request has invalid service/account scope")
    if slot is not None and (not isinstance(slot, str) or not safe_slot(slot) or root_slot(slot)):
        raise ValueError("Parent secret request targets a protected slot")
    return service, slot


def _delete(vault: WindowsVault, service: str, slot: str) -> bool:
    key = service + "/" + slot
    previous = vault.data
    slots = previous["slots"]
    if key not in slots:
        return False
    vault.data = {
        **previous,
        "slots": {name: value for name, value in slots.items() if name != key},
    }
    try:
        vault._persist()
    except BaseException:
        vault.data = previous
        vault._write_failed = True
        raise
    return True


def _search(vault: WindowsVault, service: str, slot: str | None) -> list[str]:
    if slot is not None:
        return [] if vault.get_slot(service, slot) is None else [slot]
    prefix = service + "/"
    result = []
    for key in vault.data["slots"]:
        if not key.startswith(prefix):
            continue
        candidate = key[len(prefix) :]
        if safe_slot(candidate) and not root_slot(candidate):
            result.append(candidate)
    return sorted(result)


def dispatch(authority: Authority, vault: WindowsVault, surface: str, body: dict[str, Any]) -> Any:
    if not isinstance(body, dict):
        raise ValueError("Parent keystore body must be an object")
    operation = body.get("op")
    if surface == "syn-authenticator":
        if operation not in _AUTH_OPS:
            raise ValueError("Parent authenticator operation is not permitted")
        # Syn's signed Authority performs all pin checks, transcript-domain
        # checks, native approval prompts and non-exportability enforcement.
        return authority.request(body)
    if surface != "secret-service" or operation not in _SECRET_OPS:
        raise ValueError("Parent keystore request surface is not permitted")
    service, slot = _secret_identity(body)
    if operation in {"get", "set"}:
        # Keep generic secret storage behind the parent Authority as well.
        return authority.request(body)
    if operation == "delete":
        assert slot is not None
        return _delete(vault, service, slot)
    return _search(vault, service, slot)


def _read_request(channel: socket.socket) -> bytes:
    data = bytearray()
    while b"\n" not in data:
        chunk = channel.recv(min(4096, MAX_MESSAGE + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_MESSAGE:
            raise ValueError("Parent keystore request exceeds size limit")
    if b"\n" not in data:
        raise ValueError("Parent keystore request is incomplete")
    line, trailing = bytes(data).split(b"\n", 1)
    if trailing.strip():
        raise ValueError("Parent keystore request has trailing data")
    return line


def _response(token: bytes, request_id: str, response: dict[str, Any]) -> bytes:
    unsigned = {
        "profile": RESPONSE_PROFILE,
        "request_id": request_id,
        "response": response,
    }
    return canonical({**unsigned, "mac": mac(token, unsigned)}) + b"\n"


def _open_vault() -> WindowsVault:
    deadline = time.monotonic() + 10.0
    while True:
        try:
            return WindowsVault()
        except VaultInUseError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def serve(
    host: str,
    guest: str,
    port: int,
    session: str,
    token: bytes,
    owner_pid: int,
    owner_created: int,
) -> None:
    replay: set[str] = set()
    with OwnerProcess(owner_pid, owner_created), _open_vault() as vault:
        authority = Authority(vault, approve=host_approve)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, cast(Any, socket).SO_EXCLUSIVEADDRUSE, 1)
            server.bind((host, port))
            server.listen(8)
            while True:
                channel, peer = server.accept()
                with channel:
                    channel.settimeout(330)
                    request_id = ""
                    try:
                        if peer[0] != guest:
                            raise ValueError("Parent keystore rejected a non-WSL peer")
                        raw = _read_request(channel)
                        envelope = json.loads(raw)
                        if not isinstance(envelope, dict) or set(envelope) != {
                            "profile",
                            "session",
                            "request_id",
                            "surface",
                            "body",
                            "mac",
                        }:
                            raise ValueError("Parent keystore envelope has unknown fields")
                        request_id = envelope["request_id"]
                        unsigned = {
                            name: envelope[name]
                            for name in ("profile", "session", "request_id", "surface", "body")
                        }
                        if (
                            envelope["profile"] != REQUEST_PROFILE
                            or envelope["session"] != session
                            or not isinstance(request_id, str)
                            or _HEX64.fullmatch(request_id) is None
                            or request_id in replay
                            or not isinstance(envelope["mac"], str)
                            or not hmac.compare_digest(envelope["mac"], mac(token, unsigned))
                        ):
                            raise ValueError("Parent keystore authentication failed")
                        replay.add(request_id)
                        if len(replay) > 8192:
                            # Session request IDs are random 256-bit values; a bounded
                            # replay window is enough without growing forever.
                            replay.pop()
                        result = dispatch(authority, vault, envelope["surface"], envelope["body"])
                        response = {"ok": True, "result": result}
                    except (KeyError, TypeError, ValueError, UnicodeError, OSError) as error:
                        response = {"ok": False, "error": str(error)[:200]}
                    if _HEX64.fullmatch(request_id or "") is None:
                        request_id = "0" * 64
                    channel.sendall(_response(token, request_id, response))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--guest", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--owner-created", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        socket.inet_aton(args.host)
        socket.inet_aton(args.guest)
        token_text = os.environ.get("POTBOT_PARENT_KEYSTORE_SERVER_TOKEN", "")
        token = bytes.fromhex(token_text)
    except (OSError, ValueError):
        parser.error("invalid parent keystore network/session argument")
    if (
        not 1024 <= args.port <= 65535
        or _SESSION.fullmatch(args.session) is None
        or _HEX64.fullmatch(token_text) is None
        or len(token) != 32
    ):
        parser.error("invalid parent keystore network/session argument")
    try:
        serve(
            args.host,
            args.guest,
            args.port,
            args.session,
            token,
            args.owner_pid,
            args.owner_created,
        )
    except VaultInUseError:
        print(
            "Parent keystore vault is still owned by another Syn authority",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
