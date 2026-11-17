# Installed Python profiler qualification

This gate runs the sealed Python profiling Cog through an already installed worker image, the native Syn cohort, the scoped accounting launcher, and PostgreSQL. A successful result qualifies the recorded invocation on the recorded host, image, consumer commit, and native release pins. It does not complete T07.3 or qualify every profiler backend.

**Current evidence:** the qualification runner and its rejection tests were implemented and reviewed, but the installed native host run has not been completed in this implementation session. The available workspace denied AF_UNIX socket creation, which prevents use of the installed native Syn and scoped launcher endpoints. Unit-test success is not evidence of a live host pass or a production qualification.

The implementation is [qualify_python_profile_cog.py](../../scripts/qualify_python_profile_cog.py). It uses the existing execution runtime and does not provision a workspace, publish grants, install an image, start a substitute Syn relay, or relax failed accounting checks. The authored workload is [python_profile_workload.py](../../scripts/fixtures/python_profile_workload.py). The existing fixture-image/development-relay acceptance remains separately described in [Device control, profiling and debuggers](07-device-control-profiling-debuggers.md).

## 1. Provision the qualification scope first

Use the existing operator provisioning and authorization procedures for the host. The runner expects the following state to exist before it starts; the list is not a new provisioning command sequence.

| Prerequisite | Required state |
| --- | --- |
| Consumer source | A committed checkout containing the qualifier and workload. Every tracked file under `src/potbot` must match that commit, as must the imported Potbot package. Additional uncommitted Python modules and imported modules from another package location are rejected. |
| Worker image | An installed `ghcr.io/kieransimkin/potbot-sandbox` image pinned by digest, with the profiler worker executable matching the committed `src/potbot/tools/python_profile_worker.py`. Its installed runtime and ledger pins must agree. |
| Image qualification | A retained `potbot.published-image-hosted-smoke/1` report for the same image digest, current consumer commit and current kernel. Its operator and taskstats gates must be qualified, and its workspace-smoke receipt hash must equal the installed qualification pin. An older consumer's report cannot qualify a new checkout. |
| Signature verifier | An executable `cosign` binary whose bytes are independently pinned in the private policy. The runner verifies the fixed official image publisher identity; a policy cannot substitute another publisher. |
| Operator | A healthy installed workspace operator, an existing controller ledger, and a registered Agent home. The workspace must have no active leases when acceptance begins. |
| Native Syn | An administrator-owned native Cog profile and native dispatcher socket outside guest mounts. The selected workspace, Agent cohort, image, qualification, manifest, release authority, group and epoch must agree with the live endpoint. The existing release installation/probe checks remain in force. |
| Accounting | The existing delegated cgroup-v2 subtree, pinned `strace` and cgroup helper, scoped launcher, task-exit collection and private audit directory. All descendants must have complete retained accounting. |
| Database | The existing workspace database, schema and artifact backend, reachable with an unprivileged application login. The qualifier must not use the administrative database login. |
| Task and catalogue | A running episode with the selected active task revision and repository state, plus a sealed catalogue matching `BoundCogRegistry.provenance(python_profile=True)`. The repository state must include the exact admitted workload digest. |
| Persistent Cog grant | A current, unexpired, operator-published grant for this principal, Agent, native identity and exact profiling installation, with `read` and `code_execution` capabilities. The selected active task must allow the granted capabilities. |

Place the exact committed workload in the registered Agent home using the configured `source_file` name. The file must be owned by the registered Agent UID, must not be group- or world-writable, and must not be redirected through a symlink. Capture the corresponding repository state through the existing repository-state mechanism. Copying a script into the home does not by itself update that retained repository state.

The profiler installation is the operation produced by:

```python
install_python_profile(
    source_file,
    source_sha256,
    interpreter_sha256,
    worker_sha256,
    execution_policy,
)
```

The published grant's `installation_sha256` must equal `installation_hash(operation)` for that exact operation. These functions are implemented in [python_profile_cog.py](../../src/potbot/tools/python_profile_cog.py) and [cog_capability_policy.py](../../src/potbot/controller/cog_capability_policy.py). The installation hash includes the operation binding, arguments, executable digest and protocol. Changing the filename, source, interpreter, worker or policy changes the installation identity. A grant for a superficially similar operation is insufficient.

