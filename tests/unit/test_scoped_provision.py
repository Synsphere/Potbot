import subprocess
import sys
from pathlib import Path

import pytest

from potbot.domain.errors import CapabilityUnavailableError
from potbot.tools import scoped_launcher
from potbot.tools import scoped_provision as provision


def test_wsl_prefixed_cgroup_membership_resolves_the_delegated_keeper() -> None:
    raw = (
        "0::/wsl-user/distro-280/system.slice/"
        "potbot-scoped-worker-cgroups.service/keeper\n"
    )
    assert provision._delegated_cgroup_from_membership(raw) == Path(
        "/sys/fs/cgroup/wsl-user/distro-280/system.slice/"
        "potbot-scoped-worker-cgroups.service"
    )


def test_runtime_units_use_discovered_cgroup_and_distinct_auditor_gid() -> None:
    cgroup = Path("/sys/fs/cgroup/wsl-user/distro-7/system.slice") / provision.KEEPER
    files = provision._runtime_units(
        Path("/root/venv/bin/python"),
        Path("/usr/local/libexec/potbot-cgroup-join"),
        Path("/usr/bin/strace"),
        "a" * 64,
        "b" * 64,
        1000,
        1000,
        998,
        997,
        cgroup,
    )
    broker = files[provision.UNIT_DIR / provision.BROKER]
    assert f"accept {cgroup}" in broker
    assert " 1000 1000 998 997\n" in broker
    assert f"POTBOT_OPERATOR_CGROUP_ROOT={cgroup}\n" in files[provision.ENV_FILE]
    credentials = scoped_launcher._credentials(
        998,
        997,
        "-all,+net_admin",
        supplementary_gids=(1000,),
    )
    assert "--reuid=998" in credentials and "--regid=997" in credentials
    assert "--groups=1000" in credentials
    assert "--clear-groups" not in credentials

    worker_credentials = scoped_launcher._credentials(1000, 1000)
    assert "--clear-groups" in worker_credentials
    assert not any(item.startswith("--groups=") for item in worker_credentials)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux systemd provisioning")
def test_failed_socket_start_rolls_back_fresh_keeper_and_launcher_files(tmp_path, monkeypatch):
    units = tmp_path / "units"
    units.mkdir()
    env = tmp_path / "configuration" / "monitor.env"
    env.parent.mkdir()
    audit = tmp_path / "state" / "audit"
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    monkeypatch.setattr(provision, "UNIT_DIR", units)
    monkeypatch.setattr(provision, "ENV_FILE", env)
    monkeypatch.setattr(provision, "AUDIT", audit)
    monkeypatch.setattr(provision, "DEFAULT_SOCKET", tmp_path / "launcher.sock")
    monkeypatch.setattr(provision, "_protected", lambda path: None)
    monkeypatch.setattr(provision, "_keeper_cgroup", lambda: cgroup)
    monkeypatch.setattr(
        provision,
        "_keeper_unit",
        lambda python, uid, gid: {units / provision.KEEPER: "keeper"},
    )
    monkeypatch.setattr(
        provision,
        "_runtime_units",
        lambda *args: {
            units / provision.SOCKET: "generated",
            env: "retained",
        },
    )

    def inspect(path, content):
        return "current" if path.exists() and path.read_text() == content else "missing"

    def audit_directory(uid, gid):
        audit.mkdir()
        return True

    operations = []

    def systemctl(*args, check=True):
        operations.append(args)
        if args[0] == "is-active":
            return subprocess.CompletedProcess(args, 1, "", "")
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 4, "not-found\n", "")
        if args[:2] == ("start", provision.SOCKET):
            assert (units / provision.KEEPER).is_file()
            assert (units / provision.SOCKET).is_file()
            raise CapabilityUnavailableError("cannot start")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(provision, "_inspect", inspect)
    monkeypatch.setattr(provision, "_audit_directory", audit_directory)
    monkeypatch.setattr(provision, "_manager", systemctl)
    env.write_text("retained")

    with pytest.raises(CapabilityUnavailableError, match="cannot start"):
        provision._apply(
            Path("/root/venv/bin/python"),
            Path("/usr/local/libexec/potbot-cgroup-join"),
            Path("/usr/bin/strace"),
            "a" * 64,
            "b" * 64,
            1000,
            1000,
            998,
            997,
        )

    assert env.read_text() == "retained"
    assert not (units / provision.KEEPER).exists()
    assert not (units / provision.SOCKET).exists()
    assert not audit.parent.exists()
    assert operations[-4:] == [
        ("stop", provision.SOCKET),
        ("disable", provision.SOCKET),
        ("stop", provision.KEEPER),
        ("daemon-reload",),
    ]


