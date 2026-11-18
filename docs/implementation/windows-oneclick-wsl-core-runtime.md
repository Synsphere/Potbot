# Windows one-click mandatory WSL core runtime

The normal Windows launcher keeps PostgreSQL, the API and Next.js native while
Potbot's mandatory operator, scoped sandbox and Xpra bridge run inside the
existing owned `Potbot-SynOperator` WSL2 distribution.

The first start (and source changes) stages the current source tree by digest,
installs SHA-256-pinned uv and Cosign, synchronizes the existing lockfile,
installs the Ubuntu Xpra/Bubblewrap/OCI prerequisites, verifies the pinned
signed sandbox image, runs the repository's existing published-image host smoke,
provisions the scoped worker launcher, pins the qualified runtime and starts the
operator plus Xpra bridge.

Only the fresh Xpra bridge bearer crosses from Windows to WSL, via `WSLENV`.
PostgreSQL, provider credentials, API tokens and the GUI password stay in their
existing Windows custody.

A failed WSL setup/restart, image signature, kernel accounting gate, sandbox
qualification, operator attachment or Xpra bridge remains a startup failure.
The service error response now preserves the fixed DomainError stage instead of
mislabeling every failure as a native service-manager permissions problem.
