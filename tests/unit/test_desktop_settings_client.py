"""Native HTTP projections, connection custody and exact draft recovery."""

from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.desktop_settings_client import (
    MAX_RESPONSE,
    NativeConnection,
    NativeSettingsClient,
    NativeSettingsError,
    load_native_clients,
    project_document,
)
from potbot.desktop_settings_state import PreferenceDraft, RevocationJournal

FIXTURE = Path(__file__).resolve().parents[2] / "frontend/tests/fixtures/settings-http.json"


@pytest.fixture
def wire():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def connection(wire, **overrides):
    return NativeConnection(
        workspace_id=wire["catalogue"]["workspaceId"],
        name="Native fixture",
        base="http://127.0.0.1:8765",
        token="fixture-access-" * 4,
        principal_id=wire["catalogue"]["principalId"],
        **overrides,
    )


@pytest.mark.parametrize("resource", ["catalogue", "effective", "models", "tools", "swarm"])
def test_native_projection_accepts_existing_http_contract_and_discards_private_fields(
    wire, resource
):
    raw = wire[resource] | {"privateCredential": "PRIVATE", "absoluteRoot": "/PRIVATE"}
    result = project_document(resource, raw, wire["catalogue"]["workspaceId"])
    assert "PRIVATE" not in json.dumps(result)
    with pytest.raises(NativeSettingsError):
        project_document(resource, raw, str(uuid4()))
    with pytest.raises(NativeSettingsError):
        project_document(resource, raw, wire["catalogue"]["workspaceId"], str(uuid4()))


def test_native_inventory_never_promotes_files_or_exposes_paths(wire):
    for patch in (
        {"runtime": "healthy"},
        {"qualification": "qualified"},
        {"permission": "granted"},
        {"presence": "not_observed"},
        {"location": "C:/private"},
        {"location": "../private"},
        {"origin": "package_metadata"},
    ):
        raw = copy.deepcopy(wire["models"])
        raw["items"][0].update(patch)
        with pytest.raises(NativeSettingsError):
            project_document("models", raw, raw["workspaceId"])
    raw = copy.deepcopy(wire["effective"])
    next(row for row in raw["defaults"] if row["id"] == "configuration.secret_refs")["value"] = (
        "secret://PRIVATE"
    )
    with pytest.raises(NativeSettingsError):
        project_document("effective", raw, raw["workspaceId"])


def test_native_reads_omit_setup_and_mutations_require_exact_receipts(wire):
    calls, setup_calls = [], []

    def setup():
        setup_calls.append(True)
        return "fixture-setup-" * 4

    def transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        if url.endswith("catalogue"):
            return 200, json.dumps(wire["catalogue"]).encode()
        if url.endswith("preferences"):
            value = {"revision": 2, "settings": {"theme": "light"}}
            return 200, json.dumps(value).encode()
        value = json.loads(body)
        return 202, json.dumps(
            {**wire["catalogue"], "requestId": value["requestId"], "state": "requested"}
        ).encode()

    client = NativeSettingsClient(connection(wire, setup=setup), transport)
    client.read("catalogue")
    client.save_preferences(wire["catalogue"]["principalId"], 1, {"theme": "light"})
    assert setup_calls == []
    assert all("X-Potbot-Setup-Token" not in call[2] for call in calls)
    assert all("Origin" not in call[2] for call in calls if call[0] == "GET")
    grant = wire["swarm"]["grants"][0]
    body = {
        "requestId": str(uuid4()),
        "grantId": grant["id"],
        "expectedRevision": grant["revision"],
        "expectedGeneration": grant["revocationGeneration"],
        "grantSha256": grant["sha256"],
        "confirm": True,
    }
    receipt = client.request_revocation(wire["catalogue"]["principalId"], body)
    assert receipt["requestId"] == body["requestId"] and setup_calls == [True]
    assert calls[-1][0] == "POST" and calls[-1][1].endswith("/v1/ui/swarm/revocations")
    assert json.loads(calls[-1][3]) == body
    assert calls[-1][2]["X-Potbot-Setup-Token"] == "fixture-setup-" * 4
    count = len(calls)
    with pytest.raises(NativeSettingsError):
        client.request_revocation(wire["catalogue"]["principalId"], body | {"argv": ["sh"]})
    assert len(calls) == count
    assert "fixture-access" not in repr(client.connection)


def test_wrong_scope_precedes_preferences_read_or_write(wire):
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url))
        return 200, json.dumps(wire["catalogue"] | {"workspaceId": str(uuid4())}).encode()

    client = NativeSettingsClient(connection(wire), transport)
    for operation in (
        lambda: client.read("preferences"),
        lambda: client.save_preferences(wire["catalogue"]["principalId"], 1, {}),
    ):
        with pytest.raises(NativeSettingsError):
            operation()
    assert all(method == "GET" and url.endswith("catalogue") for method, url in calls)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8765",
        "http://remote.invalid:8765",
        "http://127.0.0.1:8765/private",
        "http://user:password@127.0.0.1:8765",
        "http://127.0.0.1:8765/?url=private",
    ],
)
def test_native_connection_cannot_become_an_arbitrary_credential_proxy(wire, url):
    with pytest.raises(NativeSettingsError):
        NativeConnection(wire["catalogue"]["workspaceId"], "fixture", url, "safe-token-" * 4)


