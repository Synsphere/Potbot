"""A listening API/GUI must not hide failure of the mandatory Xpra runtime."""

from __future__ import annotations

import io
import json
import os
import time
from types import SimpleNamespace

import pytest

from potbot import desktop_state, oneclick
from potbot.domain.errors import CapabilityUnavailableError
from potbot.instance import InstanceLease


@pytest.fixture
def running(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(oneclick, "project_root", lambda: tmp_path)
    monkeypatch.setattr(oneclick, "_probe", lambda *args, **kwargs: True)
    monkeypatch.setattr(oneclick, "CredentialStore", lambda _: SimpleNamespace(get=lambda _: "token"))
    config = SimpleNamespace(
        frontend_enabled=True, workspace_id="this-workspace", host="127.0.0.1", port=8765,
    )

    def response(*args, **kwargs):
        result = io.BytesIO(b'{"workspace":{"id":"this-workspace"}}')
        result.status = 200
        return result

    monkeypatch.setattr(oneclick.urllib.request, "urlopen", response)
    status = tmp_path / ".potbot" / "service" / "xpra-status.json"
    status.parent.mkdir(parents=True)

    def publish(stage, **extra):
        document = dict(workspace=config.workspace_id, stage=stage, ready=stage == "ready",
                        updated=time.time())
        document.update(extra)
        status.write_text(json.dumps(document))

    with InstanceLease("service", root=tmp_path) as lease:
        yield config, lease, status, publish


@pytest.mark.parametrize("stage", ["starting", "runtime-failed", "restart-limit", "ready"])
def test_live_backend_and_gui_require_xpra_ready(running, stage):
    config, _, _, publish = running
    publish(stage)
    assert oneclick._healthy(config) is (stage == "ready")


@pytest.mark.parametrize("extra", [{"workspace": "another-workspace"}, {"updated": 0}, {"ready": False}])
def test_rejects_foreign_stale_or_unready_status(running, extra):
    config, _, _, publish = running
    publish("ready", **extra)
    assert not oneclick._healthy(config)


@pytest.mark.parametrize("stage", ["ready", "restart-limit"])
def test_previous_generation_readiness_is_rejected(running, stage):
    config, lease, status, publish = running
    publish(stage)
    earlier = (lease.home / "service.json").stat().st_mtime_ns - 1_000_000
    os.utime(status, ns=(earlier, earlier))
    assert not oneclick._healthy(config)
    assert oneclick._xpra_readiness(config) == (False, "")


@pytest.mark.parametrize("foreign", [False, True])
def test_ready_file_requires_a_live_owner_from_this_checkout(monkeypatch, running, foreign):
    config, lease, _, publish = running
    publish("ready")
    owner = lease.root / "other-checkout" if foreign else None
    monkeypatch.setattr(oneclick, "owner_root", lambda *args, **kwargs: owner)
    assert not oneclick._healthy(config)


def test_rejects_supervisor_generation_change_during_probe(monkeypatch, running):
    config, lease, _, publish = running
    publish("ready")
    original = desktop_state._xpra_status

    def replace_owner(*args):
        result = original(*args)
        receipt = lease.home / "service.json"
        value = json.loads(receipt.read_text())
        value["generation"] = "f" * 32
        receipt.write_text(json.dumps(value))
        return result

    monkeypatch.setattr(desktop_state, "_xpra_status", replace_owner)
    assert not oneclick._healthy(config)


@pytest.mark.parametrize("stage", [
    "custody-tcp-failed", "custody-auth-failed", "custody-firewall-failed",
    "custody-firewall-declined", "restart-limit", "startup-timeout",
    "database-firewall-declined", "database-firewall-failed", "database-failed",
])
def test_terminal_runtime_failure_aborts_wait_immediately(running, stage):
    config, lease, _, publish = running
    publish(stage)
    waiter = SimpleNamespace(
        root=lease.root, check_stop=lambda: None,
        stopped=SimpleNamespace(wait=lambda _: pytest.fail("Startup retried a terminal failure")),
    )
    with pytest.raises(CapabilityUnavailableError, match="Potbot Xpra startup failed"):
        oneclick._wait_for_existing_service(config, waiter, open_browser=False)


@pytest.mark.parametrize("stage", ["runtime-failed", "host-bridge-unreachable"])
def test_retryable_runtime_stages_do_not_abort_startup(running, stage):
    config, _, _, publish = running
    publish(stage)
    oneclick._raise_xpra_failure(config)
