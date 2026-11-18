"use client";

import type { TaskExecutionContext, TaskExecutionJob, TaskExecutionResult } from "../../lib/task-execution.ts";
import { MAX_ARTIFACT_DOWNLOAD_BYTES } from "../../lib/operation-workbench-download.ts";
import styles from "./workbench.module.css";

export function SelectedJSON({ title, value }: { title: string; value: unknown }) { return <details className={styles.details}><summary>{title}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>; }

export function SelectedContextView({ context }: { context: TaskExecutionContext }) {
  return <section className={styles.result} aria-label="Selected task context"><h4>{context.operation?.name ?? "Selected capability"}</h4>
    <dl><dt>Selection</dt><dd><code>{context.selectionId}</code></dd><dt>Selection digest</dt><dd><code>{context.selectionSha256}</code></dd><dt>Task</dt><dd><code>{context.taskBinding.episodeId}</code></dd><dt>State</dt><dd><code>{context.taskBinding.repositoryStateId}</code></dd><dt>Currentness</dt><dd>{context.current ? "Current selection and task evidence" : "Historical or held; a new check or launch is unavailable"}</dd><dt>Execution route</dt><dd>{context.supported ? context.operation?.jobType : "No supported operation route for this selection"}</dd></dl>
    {context.reasonCodes.length > 0 && <ul>{context.reasonCodes.map(code => <li key={code}><code>{code}</code></li>)}</ul>}
    {context.operation?.reasonCodes.length ? <p className={styles.help}>{context.operation.reasonCodes.join(" · ")}</p> : null}
    <p className={styles.help}>The discovery selection identifies an existing owner. Check and launch remain separate decisions under the task’s current permissions, runtime and budget.</p>
    <SelectedJSON title="Exact task, source owner and execution contract" value={context} />
  </section>;
}

export function SelectedCheckView({ entry }: { entry: TaskExecutionJob }) {
  const check = entry.job.result?.operation_check;
  return <section className={styles.checkReport}><h4>Worker check</h4>{check ? <>
    <p>{check.eligible ? "This exact request passed the worker’s bounded check." : "This exact request is blocked or unsupported."} Launch rechecks the current selection and worker authority.</p>
    <ul>{check.checks.map(item => <li key={item.code}><span className={item.status === "passed" ? styles.passed : styles.blocked}>{item.status}</span><code>{item.code}</code></li>)}</ul>
    <p className={styles.help}>Checked {check.checkedAt} · expires {check.expiresAt}</p><SelectedJSON title="Exact checked runtime and request bindings" value={check} />
  </> : <p className={styles.help}>No completed worker check is retained for this job yet. A queued or successful job flag alone does not authorize launch.</p>}</section>;
}

export function SelectedOutcomeView({ result, downloading, download }: { result: TaskExecutionResult; downloading: boolean; download: () => void }) {
  const outcome = result.verification, artifact = result.result.artifact;
  const labels = { pending: "Verification pending", passed: "Declared assertions passed", failed: "Declared assertions failed", unavailable: "Verification unavailable" };
  return <section className={styles.result} aria-label="Execution and assertion outcomes"><h4>Execution outcome: {result.result.job.state}</h4>
    <p className={styles.help}>Execution status describes the retained job. The independent assertion result below evaluates complete owned output and its execution/accounting evidence.</p>
    <section className={styles.checkReport} aria-label="Declared assertion verification"><h4>{labels[outcome.status]}</h4>
      <p className={styles.help}>A pass applies only to the fixed assertions reviewed before the check. It does not mark the whole task complete or qualify a deployed host or model.</p>
      {outcome.reasonCodes.length > 0 && <ul>{outcome.reasonCodes.map(code => <li key={code}><code>{code}</code></li>)}</ul>}
      {outcome.checks.length > 0 && <ul>{outcome.checks.map(check => { const assertion = result.binding.acceptance.assertions[check.index]; return <li key={check.index}><span className={check.passed ? styles.passed : styles.blocked}>{check.passed ? "Passed" : "Failed"}</span><span>{check.index + 1}. <code>{assertion.op} {assertion.pointer || "(root)"}</code>{check.reasonCode && <> · <code>{check.reasonCode}</code></>}</span></li>; })}</ul>}
      <dl><dt>Input binding</dt><dd><code>{outcome.bindingSha256}</code></dd><dt>Assertions</dt><dd><code>{outcome.acceptanceSha256}</code></dd><dt>Complete output</dt><dd><code>{outcome.outputSha256 ?? "Unavailable"}</code></dd><dt>Owned evidence</dt><dd><code>{outcome.evidenceSha256 ?? "Unavailable"}</code></dd></dl>
      <SelectedJSON title="Exact verification record" value={outcome} />
    </section>
    <h4>Retained output preview</h4>{result.result.output ? <pre tabIndex={0}>{result.result.output}</pre> : <p className={styles.help}>No retained text preview.</p>}
    {result.result.truncated && <p className={styles.help}>This preview is truncated. It is not the input to assertion verification.</p>}
    {result.result.artifactText !== null && <><h4>Native result preview</h4><pre tabIndex={0}>{result.result.artifactText}</pre>{result.result.artifactTruncated && <p className={styles.help}>This native preview is truncated; verification requires the complete owned result within its independent bound.</p>}</>}
    {artifact && <><dl><dt>Artifact</dt><dd><code>{artifact.id}</code></dd><dt>SHA-256</dt><dd><code>{artifact.sha256}</code></dd><dt>Size / type</dt><dd>{artifact.bytes.toLocaleString()} bytes · {artifact.mediaType}</dd></dl><button disabled={downloading || artifact.bytes > MAX_ARTIFACT_DOWNLOAD_BYTES} onClick={download}>{downloading ? "Reading and verifying artifact…" : "Download verified owned artifact"}</button>{artifact.bytes > MAX_ARTIFACT_DOWNLOAD_BYTES && <p className={styles.help}>This artifact exceeds the browser’s 16 MiB download bound. Use the existing artifact owner to retrieve it.</p>}</>}
    <SelectedJSON title="Retained execution, invocation and accounting metadata" value={result.result.job} />
  </section>;
}
