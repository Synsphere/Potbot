# Windows one-click mandatory WSL core runtime

The normal Windows launcher keeps PostgreSQL, the API and Next.js native while
Potbot's mandatory operator, scoped sandbox and Xpra bridge run inside the
existing owned `Potbot-SynOperator` WSL2 distribution.

The first start (and source changes) stages the current source tree by digest,
including every source path required by the wheel build (`src`, vocabulary,
the cgroup helper and the native identity provider). A regression test keeps
the explicit staging allow-list aligned with Hatch's `force-include` inputs.
It installs SHA-256-pinned uv and Cosign, synchronizes the existing lockfile,
and explicitly enables Ubuntu's `universe` repository before installing the
Xpra/Bubblewrap/OCI prerequisites. Before worker qualification, Windows also
downloads and verifies the signed `v0.5.0.dev13` TASKSTATS/pid-namespace WSL
kernel when the managed VM is not already running it, activates that kernel
through the existing rollback-safe `.wslconfig` transaction, and refuses to
terminate unrelated running WSL distributions. It then verifies the pinned
signed sandbox image, runs the repository's existing published-image host smoke,
provisions the scoped worker launcher, pins the qualified runtime and starts the operator
plus Xpra bridge.

The Windows handoff retains bounded diagnostics in
`.potbot/service/wsl-core-bootstrap.log` and
`.potbot/service/wsl-core-qualification.log`. Bootstrap failures identify the
exact failed stage (`ubuntu-repositories`, `core-packages`, `uv`, `cosign`,
`source-staging` or `locked-python`) instead of collapsing every failure into a
generic capability error. WSL-side qualification now emits stage markers too, so
failures distinguish scoped-launcher provisioning, signed sandbox-image
qualification and final workspace-state creation.

Only the fresh Xpra bridge bearer crosses from Windows to WSL, via `WSLENV`.
PostgreSQL, provider credentials, API tokens and the GUI password stay in their
existing Windows custody.

A failed WSL setup/restart, image signature, kernel accounting gate, sandbox
qualification, operator attachment or Xpra bridge remains a startup failure.
The service error response preserves the fixed DomainError stage instead of
mislabeling every failure as a native service-manager permissions problem.

## Windows Operator custody

Windows Operator custody is host-owned. Potbot requires the exact signed Syn
`v0.11.0` operator policy (source
`3ed8a43c7e08480b4e5ce13c9e4720c3001b70dc`, manifest
`bb496a73ebd9d37018f6274a6dc29e4e20f03b96b9ab4e687289dcf6a2ac9a6e`) and runs
Syn's Windows DPAPI host-custody broker as the logged-in Windows user. WSL
receives only the bounded named-pipe bridge variables. When Windows custody is
required, the Potbot operator wrapper refuses to launch if that bridge is
missing or if the selected Syn policy differs, so the operator cannot silently
fall back to GNOME Keyring.

Syn 0.11.0's ordinary runtime wheel does not contain the separate
`host-custody/` project. As an interim integration, Potbot stages only that
project's required files from the exact signed release source commit, verifies
their immutable Git blob identities, and creates a separate environment from
Syn's own `host-custody/uv.lock`. This environment is deliberately separate from
Potbot's Python environment because the two projects retain different
cryptography dependency contracts.

Interactive/operator-administration commands use a short-lived broker. The
installed Windows lifetime task uses a separate broker whose process lifetime is
bound to the WSL operator service. The short-lived broker is stopped before the
lifetime broker starts so both processes never contend for the same exclusive
DPAPI vault lock.

### Planned release-flow improvement — not implemented

A future Syn release milestone should emit Potbot-specific signed artifacts for
the Windows integration instead of requiring source-file staging. At minimum the
release should provide a versioned Potbot host-custody wheel/bundle containing
the broker, pipe client and exact dependency lock, identify it in the signed Syn
release manifest, and publish immutable hashes alongside the normal runtime and
client packages. Potbot should then consume that artifact through its existing
release verifier, eliminating raw source retrieval and the temporary Git-blob
allow-list.

Acceptance for that future milestone must prove reproducible Windows artifacts,
signed-manifest coverage, clean/offline installation from retained release
bytes, DPAPI/named-pipe qualification, upgrade/rollback without identity
replacement, and refusal of an absent or mismatched Potbot-specific artifact.
It must not be implemented by silently copying an arbitrary Syn checkout into a
Potbot release.