Use the existing grant publication mechanism, backed by [cog_grant_store.py](../../src/potbot/controller/cog_grant_store.py), to establish the grant before acceptance. Grant publication is an operator action; the runner only reads and enforces it. `execution_policy.policy_revision_hash` identifies the published policy's **revision** (`revision_sha256`), not the complete published policy fingerprint. Keep its native identity and enrollment pins consistent with the installed cohort. The active task must include all capabilities in the selected grant, so avoid adding unrelated capabilities to this qualification grant.

## 2. Prepare the private policy

Create an operator-private policy file with mode `0600`, owned by the unprivileged worker service account. Use canonical absolute paths without symlinks or traversal components. Keep the policy, report, native endpoint and other control files outside every guest mount, including shared tools, models and skills.

The example below is a template. Replace every angle-bracket placeholder with the provisioned identity or verified lowercase SHA-256 value. Placeholder text is intentionally invalid; it must not be submitted as a policy. Do not add a DSN, arbitrary command, alternate executable arguments or a development-Syn switch to this document. The schema rejects additional fields.

```json
{
  "profile": "potbot.python-profile-installed-qualification/1",
  "workspace_id": "<canonical workspace UUID>",
  "principal_id": "<canonical principal UUID>",
  "agent_id": "<canonical registered Agent UUID>",
  "snapshot_id": "<sealed profiling catalogue snapshot UUID>",
  "task_revision_id": "<active task revision UUID>",
  "repository_state_id": "<captured repository state UUID>",
  "invocation_id": "<fresh one-use invocation UUID>",
  "operator_manifest": {
    "path": "/absolute/operator-manifest.json",
    "sha256": "<SHA-256 of the installed operator manifest bytes>"
  },
  "native_profile": {
    "path": "/absolute/native-cog-profile.json",
    "sha256": "<SHA-256 of the administrator-owned native profile bytes>"
  },
  "image_smoke": {
    "path": "/absolute/published-image-hosted-smoke.json",
    "sha256": "<SHA-256 of the retained image smoke report bytes>"
  },
  "cosign": {
    "path": "/absolute/cosign",
    "sha256": "<independently verified SHA-256 of the cosign binary>"
  },
  "source_file": "python_profile_workload.py",
  "source_sha256": "<SHA-256 of the exact committed qualification workload>",
  "interpreter_sha256": "<SHA-256 of the resolved Python 3.12 executable in the guest image>",
  "execution_policy": {
    "policy_revision_hash": "<published Cog policy revision SHA-256>",
    "configuration_hash": "<operator-selected configuration SHA-256>",
    "source_digest": "<content_digest of the captured repository state>",
    "executor_version": "<explicit executor version identifier>",
    "approval_receipt_hash": null,
    "max_wall_ms": 30000,
    "max_output_bytes": 16384,
    "budget_json": "{}",
    "host_clock_offset_ms": null,
    "host_clock_uncertainty_ms": null
  },
  "repetitions": 2
}
```

Keep all fields of `ExecutionPolicy.to_dict()`, including the null fields. `max_wall_ms` must be between 30,000 and 60,000; `max_output_bytes` is bounded by the existing execution contract; repetitions must be the integer `2` or `3`. `executor_version` must be a bounded identifier without whitespace. Use the same policy values when computing and publishing the installed operation grant.

`source_sha256` is the digest of `scripts/fixtures/python_profile_workload.py`, while `execution_policy.source_digest` is the repository state's retained content digest. They identify different things. `interpreter_sha256` is the guest interpreter digest, not the host virtual environment's Python digest. The runner derives the profiler worker digest from the committed source and checks the installed image's corresponding executable against it.

All UUIDs must be canonical strings. Use a fresh invocation ID for an independently authorized acceptance run. Once a run has dispatched, reusing its invocation ID must not launch another worker. If a run fails after dispatch, inspect its retained journal, audit and lease state before deciding whether another acceptance attempt is appropriate.

## 3. Run on the installed worker host

