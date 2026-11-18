# Windows one-click mandatory WSL core runtime

The normal Windows launcher keeps PostgreSQL, the API and Next.js native while
Potbot's mandatory operator, scoped sandbox and Xpra bridge run inside the
existing owned `Potbot-SynOperator` WSL2 distribution.

The first start (and source changes) stages the current source tree by digest,
installs SHA-256-pinned uv and Cosign, synchronizes the existing lockfile,
and explicitly enables Ubuntu's `universe` repository before installing the
Xpra/Bubblewrap/OCI prerequisites. It then verifies the pinned signed sandbox
image, runs the repository's existing published-image host smoke, provisions
the scoped worker launcher, pins the qualified runtime and starts the operator
plus Xpra bridge.

The Windows handoff retains bounded diagnostics in
`.potbot/service/wsl-core-bootstrap.log` and
`.potbot/service/wsl-core-qualification.log`. Bootstrap failures identify the
exact failed stage (`ubuntu-repositories`, `core-packages`, `uv`, `cosign`,
`source-staging` or `locked-python`) instead of collapsing every failure into a
generic capability error.

Only the fresh Xpra bridge bearer crosses from Windows to WSL, via `WSLENV`.
PostgreSQL, provider credentials, API tokens and the GUI password stay in their
existing Windows custody.

A failed WSL setup/restart, image signature, kernel accounting gate, sandbox
qualification, operator attachment or Xpra bridge remains a startup failure.
The service error response preserves the fixed DomainError stage instead of
mislabeling every failure as a native service-manager permissions problem.
