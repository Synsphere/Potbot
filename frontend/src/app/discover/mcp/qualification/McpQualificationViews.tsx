"use client";

import type { QualificationSuite, QualificationAttempt, QualificationReport, QualificationCaseResult, QualificationDecision } from "../../../../lib/mcp-qualification.ts";
import { QUALIFICATION_ATTEMPT_LABELS, qualificationReportCurrent } from "../../../../lib/mcp-qualification-client.ts";
import styles from "../../../skills/skills.module.css";

export type QualificationMutate = (path: string[], body: QualificationDecision) => void;
export const readable = (value: string) => value.replaceAll("_", " ");
const at = (value: string) => new Date(value).toLocaleString();

export function SuiteReview({ suite, locked, current, mutate }: { suite: QualificationSuite; locked: boolean; current: boolean; mutate: QualificationMutate }) {
  const labels = { qualify: "Request one qualification attempt", enable: "Enable suite", disable: "Disable suite", revoke: "Revoke suite permanently" };
  return <section aria-label="Selected MCP qualification suite"><div className={styles.eyebrow}>ADMITTED TASK SUITE</div><h2>{suite.name}</h2><p>{suite.description || "No suite description retained."}</p>
    <div className={styles.badges}><span className={styles.badge}>{readable(suite.state)}</span><span className={styles.badge}>{readable(suite.transport)}</span><span className={styles.badge}>{suite.approvalValid ? "Signature current" : "Admission requires attention"}</span><span className={styles.badge}>{current && suite.authorityCurrent ? "Stored authority current" : "Authority requires revalidation"}</span></div>
    <dl className={styles.facts}><dt>Configured tool</dt><dd>{suite.toolName}</dd><dt>Agent</dt><dd><code>{suite.agentId}</code></dd><dt>Admitted cases</dt><dd>{suite.cases.length}</dd><dt>Revision</dt><dd>{suite.revision}</dd><dt>Updated</dt><dd>{at(suite.updatedAt)}</dd></dl>
    {!current && <p className={styles.warning}>Refresh the suite before reviewing another decision.</p>}
    {suite.reasonCodes.length > 0 && <p className={styles.help}>{suite.reasonCodes.map(readable).join(" · ")}</p>}
    {suite.state === "revoked" && <p className={styles.help}>Revocation is terminal. A replacement requires a new explicit admission on the configured host.</p>}
    <div className={styles.actions}>{suite.actions.map(action => <button type="button" key={action} className={action === "qualify" ? styles.primary : action === "revoke" ? styles.danger : undefined}
      disabled={locked || !current || action === "qualify" && !suite.authorityCurrent} onClick={() => mutate(["suites", suite.id, action], { revision: suite.revision, digest: suite.bindingSha256, confirm: true })}>{labels[action]}</button>)}</div>
    <p className={styles.help}>A qualification request records one attempt for the configured host. Native execution still checks the exact runtime, profile and existing task authority.</p>
    <section className={styles.section} aria-label="Admitted task cases"><h3>Cases in admitted order</h3><ol className={styles.history}>{suite.cases.map(item => <li key={item.id}><strong>{item.title}</strong><p><code>{item.id}</code> · {item.assertionCount} declared assertions · {item.toolName}</p>
      <details className={styles.details}><summary>Exact case identities</summary><dl className={styles.facts}><dt>Case</dt><dd><code>{item.caseSha256}</code></dd><dt>Arguments digest</dt><dd><code>{item.argumentsSha256}</code></dd><dt>Oracle digest</dt><dd><code>{item.oracleSha256}</code></dd></dl></details></li>)}</ol></section>
    <details className={styles.details}><summary>Task, source and installed-owner binding</summary><pre>{JSON.stringify({ id: suite.id, bindingId: suite.bindingId, bindingSha256: suite.bindingSha256,
      suiteSha256: suite.suiteSha256, suiteFileSha256: suite.suiteFileSha256, configurationSha256: suite.configurationSha256, installationSha256: suite.installationSha256, consumerSha256: suite.consumerSha256, taskBinding: suite.taskBinding }, null, 2)}</pre></details>
  </section>;
}