def test_native_allowlist_is_bounded_and_uses_separate_workspace_credentials(
    tmp_path, monkeypatch, wire
):
    from potbot import service

    monkeypatch.setattr(service, "config_path", lambda: tmp_path / "missing.json")
    other = str(uuid4())
    entries = [
        {
            "id": wire["catalogue"]["workspaceId"],
            "name": "Home",
            "url": "http://127.0.0.1:8765",
            "token": "first-scoped-token-" * 3,
        },
        {
            "id": other,
            "name": "Other",
            "url": "http://127.0.0.1:8767",
            "token": "second-scoped-token-" * 3,
        },
    ]
    clients = load_native_clients({"POTBOT_UI_WORKSPACES_JSON": json.dumps(entries)})
    assert [item.connection.name for item in clients] == ["Home", "Other"]
    assert clients[0].connection.token != clients[1].connection.token
    assert clients[1].connection.workspace_id == other
    with pytest.raises(NativeSettingsError):
        load_native_clients({"POTBOT_UI_WORKSPACES_JSON": json.dumps(entries + [entries[0]])})
    with pytest.raises(NativeSettingsError):
        load_native_clients(
            {
                "POTBOT_UI_WORKSPACES_JSON": json.dumps(
                    [entries[0] | {"url": "http://private.invalid:1"}]
                )
            }
        )


def test_native_uses_the_service_selected_vault_profile_and_never_reads_admin_dsn(
    tmp_path, monkeypatch, wire
):
    from dataclasses import asdict

    from potbot import credentials, service

    config = service.ServiceConfig(
        wire["catalogue"]["workspaceId"],
        wire["catalogue"]["principalId"],
        str(tmp_path / "policy.json"),
        credential_profile="default",
    )
    path = tmp_path / "service.json"
    path.write_text(json.dumps(asdict(config)), encoding="utf-8")
    monkeypatch.setattr(service, "config_path", lambda: path)
    reads = []

    class Store:
        def __init__(self, profile):
            assert profile == config.credential_profile

        def get(self, key):
            reads.append(key)
            return "fixture-native-key-" * 3

    def forbidden(*args, **kwargs):
        raise AssertionError("Native settings requested an administrator DSN")

    monkeypatch.setattr(credentials, "CredentialStore", Store)
    monkeypatch.setattr(credentials, "resolve_postgres_dsn", forbidden)
    clients = load_native_clients({})
    assert clients[0].connection.principal_id == config.principal_id
    assert reads == ["api.access_token"]


def test_native_http_transport_refuses_redirects_oversize_and_private_errors(wire):
    mode, paths = ["normal"], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            paths.append(self.path)
            if mode[0] == "redirect":
                self.send_response(302)
                self.send_header("Location", "/PRIVATE")
                self.end_headers()
                return
            status = 403 if mode[0] == "error" else 200
            body = (
                b"PRIVATE diagnostics"
                if status == 403
                else b"x" * (MAX_RESPONSE + 1)
                if mode[0] == "oversize"
                else b'{"duplicate":1,"duplicate":2}'
                if mode[0] == "duplicate"
                else json.dumps(wire["catalogue"]).encode()
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        client = NativeSettingsClient(
            NativeConnection(
                wire["catalogue"]["workspaceId"],
                "HTTP fixture",
                f"http://127.0.0.1:{server.server_port}",
                "fixture-http-token-" * 3,
            )
        )
        assert client.read("catalogue")["workspaceId"] == wire["catalogue"]["workspaceId"]
        for selection in ("redirect", "oversize", "error", "duplicate"):
            mode[0] = selection
            with pytest.raises(NativeSettingsError) as error:
                client.read("catalogue")
            assert "PRIVATE" not in str(error.value)
        assert "/PRIVATE" not in paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


def test_native_preferences_rebase_only_the_edited_fields():
    state = PreferenceDraft({"revision": 1, "settings": {"theme": "system", "graphDisplay": "3d"}})
    state.edit("theme", "dark")
    state.observe({"revision": 2, "settings": {"theme": "light", "graphDisplay": "list"}})
    assert state.draft["theme"] == "dark" and state.base["revision"] == 1
    assert state.rebase() == ["theme"]
    assert state.base["revision"] == 2 and state.draft == {"theme": "dark", "graphDisplay": "list"}
    state.accept({"revision": 3, "settings": dict(state.draft)})
    assert state.changed == []


def test_native_revocation_journal_survives_restart_and_separates_scopes(tmp_path, wire):
    workspace, principal = wire["catalogue"]["workspaceId"], wire["catalogue"]["principalId"]
    grant = wire["swarm"]["grants"][0]
    directory = tmp_path / "private" / "revocations"
    first = RevocationJournal(directory).prepare(workspace, principal, grant, [])
    assert first == RevocationJournal(directory).prepare(workspace, principal, grant, [])
    assert (
        RevocationJournal(directory).prepare(str(uuid4()), principal, grant, [])["requestId"]
        != first["requestId"]
    )
    assert (
        RevocationJournal(directory).prepare(
            workspace, principal, grant | {"revision": grant["revision"] + 1}, []
        )["requestId"]
        != first["requestId"]
    )
    raw = "".join(path.read_text() for path in directory.glob("*.json"))
    assert "token" not in raw and "secret" not in raw


def test_native_journal_follows_no_redirected_directory(tmp_path, wire):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "redirect").symlink_to(outside, target_is_directory=True)
    with pytest.raises(NativeSettingsError):
        RevocationJournal(tmp_path / "redirect" / "revocations").prepare(
            wire["catalogue"]["workspaceId"],
            wire["catalogue"]["principalId"],
            wire["swarm"]["grants"][0],
            [],
        )
    assert list(outside.iterdir()) == []
