"""Mandatory Potbot operator/Xpra runtime inside the owned WSL distribution."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import UUID

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger

IMAGE = "ghcr.io/synsphere/potbot-sandbox"
IMAGE_SIGNER = (
    "https://github.com/Synsphere/Potbot/"
    ".github/workflows/sandbox-system-image.yml@refs/heads/main"
)
COSIGN = Path("/usr/local/libexec/potbot-cosign")
SCOPED_SOCKET = Path("/run/potbot-scoped-worker.sock")
MONITOR_ENV = Path("/etc/potbot-scoped-worker/monitor.env")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_MONITOR_KEYS = {
    "POTBOT_OPERATOR_AUDIT_ROOT",
    "POTBOT_OPERATOR_CGROUP_ROOT",
    "POTBOT_OPERATOR_STRACE",
    "POTBOT_OPERATOR_STRACE_SHA256",
    "POTBOT_OPERATOR_CGROUP_JOIN",
    "POTBOT_OPERATOR_CGROUP_JOIN_SHA256",
}


def _run(argv: list[str], *, timeout: int = 300, cwd: Path | None = None,
         env: dict[str, str] | None = None, capture: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv, cwd=cwd, env=env, check=False, timeout=timeout, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if result.returncode:
        raise CapabilityUnavailableError("Required WSL core-runtime command failed")
    return result


def _protected_root(path: Path) -> Path:
    selected = path.resolve(strict=True)
    if selected != path or path.is_symlink() or not path.is_dir():
        raise InvalidInputError("Core source root must be canonical")
    for item in (path, *path.parents):
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise InvalidInputError("Core source root must be root-owned and immutable to users")
        if item == Path("/var/lib"):
            break
    return selected


def _ensure_user() -> tuple[pwd.struct_passwd, pwd.struct_passwd]:
    service = pwd.getpwnam("synoperator")
    try:
        auditor = pwd.getpwnam("potbot-auditor")
    except KeyError:
        _run(
            ["/usr/sbin/useradd", "--system", "--user-group", "--no-create-home",
             "--shell", "/usr/sbin/nologin", "potbot-auditor"],
            timeout=30,
        )
        auditor = pwd.getpwnam("potbot-auditor")
    if min(service.pw_uid, service.pw_gid, auditor.pw_uid, auditor.pw_gid) <= 0:
        raise CapabilityUnavailableError("Core runtime requires unprivileged service identities")
    if service.pw_uid == auditor.pw_uid or service.pw_gid == auditor.pw_gid:
        raise CapabilityUnavailableError("Core runtime service and auditor identities must differ")
    return service, auditor


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _ensure_scoped_launcher(source: Path, python: Path, service: pwd.struct_passwd,
                            auditor: pwd.struct_passwd) -> None:
    helper_source = source / "native/potbot-cgroup-join/cgroup_join.c"
    if not helper_source.is_file():
        raise CapabilityUnavailableError("Scoped launcher native source is missing")
    target = Path("/usr/local/libexec/potbot-cgroup-join")
    with tempfile.TemporaryDirectory(prefix="potbot-join-", dir="/var/tmp") as directory:
        built = Path(directory) / "potbot-cgroup-join"
        _run(
            ["/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
             "-fstack-protector-strong", "-D_FORTIFY_SOURCE=2",
             "-o", str(built), str(helper_source)],
            timeout=60,
        )
        payload = built.read_bytes()
        if not target.exists() or target.read_bytes() != payload:
            temporary = target.with_name(".potbot-cgroup-join.pending")
            temporary.unlink(missing_ok=True)
            temporary.write_bytes(payload)
            os.chown(temporary, 0, 0)
            os.chmod(temporary, 0o755)
            os.replace(temporary, target)
    tracer = Path("/usr/bin/strace").resolve(strict=True)
    from potbot.tools import scoped_provision
    code = scoped_provision.main([
        "install",
        "--service-user", service.pw_name,
        "--auditor-user", auditor.pw_name,
        "--join", str(target),
        "--join-sha256", _sha(target),
        "--strace", str(tracer),
        "--strace-sha256", _sha(tracer),
    ])
    if code:
        raise CapabilityUnavailableError("Scoped worker launcher could not be provisioned")


def _resolve_image_digest(image_source: str) -> str:
    if _COMMIT.fullmatch(image_source) is None:
        raise InvalidInputError("Sandbox image source commit is invalid")
    tag = f"{IMAGE}:system-v1-{image_source}-amd64"
    for attempt in range(120):
        try:
            result = subprocess.run(
                ["/usr/bin/skopeo", "inspect", "docker://" + tag],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env={"PATH": "/usr/sbin:/usr/bin", "LANG": "C"},
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0:
            try:
                document = json.loads(result.stdout)
                digest = document["Digest"]
            except (KeyError, TypeError, ValueError):
                raise CapabilityUnavailableError(
                    "Published sandbox image tag returned invalid registry metadata"
                ) from None
            if (
                isinstance(digest, str)
                and digest.startswith("sha256:")
                and _HEX.fullmatch(digest.removeprefix("sha256:")) is not None
            ):
                return digest.removeprefix("sha256:")
            raise CapabilityUnavailableError(
                "Published sandbox image tag returned an invalid digest"
            )
        error = "" if result is None else (result.stderr or "")
        lowered = error.lower()
        if "unauthorized" in lowered or "denied" in lowered:
            raise CapabilityUnavailableError(
                "Synsphere sandbox image is not publicly readable from GHCR"
            )
        if attempt % 6 == 0:
            print(
                f"[Potbot WSL] waiting for signed sandbox image {image_source[:12]}",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(10)
    raise CapabilityUnavailableError(
        "Signed sandbox image for the selected source commit was not published"
    )


def _verify_image(source: Path, source_sha: str, image_source: str) -> tuple[Path, dict]:
    image_digest = _resolve_image_digest(image_source)
    ref = f"{IMAGE}@sha256:{image_digest}"
    _run([
        str(COSIGN), "verify",
        "--certificate-identity", IMAGE_SIGNER,
        "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com",
        ref,
    ], timeout=180)
    inspected = json.loads(
        _run(
            ["/usr/bin/skopeo", "inspect", "--config", "docker://" + ref],
            timeout=180,
        ).stdout
    )
    if (
        inspected.get("os") != "linux"
        or inspected.get("architecture") != "amd64"
        or inspected.get("config", {}).get("Labels", {}).get("org.opencontainers.image.revision")
        != image_source
    ):
        raise CapabilityUnavailableError("Published sandbox image identity differs")

    image_root = Path("/var/lib/potbot/core-images") / image_digest
    rootfs = image_root / "bundle/rootfs"
    if not rootfs.is_dir():
        if image_root.exists() or image_root.is_symlink():
            raise CapabilityUnavailableError("Incomplete sandbox image needs reconciliation")
        image_root.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        pending = image_root.parent / (".pending-" + os.urandom(8).hex())
        pending.mkdir(mode=0o700)
        try:
            _run(
                [
                    "/usr/bin/skopeo",
                    "copy",
                    "--format",
                    "oci",
                    "docker://" + ref,
                    f"oci:{pending}/oci:candidate",
                ],
                timeout=600,
            )
            _run(
                [
                    "/usr/bin/umoci",
                    "unpack",
                    "--image",
                    f"{pending}/oci:candidate",
                    str(pending / "bundle"),
                ],
                timeout=600,
            )
            if not (pending / "bundle/rootfs").is_dir():
                raise CapabilityUnavailableError("Sandbox image unpack produced no rootfs")
            os.rename(pending, image_root)
        except BaseException:
            shutil.rmtree(pending, ignore_errors=True)
            raise

    kernel = os.uname().release
    key = hashlib.sha256(
        (
            source_sha
            + "\0"
            + kernel
            + "\0"
            + image_digest
            + "\0"
            + image_source
        ).encode()
    ).hexdigest()
    qroot = Path("/var/lib/potbot/core-qualification") / key
    report = qroot / "summary.json"
    if not report.is_file():
        if qroot.exists():
            raise CapabilityUnavailableError("Incomplete core qualification needs reconciliation")
        qroot.mkdir(mode=0o700, parents=True)
        command = [
            "/usr/bin/setpriv",
            "--regid",
            "69",
            "--clear-groups",
            str(source / ".venv/bin/python"),
            "-B",
            "scripts/qualify_published_sandbox_image.py",
            "--rootfs",
            str(rootfs),
            "--scratch",
            str(qroot),
            "--image-digest",
            image_digest,
            "--image-source",
            image_source,
            "--consumer",
            os.environ["POTBOT_CORE_CONSUMER_COMMIT"],
            "--report",
            str(report),
        ]
        try:
            execution = subprocess.run(
                command,
                timeout=900,
                cwd=source,
                env={**os.environ, "PYTHONPATH": str(source)},
                check=False,
            )
        except subprocess.TimeoutExpired:
            shutil.rmtree(qroot, ignore_errors=True)
            raise CapabilityUnavailableError(
                "Published sandbox image qualification timed out"
            ) from None
        if execution.returncode:
            summary: dict[str, object] = {"stage": "unreported"}
            if report.is_file():
                try:
                    failed_report = json.loads(report.read_text(encoding="utf-8"))
                    candidate = failed_report.get("failure_summary")
                    if (
                        isinstance(candidate, dict)
                        and isinstance(candidate.get("stage"), str)
                    ):
                        summary = candidate
                except (OSError, ValueError, TypeError):
                    pass
            print(
                "POTBOT_SANDBOX_FAILURE="
                + json.dumps(summary, sort_keys=True, separators=(",", ":")),
                file=sys.stderr,
                flush=True,
            )
            stage = str(summary.get("stage", "unreported"))
            shutil.rmtree(qroot, ignore_errors=True)
            raise CapabilityUnavailableError(
                f"Published sandbox image qualification failed during {stage}"
            )

    document = json.loads(report.read_text(encoding="utf-8"))
    workspace = dict(document.get("workspace_smoke") or {})
    qualification = workspace.pop("receipt_sha256", None)
    calculated = hashlib.sha256(
        json.dumps(workspace, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        document.get("schema") != "potbot.published-image-hosted-smoke/1"
        or document.get("qualified_smoke") is not True
        or document.get("image_digest") != image_digest
        or document.get("image_source_commit") != image_source
        or document.get("consumer_commit") != os.environ["POTBOT_CORE_CONSUMER_COMMIT"]
        or document.get("kernel_release") != kernel
        or workspace.get("qualified_smoke") is not True
        or workspace.get("image_digest") != image_digest
        or not isinstance(qualification, str)
        or calculated != qualification
    ):
        raise CapabilityUnavailableError("Published sandbox image did not qualify this WSL host")
    return rootfs, {
        "digest": image_digest,
        "source": image_source,
        "qualification": qualification,
        "report": str(report),
    }


def _ensure_directory(path: Path, uid: int, gid: int, mode: int) -> None:
    if path.is_symlink():
        raise CapabilityUnavailableError("Core runtime directory is redirected")
    path.mkdir(parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise CapabilityUnavailableError("Core runtime path is not a directory")
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def _write_private(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise CapabilityUnavailableError("Core runtime control file is redirected")
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    temporary = path.with_name("." + path.name + ".pending")
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as writer:
            writer.write(data)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _finalize(args: argparse.Namespace) -> int:
    if os.geteuid() == 0:
        raise CapabilityUnavailableError("Final core runtime ownership must be unprivileged")
    workspace, principal = UUID(args.workspace_id), UUID(args.principal_id)
    home = Path(args.home).resolve(strict=True)
    private = Path(args.private).resolve(strict=True)
    rootfs = Path(args.rootfs).resolve(strict=True)
    qualification = args.qualification_sha256
    image_digest = args.image_digest
    image_source = args.image_source
    if (
        _HEX.fullmatch(qualification) is None
        or _HEX.fullmatch(image_digest) is None
        or _COMMIT.fullmatch(image_source) is None
    ):
        raise InvalidInputError("Image or qualification identity is invalid")

    paths = {
        "home": home,
        "model_staging": Path(args.model_staging).resolve(strict=True),
        "shared_models": Path(args.shared_models).resolve(strict=True),
        "shared_tools": Path(args.shared_tools).resolve(strict=True),
        "ai_config_dir": Path(args.ai_config).resolve(strict=True),
        "ai_history_dir": Path(args.ai_past).resolve(strict=True),
        "controller_ledger": private / "leases.sqlite",
    }
    ledger = WorkspaceLeaseLedger(paths["controller_ledger"], workspace_home=home)
    pin = ledger.image_status(workspace)
    if pin is None:
        ledger.pin_image(
            workspace,
            digest=image_digest,
            image_ref=f"{IMAGE}@sha256:{image_digest}",
            build_commit=image_source,
            platform="linux/amd64",
            rootfs_path=rootfs,
            qualification_sha256=qualification,
        )
    else:
        if (
            pin["digest"] != image_digest
            or pin["qualification_sha256"] != qualification
            or pin["rootfs_path"] != str(rootfs)
        ):
            raise CapabilityUnavailableError("Existing workspace image pin differs")

    manifest = private / "operator.json"
    _write_private(manifest, {
        "workspace_id": str(workspace),
        "image_digest": image_digest,
        "qualification_sha256": qualification,
        "rootfs": str(rootfs),
        **{name: str(value) for name, value in paths.items()},
    })
    policy = private / "xpra-apps.json"
    _write_private(policy, {
        "workspaceId": str(workspace),
        "principalId": str(principal),
        "ledger": str(paths["controller_ledger"]),
        "runtime": {
            "imageDigest": image_digest,
            "qualificationSha256": qualification,
            "rootfs": str(rootfs),
            "home": str(home),
            "modelStaging": str(paths["model_staging"]),
            "sharedModels": str(paths["shared_models"]),
            "sharedTools": str(paths["shared_tools"]),
            "aiConfig": str(paths["ai_config_dir"]),
            "aiPast": str(paths["ai_history_dir"]),
        },
        "browserLeases": True,
        "strictXpraInput": True,
        "recipes": [
            {"kind": "terminal", "title": "Bash (xterm)",
             "argv": ["/usr/bin/xterm", "-fa", "Monospace", "-e", "/bin/bash"]},
            {"kind": "controlled-browser", "title": "Controlled Chromium",
             "argv": ["/opt/potbot/browser/bin/python",
                      "/opt/potbot/browser_worker.py", "--engine", "playwright"]},
        ],
    })
    state = private / "core-state.json"
    _write_private(state, {
        "workspaceId": str(workspace),
        "principalId": str(principal),
        "manifest": str(manifest),
        "policy": str(policy),
        "home": str(home),
        "private": str(private),
        "python": str(Path(sys.executable).resolve()),
        "imageDigest": image_digest,
        "imageSource": image_source,
        "qualificationSha256": qualification,
    })
    print(json.dumps({"state": str(state)}, sort_keys=True))
    return 0


def _qualification_stage(name: str) -> None:
    print(f"[Potbot WSL] qualification: {name}", file=sys.stderr, flush=True)
    print(f"POTBOT_QUALIFICATION_STAGE={name}", file=sys.stderr, flush=True)


def _prepare(args: argparse.Namespace) -> int:
    _qualification_stage("runtime-preflight")
    if not sys.platform.startswith("linux") or os.geteuid() != 0 or not sys.flags.isolated:
        raise CapabilityUnavailableError("Core runtime preparation requires isolated root Python in WSL")
    if (
        _HEX.fullmatch(args.source_sha256) is None
        or _COMMIT.fullmatch(args.consumer_commit) is None
        or _COMMIT.fullmatch(args.image_source) is None
    ):
        raise InvalidInputError("Core source identity is invalid")
    source = _protected_root(Path(args.source_root))
    os.environ["POTBOT_CORE_CONSUMER_COMMIT"] = args.consumer_commit

    _qualification_stage("service-identities")
    service, auditor = _ensure_user()
    python = source / ".venv/bin/python"
    if not python.is_file():
        raise CapabilityUnavailableError("Core runtime virtual environment is missing")

    _qualification_stage("scoped-launcher")
    _ensure_scoped_launcher(source, python, service, auditor)

    _qualification_stage("sandbox-image")
    rootfs, image = _verify_image(source, args.source_sha256, args.image_source)

    _qualification_stage("workspace-state")
    base = Path("/home") / service.pw_name / ".local/share/potbot/core" / args.workspace_id
    private = base / "private"
    home = base / "home"
    staging = base / "model-staging"
    models = base / "models"
    tools = base / "tools"
    ai = base / "ai"
    past = base / "ai-past"
    for path, mode in (
        (base, 0o700), (private, 0o700), (home, 0o700), (staging, 0o700),
        (models, 0o755), (tools, 0o2775), (ai, 0o700), (past, 0o700),
    ):
        _ensure_directory(path, service.pw_uid, service.pw_gid, mode)
    _ensure_directory(home / ".tmp", service.pw_uid, service.pw_gid, 0o700)

    _qualification_stage("finalize")
    command = [
        "/usr/sbin/runuser", "-u", service.pw_name, "--",
        str(python), "-I", "-B", "-m", "potbot.wsl_core_runtime", "finalize",
        "--workspace-id", args.workspace_id,
        "--principal-id", args.principal_id,
        "--rootfs", str(rootfs),
        "--image-digest", image["digest"],
        "--image-source", image["source"],
        "--qualification-sha256", image["qualification"],
        "--home", str(home),
        "--private", str(private),
        "--model-staging", str(staging),
        "--shared-models", str(models),
        "--shared-tools", str(tools),
        "--ai-config", str(ai),
        "--ai-past", str(past),
    ]
    result = _run(command, timeout=120)
    _qualification_stage("complete")
    print(result.stdout.strip())
    return 0


def _monitor_environment() -> dict[str, str]:
    if not MONITOR_ENV.is_file() or MONITOR_ENV.is_symlink():
        raise CapabilityUnavailableError("Scoped worker monitor environment is missing")
    result: dict[str, str] = {}
    for line in MONITOR_ENV.read_text(encoding="ascii").splitlines():
        if not line or "=" not in line:
            raise CapabilityUnavailableError("Scoped worker monitor environment is invalid")
        name, value = line.split("=", 1)
        if name not in _MONITOR_KEYS or name in result or not value:
            raise CapabilityUnavailableError("Scoped worker monitor environment is invalid")
        result[name] = value
    if set(result) != _MONITOR_KEYS:
        raise CapabilityUnavailableError("Scoped worker monitor environment is incomplete")
    return result


def _run_core(args: argparse.Namespace) -> int:
    if os.geteuid() == 0:
        raise CapabilityUnavailableError("Core operator/Xpra runtime must be unprivileged")
    state = Path(args.state)
    if (
        not state.is_absolute() or state.is_symlink() or not state.is_file()
        or state.resolve(strict=True) != state or state.stat().st_uid != os.geteuid()
        or stat.S_IMODE(state.stat().st_mode) & 0o077
    ):
        raise InvalidInputError("Core runtime state is not private")
    data = json.loads(state.read_text(encoding="utf-8"))
    workspace = UUID(data["workspaceId"])
    UUID(data["principalId"])  # Validate retained scope even though the operator owns it.
    manifest, policy, home, private = (
        Path(data[key]).resolve(strict=True) for key in ("manifest", "policy", "home", "private")
    )
    python = Path(data["python"]).resolve(strict=True)
    token = os.environ.get("POTBOT_XPRA_BRIDGE_TOKEN", "")
    if not 32 <= len(token) <= 4096 or "\n" in token or "\r" in token:
        raise CapabilityUnavailableError("Xpra bridge token was not inherited")

    from potbot.tools.operator_attachment_s13 import OperatorAttachmentClient, mount_fingerprint
    from potbot.tools.operator_cli import parse_manifest
    paths = parse_manifest(manifest)
    socket_path = private / "operator.sock"
    attachment = OperatorAttachmentClient(
        socket_path=socket_path,
        workspace_id=paths.workspace_id,
        image_digest=paths.image_digest,
        qualification_sha256=paths.qualification_sha256,
        mounts_sha256=mount_fingerprint(paths.ai_config_dir, paths.ai_history_dir),
    )
    env = {**os.environ, **_monitor_environment()}
    env.update({
        "POTBOT_OPERATOR_REQUIRED": "1",
        "POTBOT_OPERATOR_SOCKET": str(socket_path),
        "POTBOT_OPERATOR_WORKSPACE_ID": str(workspace),
        "POTBOT_OPERATOR_IMAGE_DIGEST": paths.image_digest,
        "POTBOT_OPERATOR_QUALIFICATION_SHA256": paths.qualification_sha256,
        "POTBOT_OPERATOR_MOUNTS_SHA256": attachment.mounts_sha256,
        "POTBOT_OPERATOR_SCOPED_LAUNCHER": str(SCOPED_SOCKET),
        "POTBOT_GUI_AUDIT_REQUIRED": "1",
        "POTBOT_XPRA_BRIDGE_TOKEN": token,
    })
    operator = subprocess.Popen(
        [str(python), "-I", "-B", "-m", "potbot.startup", "operator", "run",
         "--manifest", str(manifest), "--socket-path", str(socket_path)],
        env=env,
    )
    try:
        for _ in range(100):
            if operator.poll() is not None:
                raise CapabilityUnavailableError("Managed WSL operator exited during startup")
            try:
                if attachment.healthy():
                    break
            except Exception:
                pass
            time.sleep(0.1)
        attachment.require_healthy()
        xpra = subprocess.Popen(
            [str(python), "-I", "-B", "-m", "potbot.tools.x11_workspace_server",
             "--workspace-id", str(workspace),
             "--sandbox-root", str(home),
             "--browser-origin", "http://127.0.0.1:3100",
             "--port", "8766",
             "--apps-policy", str(policy)],
            env=env,
        )
        stopped = threading.Event()
        def halt(_signum: int, _frame: object) -> None:
            stopped.set()
        for name in ("SIGTERM", "SIGINT"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), halt)
        while not stopped.wait(0.5):
            if operator.poll() is not None:
                raise CapabilityUnavailableError("Managed WSL operator stopped")
            if xpra.poll() is not None:
                raise CapabilityUnavailableError("Managed WSL Xpra bridge stopped")
        return 0
    finally:
        for process in (locals().get("xpra"), operator):
            if process is not None and process.poll() is None:
                process.terminate()
        for process in (locals().get("xpra"), operator):
            if process is None:
                continue
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--workspace-id", required=True)
    prepare.add_argument("--principal-id", required=True)
    prepare.add_argument("--source-root", required=True)
    prepare.add_argument("--source-sha256", required=True)
    prepare.add_argument("--consumer-commit", required=True)
    prepare.add_argument("--image-source", required=True)

    finalize = commands.add_parser("finalize")
    finalize.add_argument("--workspace-id", required=True)
    finalize.add_argument("--principal-id", required=True)
    finalize.add_argument("--rootfs", required=True)
    finalize.add_argument("--image-digest", required=True)
    finalize.add_argument("--image-source", required=True)
    finalize.add_argument("--qualification-sha256", required=True)
    finalize.add_argument("--home", required=True)
    finalize.add_argument("--private", required=True)
    finalize.add_argument("--model-staging", required=True)
    finalize.add_argument("--shared-models", required=True)
    finalize.add_argument("--shared-tools", required=True)
    finalize.add_argument("--ai-config", required=True)
    finalize.add_argument("--ai-past", required=True)

    run = commands.add_parser("run")
    run.add_argument("--state", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            return _prepare(args)
        if args.action == "finalize":
            return _finalize(args)
        return _run_core(args)
    except (CapabilityUnavailableError, InvalidInputError, OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
