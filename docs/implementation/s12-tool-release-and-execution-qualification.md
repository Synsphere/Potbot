# S12 release enforcement and execution qualification

**Patch baseline:** `413494517bd359dd908fcfd9571a22789b24ebb3`, reviewed 17 November 2026.
**Scope:** U04-R/RD14, T01.4, selected U01-A/T01.3 follow-through, browser timeout
recovery, and an installed Python profiling qualification entry point. The
canonical 48-stage tracker and historical U00 audit checkpoint are retained.

This increment implements the missing post-activation release decision on the
existing S9/S10/S11 path. It also makes acceptance reports harder to misread as
production qualification. It does not establish whole-platform qualification.

## Release ownership and enforcement

Application migration **38** extends `ui_tool_upgrade_approvals` and its existing
append-only audit. PostgreSQL retains the release phase, generation, binding,
postflight evidence and keyed release MAC. Migration 28 and other historical
migrations retain their original checksums and data.

The operator writes a private denial projection before changing an installation.
The projection binds the workspace, Agent, operation, direction, source commit,
definition and installed artifact identities, image and qualification digests,
approved suite/test evidence, and exact executable pins. This projection can
hold a tool; it cannot release one. The existing local maintenance journal
retains recovery information for interrupted filesystem exchanges.

Normal launch admission reads the PostgreSQL decision and independently checks
the original S11 approval MAC, installed source/artifacts and current runtime
pins. An unavailable database, missing credential, changed source, altered
evidence or missing postflight leaves the tool held. The installed native Cog
startup binds this reader from the existing protected setup credential and
workspace database context; reconstructing the controller does not manufacture
an approval or rerun postflight.

At sandbox construction, held tool directories and inactive S9 stages are
masked so an image interpreter cannot simply read a held script. A bounded
maintenance probe permits only its selected stage or exact installed binding
while the existing exclusive tool lease remains live. Untouched installations
retain their previous admission policy; migration 38 does not automatically
certify existing installations.

Changed installations must retain the existing S9 four-directory layout
(`checkout`, `environment`, `static`, `downloads`) and at least one declared
executable reusable artifact under `static` or `environment`. Undeclared root
content is rejected. Source-only definitions without executable pins cannot
enter this release path; they are rejected before the installation exchange.

Whole-tree sandbox readers now declare a `shared_tool: *` read footprint in
the existing lease ledger. This prevents a running guest from seeing a different
tool's later swap or newly created stage through the parent mount. Parallel
readers remain allowed; tool maintenance waits for whole-tree readers, including
other workspaces sharing that host ledger. Maintenance probes use the same
footprint, and a writable tool mount cannot override a release hold.

## State model

The public S11 `state` remains compatible. Four added public fields expose only
`releaseState`, `releaseGeneration`, `releaseBindingSha256`, and
`releaseEvidenceSha256`. Private commands, locations, tokens and raw output are
not included. The existing `/setup/tools` page displays this additional state.

