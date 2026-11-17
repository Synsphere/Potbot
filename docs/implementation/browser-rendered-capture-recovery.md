# Browser rendered capture recovery

## Current follow-through — 17 November 2026

At source baseline `413494517bd359dd908fcfd9571a22789b24ebb3`, the
[rendered-worker matrix](https://github.com/kieransimkin/potbot/actions/runs/43501963299)
failed only in the `playwright-stealth` profile. The accessibility/action case
timed out at the initial `Page.captureScreenshot` surface read after about six
seconds, before the semantic action. The other three profiles passed. This is
different from the completed native capture error described in the historical
note below.

The current patch retains the existing six-second capture deadline and permits
the view fallback only after the precise completed native error. A timeout or
cancellation now permanently fences the **worker instance**, including later
capture and mutation requests. Direct concurrent captures have one owner. The
worker flushes one bounded failure with `session_fenced: true`, then closes its
private pipe/browser. The host validates this failure-only marker and retires
the channel immediately, even while process cleanup continues. A fenced worker
cannot restart itself or replay an action; replacement belongs to the operator.
Ordinary completed read rejection leaves a healthy channel usable.

Regression tests cover timeout/cancellation followed by later requests,
concurrent capture ownership, shutdown response, invalid fence markers and host
channel retirement. The new failure cases were also checked against the original
source, where they failed. These tests establish the recovery behavior, not a
remedy for Chromium's underlying compositor stall.

The graphical matrix and actual Xpra/operator host still need rerunning at the
final patch commit. This implementation environment denied AF_UNIX sockets and
could not supply the private display/native endpoint setup. No browser profile
was relabelled as qualified and no timeout was increased to hide the failure.

## Historical repair — 1 November 2026

Baseline: `main` at `4c52d8b4402fec12ee1a8eb4e6f6795805f62ccd`.
Status at that historical checkpoint: source patch prepared; **not merged or live-host qualified**.

## Confirmed failure and bounded repair

In the GitHub Actions rendered-worker matrix run `41301571455`, Patchright,
Playwright+Stealth and Patchright+Stealth passed all five real cases. Plain
Playwright failed 1/5 cases during the first JPEG screenshot with Chromium's
`Page.captureScreenshot: Unable to capture screenshot`, after fonts had loaded.

This change keeps the same private Playwright/Patchright page, existing image
shape, SHA-256 evidence, size limit (600 KiB), restricted CDP RPC allowlist,
and no host-browser attach or network privilege. For **only** that precise
surface-capture error, it retries the read once and then tries a bounded
internal `Page.captureScreenshot` with `fromSurface=false`. The fallback is not
added to the model-callable CDP allowlist. Any missing, malformed, oversized or
non-JPEG bytes fail closed. Neither mutating actions nor other errors are
retried. The worker's independent 12-second RPC deadline remains authoritative.

## Requalification

Run the added mock-based regression tests, then the existing four-profile
rendered acceptance workflow against a clean checkout on its real browser
matrix. Required evidence is **five passing cases per profile**, no skips,
matching package pins and browser-binary identities. A patched source or local
unit test alone does **not** prove that the original Chromium failure is fixed.

```sh
uv run --locked python -m pytest -q \
  tests/unit/test_browser_capture_recovery.py \
  tests/unit/test_browser_worker_hardening.py \
  tests/unit/test_browser_rendered_gate.py \
  tests/unit/test_browser_rendered_runtime.py
```

The S48 browser-controller PostgreSQL workflow passed on `0b4b51dfa`, but the
repository-wide verification workflow on `4c52d8b` remains red, with a Ruff
report of 380 findings on 1 November 2026. Do not represent that as a passing
release. The older `tests/integration/test_execution_provenance.py` still
installs host callbacks, which current sandbox worker admission rejects. Its
replacement needs real admitted child execution and a PostgreSQL transcript;
removing the test, silently allowing host callbacks, or marking it skipped is
not an acceptable completion path.

The actual Linux/WSL2 operator must separately qualify two registered agents'
leased Xpra sessions, cookie/socket isolation, stream epochs and stale frames,
TLS/DNS egress denial, operator death/restart fencing, process-tree shutdown,
audit and PostgreSQL artifact readback. CI-rendered Chromium is not a surrogate
for these host gates. All missing/failed required evidence remains incomplete.