Run from the committed checkout with the installed unprivileged worker's environment. Retain the service's existing operator attachment and accounting settings. The explicit required settings are:

```text
POTBOT_OPERATOR_REQUIRED=1
POTBOT_OPERATOR_SCOPED_LAUNCHER=<existing installed scoped launcher endpoint>
POTBOT_PYTHON_PROFILE_DSN=<existing unprivileged application PostgreSQL DSN>
```

The existing `WorkerMonitorConfig` also reads `POTBOT_OPERATOR_AUDIT_ROOT`, `POTBOT_OPERATOR_CGROUP_ROOT`, `POTBOT_OPERATOR_STRACE`, `POTBOT_OPERATOR_CGROUP_JOIN`, `POTBOT_OPERATOR_STRACE_SHA256`, and `POTBOT_OPERATOR_CGROUP_JOIN_SHA256`. Reuse their qualified installed values. Do not make a missing monitor pass by removing accounting requirements or running the qualifier as root.

Supply the DSN through the service's existing secret environment mechanism. The runner keeps it out of the policy and qualification report and does not forward it to `cosign`.

Choose an existing absolute report directory owned by the worker account with mode `0700`, and choose a report filename that does not already exist. Then run:

```bash
.venv/bin/python -m scripts.qualify_python_profile_cog \
  --policy /private/python-profile-policy.json \
  --report /private/python-profile-result-unique.json \
  --run-live
```

The paths above stand for already provisioned private locations. `--run-live` is mandatory. The runner verifies the image signature against:

```text
image: ghcr.io/kieransimkin/potbot-sandbox@sha256:<installed digest>
certificate identity: https://github.com/kieransimkin/potbot/.github/workflows/sandbox-system-image.yml@refs/heads/main
OIDC issuer: https://token.actions.githubusercontent.com
```

The approved `cosign` binary must be able to complete its ordinary verification under the host's configured network/trust environment. The qualifier neither downloads nor installs a replacement verifier or image when verification fails.

## 4. What the acceptance run checks

The successful path performs the following checks in the ordinary controller and worker execution path:

1. Verify committed consumer bytes, installed pins, the official image signature, current image/kernel smoke, registered Agent home, idle workspace, live operator, native cohort and accounting prerequisites.
2. Construct the exact profiling operation and use its existing sealed catalogue, active task and persistent Cog grant. Malformed input containing an unadmitted filename must be refused before any worker dispatch.
3. Dispatch exactly one admitted profiling Cog. The fixed authored workload records the registered UID/GID, computes a deterministic result and exercises denied network socket creation and denied image-root file creation.
4. Validate every retained baseline and profiled sample, equal result hashes, the expected `item_cost` hotspot with 200,000 calls, repetition counts, source/interpreter pins, current kernel and recomputed timing summaries. Private workload output must not appear in the retained result artifact. `root_write_denied` describes the write probe; it is not a standalone proof that every image mount is read-only.
5. Refuse the completed invocation through both the same controller and a newly reconstructed `ExecutionCogRuntime`, with no additional worker dispatch. Confirm that the workspace lease is released.
6. Verify the retained audit/process tree, complete task-exit accounting and exact workspace/run identity. Through a fresh database handle, find exactly one terminal completion, read its artifact, and verify the artifact digest and Cog receipt against the returned outcome.
7. Revalidate the runtime pin, then recheck the consumer commit, package bytes and imported module origins before writing a success report.

The warm-up/sample processes are descendants of the single Cog worker dispatch. `worker_launches: 1` counts that dispatch; it does not claim that the profiling measurement uses only one operating-system process.

## 5. Interpret and retain the result

On success, the command exits `0`, emits a short success summary, and creates the new mode-`0600` report. Its schema is `potbot.python-profile-installed-qualification/1` and its scope is `installed_python_profiler_cog`.

The report records the consumer commit, landmark source hashes and complete package-tree digest; image digest, workspace qualification and signature-evidence hashes; native profile and release-authority hashes plus native provenance; kernel, workspace, Agent and invocation IDs; registered UID/GID; worker/interpreter/workload digests; timing samples and comparison; audit summary; terminal artifact/receipt hashes; and the completed dispatch/readback checks. Retain the corresponding private operator audit and database artifacts using the host's existing evidence-retention procedure. The bounded qualification report is not a replacement for those retained records.

