"use client";

/** A recoverable guard for unexpected client rendering exceptions. */
export default function ErrorPage({ reset }: { error: Error & {digest?: string}; reset: () => void }) {
  return <main style={{maxWidth: 640, margin: "12vh auto", padding: 24, fontFamily: "system-ui"}}>
    <h1>Potbot workspace could not render</h1>
    <p>One view failed, but backend jobs may still be running. Retrying this display does not resubmit work.</p>
    <button type="button" onClick={reset}>Retry display</button>
    <p><a href="/">Return to workspace</a></p>
  </main>;
}