def test_legacy_potbot_keeper_is_upgradeable_but_foreign_unit_is_refused(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(provision, "UNIT_DIR", tmp_path)
    monkeypatch.setattr(provision, "_protected", lambda path: None)
    keeper = tmp_path / provision.KEEPER
    desired = provision._keeper_unit(Path("/root/venv/bin/python"), 1000, 1000)[keeper]
    legacy = desired.removeprefix(provision._MANAGED_HEADER)
    keeper.write_text(legacy, encoding="ascii")
    keeper.chmod(0o644)

    assert provision._inspect(keeper, desired) == "stale"

    keeper.write_text(
        legacy.replace(
            "Description=Potbot scoped worker accounting subtree",
            "Description=administrator-owned replacement",
        ),
        encoding="ascii",
    )
    with pytest.raises(CapabilityUnavailableError, match="existing configuration was kept"):
        provision._inspect(keeper, desired)


def test_stale_managed_file_is_atomically_replaced_and_restorable(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(provision, "UNIT_DIR", tmp_path)
    monkeypatch.setattr(provision, "_protected", lambda path: None)
    keeper = tmp_path / provision.KEEPER
    desired = provision._keeper_unit(Path("/root/venv/bin/python"), 1000, 1000)[keeper]
    legacy = desired.removeprefix(provision._MANAGED_HEADER)
    keeper.write_text(legacy, encoding="ascii")
    keeper.chmod(0o644)

    state = provision._inspect(keeper, desired)
    previous = provision._publish(keeper, desired, state)
    assert previous == legacy.encode("ascii")
    assert keeper.read_text(encoding="ascii") == desired

    provision._restore(keeper, previous)
    assert keeper.read_text(encoding="ascii") == legacy


def test_wsl_scope_detaches_only_known_infrastructure_9p_mounts(tmp_path, monkeypatch) -> None:
    mountinfo = tmp_path / "mountinfo"
    rows = [
        "1 0 0:1 / /usr/lib/wsl/drivers ro - 9p drivers ro",
        "2 0 0:2 / /usr/lib/wsl/lib ro - 9p lib ro",
        "3 0 0:3 / /init ro - 9p tools ro",
    ]
    mountinfo.write_text("\n".join(rows) + "\n", encoding="utf-8")
    removed = []

    def run(argv, **kwargs):
        removed.append(argv[-1])
        target = argv[-1]
        mountinfo.write_text(
            "\n".join(
                row
                for row in mountinfo.read_text().splitlines()
                if row.split()[4] != target
            )
            + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(scoped_launcher.subprocess, "run", run)
    scoped_launcher._detach_wsl_infrastructure_shares(mountinfo)
    assert removed == ["/usr/lib/wsl/drivers", "/usr/lib/wsl/lib", "/init"]


def test_wsl_scope_rejects_arbitrary_remaining_drvfs_mount(tmp_path, monkeypatch) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "1 0 0:1 /C:/ /mnt/c rw - 9p drvfs rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        scoped_launcher.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("must not unmount /mnt/c")
        ),
    )
    with pytest.raises(CapabilityUnavailableError, match="Windows filesystems"):
        scoped_launcher._detach_wsl_infrastructure_shares(mountinfo)