The report's `not_qualified` list explicitly excludes:

- `systemd_restart`: no worker service or operating-system process restart is exercised.
- `native_syn_restart`: no native Syn node restart is exercised.
- `syn_application_rpc_transport`: this profiler gate does not qualify a Syn application RPC route.
- `lost_reply_faults`: no dropped-reply or indeterminate-dispatch fault is injected.
- `stable_performance_benchmark`: the measured overhead is evidence for this run, not a portable performance guarantee.
- `all_T07.3_backends`: other profilers, devices, operating systems and specialist routes retain their separate gates.

`fresh_controller_replay_refused` means reconstruction of the controller object against retained state. `postgres_receipt_readback` means readback through a fresh database handle. Neither field should be described as a service-restart or crash-recovery qualification.

On a rejected or unavailable gate, the command exits `1`, emits `qualified: false` to stderr, and does not create a success report. It reports a failure phase and error class without printing private exception text or a DSN. Missing infrastructure is a failed qualification, not a skipped success.

| Failure phase | What to investigate |
| --- | --- |
| `explicit_live_opt_in` | `--run-live` was not supplied. No policy or installed state has been admitted. |
| `private_policy_and_output` | Policy fields/permissions/paths or report destination are invalid. An existing report is deliberately not overwritten. |
| `committed_consumer` | The qualifier/workload is uncommitted, package source differs from HEAD, a module comes from an unexpected location, or another required source check failed. |
| `installed_profile_and_live_acceptance` | Review the installed operator, image/signature/smoke pins, native endpoint, database/task/grant state, worker audit and retained invocation state. This phase includes checks both before and after dispatch. |
| `consumer_readback` | Source, imports or the selected commit changed during acceptance. A completed worker run does not override this failure. |
| `private_report_write` | Acceptance reached report publication but the exclusive private report write failed. Inspect retained evidence before rerunning. |

Correct the underlying installed state and reconcile any completed or indeterminate invocation. Do not convert a failed or missing report into a passing result by copying an older success file, relaxing the pins, or treating an artifact-upload warning as qualification.

## 6. GitHub Actions: contracts and installed acceptance

The workflow is [python-profile-native-qualification.yml](../../.github/workflows/python-profile-native-qualification.yml).

The `qualification-contracts` job runs on GitHub-hosted Ubuntu for the configured push/pull-request paths and for manual dispatches. It runs the qualification unit tests and Ruff checks. Those tests check policy/evidence rejection and source binding; they do not start or qualify the installed native worker.

The `installed-native-profile` job runs only when all of the following apply:

- The event is `workflow_dispatch` and the operator selected `run_live: true`.
- The selected ref is `refs/heads/main` in `kieransimkin/potbot`.
- The contract job passed.
- An existing runner has the labels `self-hosted`, `linux`, and `potbot-worker-qualified`, with the installed worker environment available to its job.
- The `worker-qualification` environment supplies the `POTBOT_PYTHON_PROFILE_POLICY` variable containing the existing private policy's absolute path and the `POTBOT_PYTHON_PROFILE_DSN` secret containing the application login.

The workflow uses the existing host policy and launcher; selecting the self-hosted label does not provision or prove them. Refresh the policy and retained image smoke to bind the exact main commit being checked out. Ensure the checked-out/imported package and installed profiler worker satisfy the same source requirements as a manual invocation.

The live job serializes access under the `potbot-worker-qualification` concurrency group and does not cancel an earlier run to start another. It writes a report under a private `artifacts/python-profile-native` directory with a run/attempt-specific filename. The upload step retains matching reports for 14 days; if a failed gate created no report, the upload step warns rather than creating evidence. The acceptance step's exit status remains authoritative.

An ordinary pull-request run, a manual dispatch with the default `run_live: false`, or a successful hosted contract job leaves installed native qualification open. Close only this scoped gate after a successful live result has been reviewed together with its recorded identities and retained evidence. Keep the explicit restart, lost-reply, other-host and other-backend gates open until their own acceptance runs are completed.
