"""Apply permanent managed WSL legacy service migration from repository root."""
from pathlib import Path
import ast
import subprocess
import sys

SOURCE = Path('src/potbot/wsl_core_runtime.py')
TEST = Path('tests/unit/test_windows_core_runtime.py')
HELPER = '''def _retire_legacy_user_service(service: pwd.struct_passwd) -> None:
    """Retire the standalone API/worker unit in the owned WSL distribution only."""
    runtime = f"/run/user/{service.pw_uid}"
    command = [
        "/usr/sbin/runuser", "-u", service.pw_name, "--", "/usr/bin/env",
        f"XDG_RUNTIME_DIR={runtime}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus",
        "/usr/bin/systemctl", "--user",
    ]

    def query() -> dict[str, str]:
        result = subprocess.run(
            command + ["show", "potbot.service", "--property=LoadState,ActiveState,UnitFileState,FragmentPath,ExecStart"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if fields.get("LoadState") == "not-found":
            return fields
        if result.returncode or fields.get("LoadState") != "loaded":
            raise CapabilityUnavailableError("Cannot inspect legacy Potbot user service")
        return fields

    fields = query()
    if fields.get("LoadState") == "not-found":
        return
    if fields.get("ActiveState") == "inactive" and fields.get("UnitFileState") == "disabled":
        return
    expected = Path(service.pw_dir) / ".config/systemd/user/potbot.service"
    if (
        fields.get("FragmentPath") != str(expected)
        or "-m potbot.startup service run" not in fields.get("ExecStart", "")
    ):
        raise CapabilityUnavailableError("Unexpected Potbot user unit; refusing automatic migration")
    _run(command + ["disable", "--now", "potbot.service"], timeout=60)
    fields = query()
    if fields.get("ActiveState") != "inactive" or fields.get("UnitFileState") != "disabled":
        raise CapabilityUnavailableError("Legacy Potbot user service did not stop and disable")
    print("[Potbot WSL] Retired legacy API/worker user service; data retained", file=sys.stderr, flush=True)


'''
TESTS = '''

@pytest.mark.skipif(os.name != "posix", reason="Linux-only WSL runtime module")
@pytest.mark.parametrize("scenario", ["missing", "disabled", "legacy", "foreign", "failed-stop"])
def test_retire_legacy_user_service(monkeypatch, scenario):
    from types import SimpleNamespace
    from potbot import wsl_core_runtime as runtime

    service = SimpleNamespace(pw_uid=1000, pw_name="synoperator", pw_dir="/home/synoperator")
    fields = {
        "LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled",
        "FragmentPath": "/home/synoperator/.config/systemd/user/potbot.service",
        "ExecStart": "{ argv[]=/srv/potbot-debugger/venv/bin/python -m potbot.startup service run ; }",
    }
    if scenario == "missing":
        fields["LoadState"] = "not-found"
    elif scenario == "disabled":
        fields.update(ActiveState="inactive", UnitFileState="disabled")
    elif scenario == "foreign":
        fields["ExecStart"] = "/usr/bin/other"
    calls = []

    def show(command, **kwargs):
        assert command[-3:-1] == ["show", "potbot.service"]
        return SimpleNamespace(returncode=0, stdout="\\n".join(f"{k}={v}" for k, v in fields.items()))

    def disable(command, **kwargs):
        calls.append(command[-3:])
        if scenario != "failed-stop":
            fields.update(ActiveState="inactive", UnitFileState="disabled")

    monkeypatch.setattr(runtime.subprocess, "run", show)
    monkeypatch.setattr(runtime, "_run", disable)
    if scenario in {"foreign", "failed-stop"}:
        with pytest.raises(runtime.CapabilityUnavailableError):
            runtime._retire_legacy_user_service(service)
    else:
        runtime._retire_legacy_user_service(service)
    assert calls == ([ ["disable", "--now", "potbot.service"] ] if scenario in {"legacy", "failed-stop"} else [])
'''

def replace_once(s, old, new):
    if s.count(old) != 1:
        raise SystemExit('Expected source anchor missing or ambiguous: ' + old[:90])
    return s.replace(old, new, 1)

def main():
    originals = {p: p.read_bytes() for p in (SOURCE, TEST)}
    source = originals[SOURCE].decode('utf-8').replace('\r\n', '\n')
    test = originals[TEST].decode('utf-8').replace('\r\n', '\n')
    if '_retire_legacy_user_service' in source:
        raise SystemExit('Migration already present; no files changed.')
    source = replace_once(source, 'def _sha(path: Path) -> str:', HELPER + 'def _sha(path: Path) -> str:')
    source = replace_once(source, '    service, auditor = _ensure_user()\n    python =', '    service, auditor = _ensure_user()\n    _qualification_stage("legacy-service-migration")\n    _retire_legacy_user_service(service)\n    python =')
    test += TESTS
    for p, s in ((SOURCE, source), (TEST, test)):
        ast.parse(s, filename=str(p))
    for p, s in ((SOURCE, source), (TEST, test)):
        p.write_bytes(s.replace('\n', '\r\n' if b'\r\n' in originals[p] else '\n').encode('utf-8'))
    print('Patched managed WSL startup and regression tests. Review git diff and run tests.')

if __name__ == '__main__':
    main()
