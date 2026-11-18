"use client";

import type { TaskDiscoveryContext, TaskDiscoverySearch, TaskDiscoveryResult, TaskDiscoverySelection, RankedTaskCandidate } from "../../../lib/task-discovery.ts";
import { taskCanSelect, taskHandoffHref, taskOwnerHref } from "../../../lib/task-discovery-client.ts";
import styles from "../../skills/skills.module.css";

export function TaskJSON({ value, title }: { value: unknown; title: string }) {
  return <details className={styles.details}><summary>{title}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>;
}

export function TaskContextView({ context }: { context: TaskDiscoveryContext }) {
  return <section className={styles.notice} aria-label="Reviewed task context"><h3>Current task snapshot</h3>
    <dl className={styles.facts}><dt>Task</dt><dd><code>{context.taskBinding.episodeId}</code></dd><dt>Repository state</dt><dd><code>{context.taskBinding.repositoryStateId}</code></dd>
      <dt>Context digest</dt><dd><code>{context.contextSha256}</code></dd><dt>Task capabilities</dt><dd>{context.taskCapabilities.join(", ") || "None"}</dd>
      <dt>Candidate sources</dt><dd>{context.counts.registry} registry · {context.counts.operations} operations · {context.counts.mcp} MCP references</dd></dl>
    <p className={styles.help}>Constraints can narrow this task contract. Source, policy or task changes require another context review before creating a search.</p>
    <TaskJSON title="Exact task, policy and source identities" value={context} />
  </section>;
}

function CandidateView({ item, index, workspace, search, result, disabled, inspect, select }: { item: RankedTaskCandidate; index: number; workspace: string; search: TaskDiscoverySearch; result: TaskDiscoveryResult; disabled: boolean; inspect: (item: RankedTaskCandidate) => void; select: (item: RankedTaskCandidate) => void }) {
  const href = taskOwnerHref(workspace, item), evidence = item.taskEvidence;
  const suiteHref = evidence.suiteId && item.kind === "mcp" ? `/discover/mcp/qualification?${new URLSearchParams({ workspace, suite: evidence.suiteId })}` : null;
  return <article className={styles.section} aria-label={`Candidate ${index + 1}: ${item.name}`}>
    <div className={styles.row}><h3>{index + 1}. {item.name}</h3><span className={styles.badge}>{item.kind}</span></div>
    <p className={styles.muted}>{item.description || "No description was supplied."}</p>
    <dl className={styles.facts}>
      <dt>Hard constraints</dt><dd>Type fit: {item.typeFit.replaceAll("_", " ")} · task permission: {item.taskPermission}</dd>
      <dt>Owner authority</dt><dd>{item.authorityCurrent ? result.authorityCurrent && search.authorityCurrent ? "Current observation" : "Current when retained; search is now historical" : "Historical or held"}{item.reasonCodes.length > 0 && <ul>{item.reasonCodes.map(code => <li key={code}><code>{code}</code></li>)}</ul>}</dd>
      <dt>Installed</dt><dd>{item.installed ? "Yes, as observed" : "No installed observation"}</dd>
      <dt>Required capability</dt><dd><code>{item.requiredTaskCapability ?? "Not declared"}</code></dd>
      <dt>Effects / scopes</dt><dd>{item.capabilities.effects.join(", ") || "None declared"} / {item.capabilities.scopes.join(", ") || "None declared"}</dd>
      <dt>Task evidence</dt><dd>{!result.authorityCurrent && "At recording: "}{evidence.status.replaceAll("_", " ")}{evidence.status !== "none" && ` · ${evidence.passedCases}/${evidence.caseCount} cases passed`}{suiteHref && <> · <a className={styles.link} href={suiteHref}>Inspect qualification suite ↗</a></>}</dd>
      <dt>Ranking score</dt><dd>{item.score} · classes {item.features.classes}, lexical {item.features.lexical}, types {item.features.types}, installed {item.features.installed}, evidence {item.features.evidence}, semantic {item.features.semantic}</dd>
      <dt>Explanation</dt><dd><ul>{item.reasons.map(reason => <li key={reason}><code>{reason}</code></li>)}</ul></dd>
      <dt>Execution grant</dt><dd>None · executable: false</dd>
    </dl>
    <p className={styles.help}>Cost, latency and general quality are unknown unless an existing owner supplies separate evidence. A retrieval score is not a success measurement.</p>
    <TaskJSON title="Candidate identity, declared types and exact evidence" value={item} />
    <div className={styles.actions}><button disabled={disabled} onClick={() => inspect(item)}>Inspect current owner detail</button>
      {href && <a className={styles.link} href={href}>Open existing {item.kind} review ↗</a>}
      <button className={styles.primary} disabled={disabled || !taskCanSelect(search, result, item)} onClick={() => select(item)}>Review selection</button></div>
  </article>;
}

