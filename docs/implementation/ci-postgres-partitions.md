# Complete PostgreSQL test coverage in isolated CI partitions

The [PR94 verification run](https://github.com/Synsphere/Potbot/actions/runs/45298121057)
passed unit and clean-wheel checks, then reached roughly 84% of the growing
PostgreSQL lane before its 30-minute subprocess deadline. Its retained output
showed passing progress rather than an assertion failure before timeout.

CI now configures four deterministic partitions of the **same original selector**:
`postgres and not sandbox`. Each hosted runner owns its own disposable Docker
database. Individual application fixtures still create fresh databases, roles
and migrations through the existing harness; they do not share mutable fixture
state between runners or use the operator's persistent server.

```mermaid
flowchart TD
    S[Same source and full original PostgreSQL collection] --> P[Stable node-ID partition]
    P --> A[Existing verify job: partition 0 plus other original gates]
    P --> B[Isolated PostgreSQL runner: partition 1]
    P --> C[Isolated PostgreSQL runner: partition 2]
    P --> D[Isolated PostgreSQL runner: partition 3]
    A --> R[Collection and terminal-outcome receipts]
    B --> R
    C --> R
    D --> R
    R --> G[Require successful jobs and exact disjoint full union]
```

*Generated configuration diagram. It explains the implemented orchestration;
new CI completion and elapsed-time improvement require actual run evidence.*

The existing `verify` check name and its unit, wheel, sandbox, migration,
accounting and acceptance gates remain. Three additional runners execute the
other PostgreSQL partitions using the same locked environment and sanitized
`scripts/check.py` owner. Exact-source T01 evidence producers also run explicitly
in the primary job, so their acceptance artifacts do not depend on which
partition owns a producer test.

`--postgres-shard 0/4` opts into a zero-based partition. The original local
`scripts/check.py --with-postgres --with-sandbox` still runs the complete lane.
Its bounded full-lane deadline is now one hour; CI partitions keep a 30-minute
subprocess bound. `--postgres-shard-only` is reserved for an explicitly selected
PostgreSQL partition and refuses incompatible flags. It does not silently disable
the default application's other checks.

The explicit pytest plugin runs after the original marker selection, hashes
each complete node ID and records the full collection identity before executing
its assigned subset. A receipt starts incomplete. Completion additionally
records pytest's exit status and exactly one terminal outcome per selected test.

The `postgres-coverage` job requires successful execution jobs and verifies:

- The exact expected GitHub source commit and unchanged selector.
- Matching full-collection counts and digests, and every unique partition index.
- Correct deterministic assignment, no overlap, and a union equal to the full
  collection in both count and digest.
- Successful pytest completion and one terminal result for every selected item.
  Skips are counted separately from passed tests.

Missing partitions, changed collections, foreign source revisions, failed or
aborted runs, collection-only runs and reused receipts cannot establish a green
coverage gate. This checks CI accounting, not cryptographic runner attestation
or production qualification; `runtime_qualified` remains false.

Unit tests exercise receipt rejection and actual isolated child-pytest processes,
including an early stop that returns exit code zero. JSON receipts, sanitized
logs and JUnit reports supply the evidence surface. This configuration change
has no native UI to capture; a screenshot or video would add no execution proof.


## Follow-up failures exposed by the complete lane

The first actual partition run reached its terminal results in 742 seconds:
195 tests passed and the tool-inventory integration case failed. Its full Git
tree contained 34,381,932 bytes, exceeding the unchanged 32 MiB repository
snapshot bound. That fixture now explicitly excludes only the generated
`docs/implementation/roadmap-inventory.json` projection through the existing
immutable snapshot profile. All Python tool sources and the original Cog,
PostgreSQL, receipt and API assertions remain. The profile and excluded path
are checked; production capture still refuses oversized snapshots.

Separately, headed first-frame acceptance sometimes timed out after a document
reported visible/focused. The synthetic test harness now additionally requires
its uniquely named native X11 window to be mapped and viewable, using bounded
read-only `xwininfo` queries. The existing pinned browser and real-pixel checks
then run unchanged. This readiness condition is a tested diagnostic/repair;
actual rendered CI must establish whether it resolves the cold-start failure.
It neither captures a warm-up image nor changes focus, the worker's six-second
deadline, capture ownership, cancellation fence, fallback rules or command set.
The existing diagram describes partition orchestration; native readiness is
retained as JSON/JUnit state rather than a screenshot of an unverified outcome.


The subsequent primary verification job passed 95 sandbox tests and failed the
mandatory authored-PDF extraction test because its runner lacked `pdftotext`.
[The retained job log](https://github.com/Synsphere/Potbot/actions/runs/45315599913/job/138181579520)
identifies the missing `poppler-utils` prerequisite. The primary runner now
installs that Ubuntu package beside its existing isolation prerequisites. The
test still pins the actual executable hash, uses the existing sandbox and
requires extraction, exact page spans and confirmed process termination. A new
CI run must establish the real PDF pass; the missing prerequisite is not waived.
Downstream source-inventory uploads were also absent because this sandbox
failure stopped their explicit producer step; their original acceptance and
artifact requirements remain in place.


The next complete partition again passed 195 tests and exposed eight existing
root-level installer/desktop modules absent from the explicit source owner map.
[Its actual assertion](https://github.com/Synsphere/Potbot/actions/runs/45318984092/job/138192600901)
refused an unowned source module. Their mappings now reuse existing owners:

| Committed source | Existing integration owner |
|---|---|
| `core_runtime.py`, `desktop.py`, `desktop_state.py`, `frontend_worker.py` | Entry points and native startup |
| `oneclick.py`, `windows_core_runtime.py`, `wsl_core_runtime.py` | Entry points and native startup |
| `native_postgres.py` | PostgreSQL persistence |

A unit regression inventories every committed Python path using inert synthetic
bodies and requires an explicit owner, while an unknown root module still fails
closed. It executes no inspected source and establishes no runtime qualification.
The actual full-source PostgreSQL integration remains a separate mandatory gate.


Updating the inventory module also changes the evidence digest used by four
curated optional inspection profiles. Discovery CI correctly rejected the stale
pin after 325 other cases passed. Those four source-only profiles now have a
new version and source commit identifying the actual inventory change, with its
verified file digest. The other sixteen profiles, unavailable/runtime flags and
inspect-only distributions remain unchanged. Existing admitted registry snapshots
are not rewritten or activated by changing this example manifest; future source
admission retains its normal review. The complete twenty-profile pin check is
included in local verification before another CI run.


The PR95 follow-up still timed out on a first surface capture after native
mapping succeeded. Chromium's own [PageHandler implementation](https://github.com/chromium/chromium/blob/main/content/browser/devtools/protocol/page_handler.cc)
shows that surface capture reapplies emulation and view sizing, while the native
view snapshot bypasses that path. The headed worker now prefers its private
native view snapshot, with a surface fallback only after a completed matching
engine error. The six-second budget, single native request ownership and
timeout/cancellation fence remain. Actual rendered tests additionally require
the JPEG's independently parsed dimensions to be exactly 1280 by 800, alongside
the existing real-pixel change and independent cold-start checks. Unit tests
alone do not establish that this resolves the observed Chromium stall.

The same run spent its entire fifteen-minute budget downloading recommended
display packages from an HTTP EC2 Ubuntu mirror. The disposable CI mirror
preparation now recognizes that configured mirror as well as Azure and standard
archives, probes bounded HTTPS metadata endpoints, and keeps Ubuntu's signed APT
verification. Explicit display tools use `--no-install-recommends`; pinned engine
dependencies and all browser variants remain mandatory. No production host's
APT configuration is changed by this CI script. The existing orchestration
diagram and actual JSON/JUnit receipts cover this repair; no native UI capture
was produced locally for an unverified result.