function CaseEvidence({ cases }: { cases: QualificationCaseResult[] }) {
  return <ol className={styles.history}>{cases.map(item => <li key={item.id}><div className={styles.row}><strong>{item.title}</strong><span className={styles.badge}>{readable(item.status)}</span></div>
    <p><code>{item.id}</code>{item.elapsedMs !== null ? ` · ${item.elapsedMs.toLocaleString()} ms` : ""}{item.reasonCode ? ` · ${readable(item.reasonCode)}` : ""}</p>
    {item.checks && <ul>{item.checks.map(check => <li key={check.index}>Assertion {check.index + 1}: {check.passed ? "passed" : readable(check.reasonCode ?? "failed")}</li>)}</ul>}
    <details className={styles.details}><summary>Retained run, result and receipt identities</summary><pre>{JSON.stringify({ caseSha256: item.caseSha256, actionId: item.actionId, candidateId: item.candidateId,
      reservationId: item.reservationId, runId: item.runId, resultArtifactId: item.resultArtifactId, resultSha256: item.resultSha256, receiptSha256: item.receiptSha256, oracleSha256: item.oracleSha256 }, null, 2)}</pre></details>
  </li>)}</ol>;
}

export function QualificationAttemptReview({ attempt, locked, current, mutate }: { attempt: QualificationAttempt; locked: boolean; current: boolean; mutate: QualificationMutate }) {
  return <section className={styles.section} aria-label="Qualification attempt"><div className={styles.eyebrow}>TASK QUALIFICATION ATTEMPT</div><h3>{QUALIFICATION_ATTEMPT_LABELS[attempt.status]}</h3>
    <dl className={styles.facts}><dt>Attempt</dt><dd><code>{attempt.id}</code></dd><dt>Requested</dt><dd>{at(attempt.createdAt)}</dd><dt>Updated</dt><dd>{at(attempt.updatedAt)}</dd><dt>Progress</dt><dd>{attempt.completedCases} of {attempt.caseCount} cases finished · {attempt.passedCases} passed · {attempt.failedCases} failed</dd><dt>Outcome</dt><dd>{attempt.reasonCode ? readable(attempt.reasonCode) : "No retained failure"}</dd></dl>
    {attempt.status === "requested" && <p className={styles.notice}>The configured host must run this exact attempt. Loading or refreshing this page does not dispatch a tool call.</p>}
    {attempt.status === "recovering" && <p className={styles.help}>The host is reconciling existing run and stopped-receipt evidence. This view only reads its retained progress.</p>}
    {attempt.status === "indeterminate" && <p className={styles.warning}>The outcome requires native inspection. Uncertain task cases are not automatically repeated.</p>}
    {attempt.recoveryAvailable && <p className={styles.notice}>Receipt reconciliation is available through the host CLI. It is a separate native recovery action.</p>}
    {!current && <p className={styles.warning}>Refresh the attempt before making another decision.</p>}
    <div className={styles.actions}>{attempt.actions.includes("cancel") && <button className={styles.danger} disabled={locked || !current} onClick={() => mutate(["attempts", attempt.id, "cancel"], { revision: attempt.revision, digest: attempt.bindingSha256, confirm: true })}>Cancel and fence this attempt</button>}</div>
    <p className={styles.help}>Cancellation fences future work. The native lifetime owner establishes whether an existing process has stopped.</p>
    <CaseEvidence cases={attempt.cases} />
  </section>;
}

export function QualificationReportReview({ report, suite, current }: { report: QualificationReport; suite: QualificationSuite; current: boolean }) {
  const authority = current && qualificationReportCurrent(report, suite);
  return <section className={styles.section} aria-label="Retained qualification report"><div className={styles.eyebrow}>RETAINED TASK EVIDENCE</div><h3>{report.passed ? "Every admitted case passed" : report.complete ? "Completed with failed cases" : "Incomplete retained evidence"}</h3>
    <div className={styles.badges}><span className={styles.badge}>{report.complete ? "All case outcomes retained" : "Partial report"}</span><span className={styles.badge}>{report.approvalValid ? "Report signature current" : "Report signature invalid"}</span><span className={styles.badge}>{authority ? "Stored authority current" : "Historical or stale authority"}</span></div>
    <dl className={styles.facts}><dt>Report</dt><dd><code>{report.id}</code></dd><dt>Attempt</dt><dd><code>{report.attemptId}</code></dd><dt>Recorded</dt><dd>{at(report.recordedAt)}</dd><dt>Observation</dt><dd>{at(report.evidence.observedAt)} – {at(report.evidence.completedAt)}</dd><dt>Report digest</dt><dd><code>{report.sha256}</code></dd></dl>
    {!current && <p className={styles.warning}>Refresh this report and its suite to inspect current stored authority.</p>}
    {report.reasonCodes.length > 0 && <p className={styles.help}>{report.reasonCodes.map(readable).join(" · ")}</p>}
    <p className={styles.help}>This report records the exact admitted cases and observed native identities. Each future execution still requires current task authority and native runtime/profile checks.</p>
    <CaseEvidence cases={report.cases} />
    <details className={styles.details}><summary>Inspect the retained native evidence binding</summary><pre>{JSON.stringify(report.evidence, null, 2)}</pre></details>
  </section>;
}