export function TaskResultView({ workspace, search, result, selection, disabled, inspect, select }: { workspace: string; search: TaskDiscoverySearch; result: TaskDiscoveryResult; selection: TaskDiscoverySelection | null; disabled: boolean; inspect: (item: RankedTaskCandidate) => void; select: (item: RankedTaskCandidate) => void }) {
  const handoff = selection && taskHandoffHref(workspace, selection);
  return <section aria-label="Explained shortlist">
    <h3>Explained shortlist</h3><p className={styles.muted}>Hard exclusions apply before ranking. Only matching current task evidence can improve the evidence score.</p>
    {(!result.authorityCurrent || !result.approvalValid) && <div className={styles.uncertain}><strong>This retained result is historical or no longer approved.</strong><p>Selection is unavailable. Review the current task context and create a fresh search.</p><ul>{result.reasonCodes.map(code => <li key={code}><code>{code}</code></li>)}</ul></div>}
    {result.noSuitableCandidate && <p className={styles.notice}>No suitable candidate was found within these constraints and retained observations. Review the exclusions and existing source owners before changing the request.</p>}
    {result.items.map((item, index) => <CandidateView key={item.id} item={item} index={index} workspace={workspace} search={search} result={result} disabled={disabled} inspect={inspect} select={select} />)}
    <details className={styles.details}><summary>Hard exclusions ({result.excluded.length})</summary>{result.excluded.length ? <ul className={styles.history}>{result.excluded.map(item => <li key={item.id}><code>{item.id}</code><ul>{item.reasonCodes.map(code => <li key={code}><code>{code}</code></li>)}</ul></li>)}</ul> : <p>No hard exclusions were recorded.</p>}</details>
    {result.semantic && <section className={styles.notice}><h4>Retained native retrieval usage</h4><p>{result.semantic.candidateCount} candidate documents · {result.semantic.calls} embedding calls · {result.semantic.inputTokens} input tokens · {result.semantic.elapsedMs} ms · {result.semantic.usageOrigin.replaceAll("_", " ")}</p><TaskJSON title="Exact embedding profile and budget reservations" value={result.semantic} /></section>}
    {result.mode === "hybrid" && result.semantic === null && <p className={styles.help}>No eligible semantic pool remained; this empty result required no native inference.</p>}
    {selection && <section className={styles.notice}><h3>Retained selection</h3><p>Selection records the exact candidate and its existing review owner. It grants no execution permission.</p><p><code>{selection.id}</code></p>
      {handoff ? <a className={styles.link} href={handoff}>Continue in the existing {selection.handoff.kind} review ↗</a> : <><p>This selection is historical or held; refresh the task and candidate before continuing.</p><ul>{selection.reasonCodes.map(code => <li key={code}><code>{code}</code></li>)}</ul></>}
      <TaskJSON title="Exact selection receipt and owner identity" value={selection} /></section>}
    <TaskJSON title="Complete retained result and source identities" value={result} />
  </section>;
}
