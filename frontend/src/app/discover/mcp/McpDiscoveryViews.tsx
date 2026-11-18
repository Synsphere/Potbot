"use client";

import { useState } from "react";
import type { McpBinding, McpEntry, McpAttempt, McpReference, McpDecision } from "../../../lib/mcp-discovery.ts";
import { MCP_ATTEMPT_LABELS, mcpBindingCurrent, mcpCanReference, mcpReferenceCurrent } from "../../../lib/mcp-discovery-client.ts";
import styles from "../../skills/skills.module.css";

export type McpMutate = (path: string[], body: McpDecision) => void;
export const readable = (value: string) => value.replaceAll("_", " ");
const short = (value: string) => value.slice(0, 16);
const at = (value: string | null) => value ? new Date(value).toLocaleString() : "Not observed";

export function BindingReview({ binding, locked, current, now, mutate }: { binding: McpBinding; locked: boolean; current: boolean; now: number; mutate: McpMutate }) {
  const fresh = mcpBindingCurrent(binding, now);
  const actions = { reconcile: "Request exact reconciliation", enable: "Enable discovery binding", disable: "Disable discovery binding", revoke: "Revoke discovery binding permanently" };
  return <section aria-label="Selected MCP binding"><div className={styles.eyebrow}>ADMITTED INSTALLATION BINDING</div><h2>{binding.name}</h2>
    <div className={styles.badges}><span className={styles.badge}>{readable(binding.state)}</span><span className={styles.badge}>{readable(binding.transport)}</span><span className={styles.badge}>{fresh ? "Current observation" : "Reference unavailable"}</span><span className={styles.badge}>{binding.approvalValid ? "Signature current" : "Admission requires attention"}</span></div>
    <p className={styles.muted}>This binding observes the configured tool <strong>{binding.toolName}</strong> and its server’s declared interfaces. Existing task and installation policies govern invocation.</p>
    <dl className={styles.facts}><dt>Agent</dt><dd><code>{binding.agentId}</code></dd><dt>Observed</dt><dd>{at(binding.fetchedAt)}</dd><dt>Expires</dt><dd>{at(binding.expiresAt)}</dd><dt>Interfaces</dt><dd>{binding.counts.tools} tools · {binding.counts.resources} resources · {binding.counts.resourceTemplates} templates · {binding.counts.prompts} prompts</dd>
      <dt>Protocol</dt><dd>{binding.catalogInfo?.protocolVersion ?? "Awaiting reconciliation"}</dd><dt>Latest outcome</dt><dd>{binding.lastErrorCode ? readable(binding.lastErrorCode) : "No retained error"}</dd></dl>
    {!current && <p className={styles.warning}>Refresh the binding before making a decision or copying a reference.</p>}
    {binding.state === "revoked" && <p className={styles.help}>Revocation is terminal. A replacement requires a new explicit admission on the configured host.</p>}
    {!binding.approvalValid && binding.state !== "revoked" && <p className={styles.help}>Current signature evidence is unavailable. Terminal revocation retires this binding; the host operator can admit a replacement.</p>}
    <div className={styles.actions}>{binding.actions.map(action => <button type="button" key={action} className={action === "reconcile" ? styles.primary : action === "revoke" ? styles.danger : undefined} disabled={locked || !current}
      onClick={() => mutate(["bindings", binding.id, action], { revision: binding.revision, digest: binding.bindingSha256, confirm: true })}>{actions[action]}</button>)}</div>
    <p className={styles.help}>Reconciliation records one request for the configured host to run. Discovery controls fence future references; installed execution and release controls retain their own authority.</p>
    <details className={styles.details}><summary>Exact revision and installation identity</summary><dl className={styles.facts}>
      <dt>Binding</dt><dd><code>{binding.id}</code></dd><dt>Revision</dt><dd>{binding.revision}</dd><dt>Binding digest</dt><dd><code>{binding.bindingSha256}</code></dd>
      <dt>Installation</dt><dd><code>{binding.installationSha256}</code></dd><dt>Configuration</dt><dd><code>{binding.configurationSha256}</code></dd><dt>Definition</dt><dd><code>{binding.definitionSha256}</code></dd><dt>Configured tool</dt><dd><code>{binding.toolFingerprint}</code></dd>
      <dt>Snapshot</dt><dd><code>{binding.snapshotId ?? "None"}</code></dd><dt>Snapshot digest</dt><dd><code>{binding.snapshotSha256 ?? "None"}</code></dd><dt>Operator TTL</dt><dd>{binding.ttlSeconds} seconds</dd><dt>Watch window</dt><dd>{binding.watchMs} milliseconds</dd>
    </dl></details>
    {binding.catalogInfo && <details className={styles.details}><summary>Advertised server identity, capabilities and collection support</summary><p className={styles.help}>These are retained peer claims. URLs, icons, resource addresses and metadata are displayed as data.</p><pre>{JSON.stringify(binding.catalogInfo, null, 2)}</pre></details>}
  </section>;
}