The frontend Next.js pin is updated from 16.4.3 to 16.4.5, the compatible
[security patch release](https://github.com/vercel/next.js/releases/tag/v16.4.5).
The old pin caused the production-build security check to stop during patch
validation. React and the supported Node engine remain unchanged.

| Release phase | Meaning | Normal use of the changed tool |
| --- | --- | --- |
| `none` | No S12 release established | Held after an activation/rollback projection exists |
| `repinned` | Independent installed-source and runtime-pin readback succeeded | Held; approved postflight still required |
| `testing` | A single postflight owner is executing the pinned suite | Held; no duplicate attempt |
| `failed` | A completed attempt did not supply complete passing evidence | Held; investigate and use the controlled rollback path |
| `indeterminate` | Completion or lease ownership is uncertain | Held; reconcile the process/lease outcome before further maintenance |
| `released` | Passing postflight and exact installed binding retained in PostgreSQL | Eligible under all existing dispatch/grant rules |

`confirm-pins` now records `repinned`; it no longer makes a new installation
generally usable. `postflight` runs the **original approved suite** against the
installed location and verifies actual dispatch, confirmed stop, successful
exit, bounded elapsed time, isolation identity and output digest. It rechecks
the installed binding before and after execution. A repeat request for an
already released, unchanged operation only verifies retained evidence. It
does not run the suite again.

## Operator sequence

Use the existing admin connection to apply pending application migrations:

```sh
uv run --locked potbot admin --schema potbot migrate
```

Keep the normal watcher and workers quiesced during the S11 maintenance CLI,
which owns the same singleton watcher. Use the same protected credential
profile as the API/operator. The existing S9 plan is the authority for the suite;
do not replace it with a new convenient smoke command after approval.

1. Stage, build and test through S9/S10, publish through S11, and obtain the
   existing operator approval in `/setup/tools`.
2. Run the existing `run-once` activation command. Read its independently
   verifiable pin proposal. The changed installation remains held.
3. Independently verify and install the new executable pins; reload the
   operator manifest through the established maintenance procedure.
4. Run `confirm-pins`, then `postflight` with the original plan. Inspect `status`
   and require `releaseState: released` before resuming use of that tool.

```sh
PYTHONPATH=src .venv/bin/python scripts/tool_approval_s11.py \
  --manifest /srv/potbot/controller/manifest.json \
  --workspace-name WORKSPACE_NAME --principal-id PRINCIPAL_UUID \
  --agent-id AGENT_UUID --operation-id OPERATION_UUID \
  --proposal-dir /srv/potbot/controller/pin-proposals confirm-pins

PYTHONPATH=src .venv/bin/python scripts/tool_approval_s11.py \
  --manifest /srv/potbot/controller/manifest.json \
  --workspace-name WORKSPACE_NAME --principal-id PRINCIPAL_UUID \
  --agent-id AGENT_UUID --operation-id OPERATION_UUID \
  --proposal-dir /srv/potbot/controller/pin-proposals \
  --plan /srv/potbot/controller/original-approved-plan.json postflight

PYTHONPATH=src .venv/bin/python scripts/tool_approval_s11.py \
  --manifest /srv/potbot/controller/manifest.json \
  --workspace-name WORKSPACE_NAME --principal-id PRINCIPAL_UUID \
  --agent-id AGENT_UUID --operation-id OPERATION_UUID \
  --proposal-dir /srv/potbot/controller/pin-proposals status
```

These paths and UUIDs are placeholders for an existing installation. The
commands use its existing PostgreSQL and native credential configuration.

### Interrupted activation and rollback

First inspect the local S9 state and establish the actual process/filesystem
outcome with the existing operator. S9 `reconcile` applies only to incomplete
`switching` or `rolling_back` transitions after lease reconciliation; do not
invoke it on an already committed `active` operation.

If S9 is `active` but the S11 acknowledgement was lost, obtain the active
`runtime-pins` proposal and independently load those exact pins. Then run
`reconcile-activation --confirm-reconciled`, `confirm-pins`, and the original
suite's `postflight` in that order. Reconciliation records the verified outcome
without another activation. It does not reopen an attempted postflight.

Rollback uses the existing S9 fenced exchange. It writes a new denial binding
for the restored bits. The S9 `runtime-pins` command now verifies a
`rolled_back` installation and emits the restored version's pin proposal:

```sh
PYTHONPATH=src .venv/bin/python scripts/tool_upgrade_s9.py \
  --manifest /srv/potbot/controller/manifest.json \
  --workspace-name WORKSPACE_NAME --agent-id AGENT_UUID \
  --operation-id OPERATION_UUID --slug TOOL_SLUG runtime-pins
```

The proposal does not edit the manifest or release the tool.
Independently install the restored pins, run
`confirm-pins`, and complete the same approved suite before release. A failed
or uncertain postflight is never silently reset and retried. Missing process
completion or a live maintenance fence must be resolved first.

Do not remove private holds, edit release rows to bypass the gate, or deploy
older code while a changed installation is awaiting S12 release. Removing the
enforcement code is not a tool rollback. The ordinary application migration
system remains the only schema authority.

Rotating `api.setup_token` invalidates both outstanding approval MACs and retained
release MACs. Previously released tools then remain held. The original record
requires the original protected credential to validate; otherwise use a genuinely
new tested and approved upgrade. `confirm-pins` cannot reauthorize an old
decision under a replacement key. Plan credential recovery/rotation alongside
maintenance rather than editing the stored decision.

## T01.4 acceptance reports

`scripts/tool_phase1_gate.py` retains its `offline` and `qualified` modes. Each
invocation now has a unique run directory, source manifest and run ID. It
invalidates the current report to a non-passing `running` state before running
tests, so interruption cannot leave an old success looking current.

Qualification requires the established full application checker, both required
PostgreSQL and sandbox result rows, and their actual non-empty JUnit cases.
Malformed XML, contradictory counters, duplicate check rows, failures, errors,
skips, missing evidence or source changes prevent qualification. A JSON summary
alone is insufficient. Dirty source is represented by its actual source hashes;
the old Git HEAD is not presented as its full identity.

The legacy `fully_qualified` field is explicitly scoped by
`qualification_scope: application_postgres_and_sandbox_tests`.
`production_runtime_qualified` remains **false**. Passing that application gate
does not qualify signed images, native custody/Syn, a provider, device or
specialist backend.

## Producer coverage and next acceptance work

This matrix records existing integration owners and the next evidence needed.
It is a review aid, not another runtime registry or a new scheduler.

| Producer | Existing integration and evidence anchors | Remaining next gate |
| --- | --- | --- |
| Local execution and signed Cog provenance | `tools/gateway.py`, `controller/execution_cogs.py`, `controller/execution_journal.py`; `test_execution_provenance.py` | Keep task/repository/policy/source/grant identities linked through dispatch, receipt and settlement for every production producer |
| Controlled queue effects | `tools/queue_transport.py`, `tools/queue_transport_worker.py`; `test_directed_queue_transport.py`; U01-A transport recovery document | Extend the existing lost-acknowledgement/receipt evidence pattern to each external effect; preserve indeterminate outcomes without blind retry |
| MCP stdio/HTTP | `tools/mcp_gateway.py`, `tools/mcp_sandbox.py`, `tools/syn_mcp_route.py`; MCP protocol/worker/broker suites | Qualify the exact installed native route, authenticated endpoint and fault recovery; a fixture service does not establish arbitrary provider compatibility |
| Browser actions | `tools/xpra_browser_worker.py`, `tools/xpra_browser_control.py`; `test_browser_controller_provenance.py` and rendered matrix | Rerun all four browser profiles, then installed Xpra lifecycle, cancellation and private-session recovery |
| Artifact range/upload | `controller/artifact_ranges.py`, `controller/artifact_uploads.py`; corresponding PostgreSQL integration suites | Preserve existing receipts and qualify each enabled network/retention backend independently |
| Python profiling Cog | `tools/python_profile_cog.py`, `controller/native_cog_dispatch.py`; `test_python_profile_cog.py`; new qualifier | Run the installed qualification described below; service/node restart and lost-reply faults remain separate |

The [installed profiler qualification guide](python-profiler-installed-qualification.md)
documents the new runner and manual trusted-host workflow. It checks the
published signed worker image, installed native Syn/runtime, persistent grant,
real profiler output, task accounting and durable receipt readback. It requires
committed source and an already provisioned operator host. Local contract tests
do not substitute for that run.

The [browser recovery note](browser-rendered-capture-recovery.md) records the
latest timeout boundary. The patch prevents a timed-out capture from allowing
later mutations or captures on the same worker. It does not claim to have
reproduced or cured the Chromium surface-capture stall on the graphical CI host.

## Verification boundary

Regression coverage includes real Git installation exchanges, held-directory
admission, existing S9/S10/S11 compatibility, browser timeout/cancellation and
private-channel retirement, acceptance-report freshness and JUnit integrity,
and the installed-profiler evidence contract. PostgreSQL integration tests use
real application migrations, workspace RLS, approval MACs, durable release
decisions and restart readback. Their injected postflight process receipts are
explicit fixtures, not native host qualification.

Before deployment, run the repository's full CI gates at the final commit,
the four-profile rendered browser workflow, and the installed-host profiler
gate. Exercise real S9 activation, repin, postflight, operator restart and
rollback with the signed image, native credentials and production maintenance
watcher. Keep U01-A/T01.3 producer coverage and T07.3 specialist profiles open
until their individual evidence is retained.

### Local patch verification, 17 November 2026

| Check | Observed result |
| --- | --- |
| Related Python contracts and regressions | 359 passed, plus 8 subtests |
| PostgreSQL S12, persistent grants, database and phase-4 upgrade suites | 30 passed |
| Roadmap inventory regression suite | 50 passed; 207 sources and 731 requirements mapped |
| Full frontend suite | 99 passed; no skips |
| Frontend typecheck and production build | Passed with Next.js 16.4.5 |
| Repository Ruff and format checks | Passed |
| Application mypy | Passed for 388 source files; gate and qualifier checked separately |
| Canonical plan checks | 48 tool stages and 32 object-memory stages preserved |

The disposable database was PostgreSQL 16.2 with pgvector 0.8.7 over local TCP,
without TLS. The new S12 database tests use explicit injected process receipts;
they establish database and release-state behavior, not native worker execution.

A broader migration/phase-3/phase-4 run also encountered the existing sandbox
CLI test returning `infrastructure_error`. The same test failed identically on
a separate pristine checkout of `413494517bd359dd908fcfd9571a22789b24ebb3`.
It remains a limitation of acceptance in this environment; the patch does not
skip, weaken or relabel it. Repository-wide CI and the qualified T01 gate have
therefore not been declared green here.