export function AttemptReview({ attempt, locked, current, mutate }: { attempt: McpAttempt; locked: boolean; current: boolean; mutate: McpMutate }) {
  return <section className={styles.section} aria-label="Reconciliation attempt"><div className={styles.eyebrow}>RECONCILIATION ATTEMPT</div><h3>{MCP_ATTEMPT_LABELS[attempt.status]}</h3>
    <dl className={styles.facts}><dt>Attempt</dt><dd><code>{attempt.id}</code></dd><dt>Requested</dt><dd>{at(attempt.createdAt)}</dd><dt>Updated</dt><dd>{at(attempt.updatedAt)}</dd><dt>Revision</dt><dd>{attempt.revision}</dd><dt>Outcome</dt><dd>{attempt.reasonCode ? readable(attempt.reasonCode) : "No reported failure"}</dd></dl>
    {attempt.status === "requested" && <p className={styles.notice}>The configured host must run this exact retained attempt. Loading this page or refreshing its status does not dispatch a process or contact an MCP server.</p>}
    {attempt.status === "running" && <p className={styles.help}>The host is reconciling the admitted installation. Cancellation fences this attempt; it does not assert that a native process stopped.</p>}
    {attempt.status === "indeterminate" && <p className={styles.warning}>The outcome requires inspection through the existing host owners. This attempt will not be automatically replayed.</p>}
    {attempt.changes && <div className={styles.badges}><span className={styles.badge}>{attempt.changes.added} added</span><span className={styles.badge}>{attempt.changes.removed} removed</span><span className={styles.badge}>{attempt.changes.changed} changed</span><span className={styles.badge}>{attempt.changes.unchanged} unchanged</span></div>}
    <div className={styles.actions}>{attempt.actions.includes("cancel") && <button className={styles.danger} disabled={locked || !current} onClick={() => mutate(["attempts", attempt.id, "cancel"], { revision: attempt.revision, digest: attempt.bindingSha256, confirm: true })}>Cancel and fence this attempt</button>}</div>
    <details className={styles.details}><summary>Exact attempt binding</summary><pre>{JSON.stringify({ bindingId: attempt.bindingId, requestedBindingSha256: attempt.requestedBindingSha256, bindingSha256: attempt.bindingSha256, installationSha256: attempt.installationSha256, configurationSha256: attempt.configurationSha256, snapshotId: attempt.snapshotId, snapshotSha256: attempt.snapshotSha256 }, null, 2)}</pre></details>
  </section>;
}

export function EntryReview({ entry, binding, reference, now, current, busy, notice, inspectReference }: { entry: McpEntry; binding: McpBinding; reference: McpReference | null; now: number; current: boolean; busy: boolean; notice: string; inspectReference: () => void }) {
  const [copyNotice, setCopyNotice] = useState("");
  const canReference = current && mcpCanReference(entry, binding, now), referenceCurrent = !!reference && current && mcpReferenceCurrent(reference, entry, binding, now);
  return <section className={styles.section} aria-label="Selected advertised interface"><div className={styles.eyebrow}>{readable(entry.kind).toUpperCase()} / RETAINED DEFINITION</div><h3>{entry.name}</h3>
    <p>{entry.description || "No description advertised."}</p><div className={styles.badges}><span className={styles.badge}>{entry.configuredTool ? "Configured tool identity" : "Advertised interface"}</span><span className={styles.badge}>{canReference ? "Reference current" : "Reference unavailable"}</span><span className={styles.badge}>No invocation grant</span></div>
    <dl className={styles.facts}><dt>Identity</dt><dd><code>{entry.identity}</code></dd><dt>Definition digest</dt><dd><code>{entry.definitionSha256}</code></dd><dt>Snapshot digest</dt><dd><code>{short(entry.snapshotSha256)}…</code></dd></dl>
    {entry.reasonCodes.length > 0 && <p className={styles.help}>{entry.reasonCodes.map(readable).join(" · ")}</p>}
    <details className={styles.details}><summary>Inspect the retained schema and metadata</summary><pre>{JSON.stringify(entry.definition, null, 2)}</pre></details>
    <button disabled={busy || !canReference} onClick={inspectReference}>Inspect exact reference</button>{notice && <p className={styles.status} role="status">{notice}</p>}
    {reference && <div className={styles.section}><h4>Exact catalogue reference</h4><p className={styles.help}>This identifies the reviewed definition and its expiry. It grants no permission to call a tool, read a resource or expand a prompt.</p>
      {!referenceCurrent && <p className={styles.warning}>This reference has expired or its current binding changed. Refresh and inspect a new exact reference.</p>}
      <pre>{JSON.stringify(reference, null, 2)}</pre><button disabled={!referenceCurrent} onClick={() => {
        if (!mcpReferenceCurrent(reference, entry, binding, Date.now())) { setCopyNotice("The reference is no longer current. Refresh it before copying."); return; }
        if (!navigator.clipboard) { setCopyNotice("Clipboard access is unavailable; the reference remains visible above."); return; }
        void navigator.clipboard.writeText(JSON.stringify(reference, null, 2)).then(() => setCopyNotice("Exact reference copied.")).catch(() => setCopyNotice("Clipboard access is unavailable; the reference remains visible above."));
      }}>Copy exact reference</button>{copyNotice && <p className={styles.status} role="status">{copyNotice}</p>}</div>}
  </section>;
}
