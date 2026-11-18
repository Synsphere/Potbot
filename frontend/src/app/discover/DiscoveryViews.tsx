"use client";

import { Fragment, useEffect, useState, type FormEvent } from "react";
import { UUID, CANDIDATE_KINDS, PLATFORMS, EFFECTS, discoveryControlBody,
  type DiscoveryCandidate, type DiscoveryCheck, type DiscoveryPublisher, type DiscoverySource, type RegistryObservation, type Requirement } from "../../lib/discovery.ts";
import { acquisitionReviewLink } from "../../lib/acquisition-handoff.ts";
import { CHECK_LABELS, DISCOVERY_ENDPOINT, discoveryCanHandoff, discoveryMeasurementCurrent, type SearchDraft } from "../../lib/discovery-client.ts";
import styles from "../skills/skills.module.css";

export type DiscoveryMutate = (path: string, body: Record<string, unknown>) => void;
export const readable = (value: string) => value.replaceAll("_", " ");
export const short = (value: string) => value.slice(0, 12);

export function SearchForm({ value, sources, disabled, change, submit }: { value: SearchDraft; sources: DiscoverySource[]; disabled: boolean; change: (value: SearchDraft) => void; submit: () => void }) {
  const field = <K extends keyof SearchDraft>(key: K, next: SearchDraft[K]) => change({ ...value, [key]: next });
  return <form className={styles.form} onSubmit={event => { event.preventDefault(); submit(); }}><fieldset className={styles.form} disabled={disabled}>
    <label className={styles.field}>Task or capability<input value={value.q} maxLength={1000} placeholder="Inspect a repository or analyze a document" onChange={event => field("q", event.target.value)} /></label>
    <label className={styles.field}>Source<select value={value.sourceId} onChange={event => field("sourceId", event.target.value)}><option value="">All retained sources</option>{sources.map(source => <option key={source.id} value={source.id}>{source.name}</option>)}</select></label>
    <label className={styles.field}>Package kind<select value={value.kind} onChange={event => field("kind", event.target.value)}><option value="">All package kinds</option>{CANDIDATE_KINDS.map(kind => <option key={kind} value={kind}>{readable(kind)}</option>)}</select></label>
    <label className={styles.field}>Required platform<select value={value.platform} onChange={event => field("platform", event.target.value)}><option value="">No platform constraint</option>{PLATFORMS.map(platform => <option key={platform} value={platform}>{platform}</option>)}</select></label>
    <label className={styles.check}><input type="checkbox" checked={value.local} onChange={event => field("local", event.target.checked)} /><span>Require a local distribution</span></label>
    <label className={styles.check}><input type="checkbox" checked={value.restrictEffects} onChange={event => field("restrictEffects", event.target.checked)} /><span>Limit permitted effects</span></label>
    {value.restrictEffects && <div className={styles.form}>{EFFECTS.map(effect => <label key={effect} className={styles.check}><input type="checkbox" checked={value.effects.includes(effect)} onChange={event => field("effects", event.target.checked ? [...value.effects, effect] : value.effects.filter(item => item !== effect))} /><span>{readable(effect)}</span></label>)}<p className={styles.help}>Selecting none excludes candidates that declare any effect.</p></div>}
    <details className={styles.details}><summary>Advanced semantic and scope constraints</summary><div className={styles.form}>
      <label className={styles.check}><input type="checkbox" checked={value.restrictScopes} onChange={event => field("restrictScopes", event.target.checked)} /><span>Limit permitted scopes</span></label>
      {value.restrictScopes && <label className={styles.field}>Permitted scopes · comma separated<input value={value.scopes} maxLength={4096} onChange={event => field("scopes", event.target.value)} placeholder="repository-read, filesystem-read" /><span className={styles.help}>An empty list permits no declared scopes.</span></label>}
      <label className={styles.field}>Capability classes<input value={value.classes} maxLength={4096} placeholder="repository_inspection, code_search" onChange={event => field("classes", event.target.value)} /></label>
      <label className={styles.field}>Input Types<input value={value.inputTypes} maxLength={4096} placeholder="syn://type/repository" onChange={event => field("inputTypes", event.target.value)} /></label>
      <label className={styles.field}>Output Types<input value={value.outputTypes} maxLength={4096} placeholder="syn://type/text" onChange={event => field("outputTypes", event.target.value)} /></label>
      <p className={styles.help}>Use exact published class names and canonical SYN or Synny Type references. Each list permits up to 32 distinct entries. Constraints filter observations; they do not grant scopes or effects.</p>
    </div></details><button type="submit" className={styles.primary}>Search retained observations</button>
  </fieldset></form>;
}

export function Requirements({ items, title = "Declared requirements" }: { items: Requirement[]; title?: string }) {
  return <section className={styles.section}><h3>{title}</h3>{items.length ? <div className={styles.tableScroll}><table className={styles.table}><thead><tr><th scope="col">Kind</th><th scope="col">Declaration</th><th scope="col">Alternatives</th></tr></thead>
    <tbody>{items.map((item, index) => <tr key={`${item.kind}:${item.name}:${index}`}><td>{readable(item.kind)}</td><td><code>{item.name}</code></td><td>{item.group || ["any_binary", "operating_system"].includes(item.kind) ? "Any declared alternative in this clause" : "Required individually"}</td></tr>)}</tbody></table></div> : <p className={styles.muted}>No requirements are declared in this record. A selected sandbox measurement is still required.</p>}</section>;
}
function Observation({ value }: { value: RegistryObservation }) {
  return <><p>{value.description || "No description was published."}</p><dl className={styles.facts}><dt>Canonical identity</dt><dd><code>{value.canonicalId}</code></dd>
    <dt>Exact version</dt><dd><code>{value.version ?? "Unresolved"}</code></dd><dt>Registry state</dt><dd>{readable(value.status)}</dd><dt>Adapter</dt><dd>{readable(value.adapter)}</dd>
    <dt>Publisher claim</dt><dd><code>{value.publisher.id}</code> · {readable(value.publisher.evidence)}</dd><dt>Source</dt><dd>{value.sourceUrl.startsWith("https://") ? <a className={styles.link} href={value.sourceUrl} target="_blank" rel="noopener noreferrer">{value.sourceUrl}</a> : <code>{value.sourceUrl}</code>}</dd>
    <dt>Source SHA-256</dt><dd><code>{value.sourceSha256}</code></dd><dt>Manifest SHA-256</dt><dd><code>{value.manifestSha256}</code></dd>
    <dt>License declaration</dt><dd>{typeof value.manifest.license === "string" ? value.manifest.license : "Inspect the retained manifest; no plain-text license declaration is available."}</dd></dl>
    <section className={styles.section}><h3>Published capability claims</h3><dl className={styles.facts}>{Object.entries(value.capabilities).map(([key, items]) => <Fragment key={key}><dt>{readable(key)}</dt><dd>{items.length ? items.join(", ") : "Not declared"}</dd></Fragment>)}</dl>
      <p className={styles.help}>These are registry or publisher declarations. Source admission and a prerequisite measurement do not establish successful task execution.</p></section>
    {value.unsupported.length > 0 && <section className={styles.warning}><h3>Unsupported declarations</h3><ul>{value.unsupported.map(item => <li key={item}>{item}</li>)}</ul></section>}
    <details className={styles.details}><summary>Exact distribution declaration</summary><pre>{JSON.stringify(value.distribution, null, 2)}</pre></details>
    <details className={styles.details}><summary>Retained source manifest</summary><pre>{JSON.stringify(value.manifest, null, 2)}</pre></details></>;
}

export function CandidateReview({ candidate, disabled, mutate, admission }: { candidate: DiscoveryCandidate; disabled: boolean; mutate: DiscoveryMutate; admission: (sourceId: string) => void }) {
  const [agentId, setAgentId] = useState(""), [acknowledged, setAcknowledged] = useState("");
  const identity = `${candidate.id}:${candidate.snapshotId}:${candidate.candidateSha256}:${agentId}`;
  function review(event: FormEvent) {
    event.preventDefault(); if (disabled || !candidate.canReview || !UUID.test(agentId) || acknowledged !== identity) return;
    mutate(`${DISCOVERY_ENDPOINT}/candidates/${candidate.id}/checks`, { snapshotId: candidate.snapshotId, candidateSha256: candidate.candidateSha256, agentId, confirm: true });
  }
  return <article className={styles.body}><span className={styles.eyebrow}>REGISTRY OBSERVATION</span><h2>{candidate.observation.name}</h2><div className={styles.badges}>
    <span className={styles.badge}>{readable(candidate.observation.kind)}</span><span className={styles.badge}>{candidate.stale ? "Snapshot stale" : "Retained snapshot"}</span><span className={styles.badge}>Execution unavailable from discovery</span></div>
    <p className={styles.notice}>Inspect the exact version and source. This entry is an observation; it does not install a package, run a tool or grant permission.</p>
    <dl className={styles.facts}><dt>Candidate SHA-256</dt><dd><code>{candidate.candidateSha256}</code></dd><dt>Snapshot</dt><dd><code>{candidate.snapshotId}</code></dd>
      <dt>Source admission</dt><dd>{candidate.sourceState} · {candidate.sourceApprovalValid ? "valid operator decision" : "approval unavailable"}</dd><dt>Publisher admission</dt><dd>{candidate.publisherState} · {candidate.publisherApprovalValid ? "valid operator decision" : "approval unavailable"}</dd>
      {candidate.score !== null && <><dt>Search score</dt><dd>{candidate.score} · retained metadata match</dd></>}</dl>
    {candidate.scoreReasons.length > 0 && <details className={styles.details}><summary>Why this observation matched</summary><ul>{candidate.scoreReasons.map(reason => <li key={reason}>{reason}</li>)}</ul></details>}
    {candidate.reasonCodes.length > 0 && <ul className={styles.warning}>{candidate.reasonCodes.map(reason => <li key={reason}>{readable(reason)}</li>)}</ul>}
    <button type="button" onClick={() => admission(candidate.sourceId)}>Review source and publisher decisions</button>
    <Observation value={candidate.observation} /><Requirements items={candidate.observation.requirements} />
    <section className={styles.section}><h3>Review the selected exact version</h3><p className={styles.help}>A skill review retains and inspects its private package through the skill owner. An executable candidate must match an already published operator recipe. The host then measures prerequisites for the selected enrolled agent.</p>
      {candidate.canReview ? <form className={styles.form} onSubmit={review}><fieldset className={styles.form} disabled={disabled}>
        <label className={styles.field}>Enrolled agent UUID<input value={agentId} maxLength={36} pattern="[a-f0-9-]{36}" required placeholder="Exact registered agent identifier" autoComplete="off" spellCheck={false} onChange={event => { setAgentId(event.target.value); setAcknowledged(""); }} /><span className={styles.help}>Use the agent the host operator will measure. Tool recipes may be bound to their published agent.</span></label>
        <label className={styles.check}><input type="checkbox" checked={acknowledged === identity} onChange={event => setAcknowledged(event.target.checked ? identity : "")} /><span>I reviewed this exact source, version and publisher decision. Retain its package review and request an operator measurement of prerequisites.</span></label>
        <button type="submit" className={styles.primary} disabled={!UUID.test(agentId) || acknowledged !== identity}>Create exact review</button>
      </fieldset></form> : <p className={styles.warning}>This observation cannot enter acquisition review under its current identity, source admission or supported distribution. Refresh or resolve the stated reasons before trying again.</p>}
    </section></article>;
}

export function CheckReview({ check, disabled, mutate, workspaceId, refresh }: { check: DiscoveryCheck; disabled: boolean; mutate: DiscoveryMutate; workspaceId: string; refresh: () => void }) {
  const [acknowledged, setAcknowledged] = useState(""), [now, setNow] = useState(Date.now());
  const identity = `${check.id}:${check.revision}:${check.bindingSha256}`, current = discoveryMeasurementCurrent(check, now), canHandoff = discoveryCanHandoff(check, now);
  useEffect(() => { setNow(Date.now()); const expiry = check.measurement && Date.parse(check.measurement.expiresAt); if (!expiry || expiry <= Date.now()) return;
    const timer = window.setTimeout(() => setNow(Date.now()), Math.min(120_001, expiry - Date.now() + 1)); return () => window.clearTimeout(timer); }, [check.measurement?.expiresAt, check.revision]);
  function act(action: "handoff" | "cancel") {
    if (disabled || (action === "handoff" && (acknowledged !== identity || !discoveryCanHandoff(check)))) return;
    const body = discoveryControlBody(check, action); if (body) mutate(`${DISCOVERY_ENDPOINT}/checks/${check.id}/${action}`, body);
  }
  const target = check.target && acquisitionReviewLink(workspaceId, check.target.kind, check.target.id), report = check.measurement;
  return <article className={styles.body}><span className={styles.eyebrow}>EXACT REVIEW AND HOST MEASUREMENT</span><h2>{check.candidate.observation.name}</h2><div className={styles.badges}><span className={styles.badge}>{CHECK_LABELS[check.status]}</span><span className={styles.badge}>Revision {check.revision}</span></div>
    <dl className={styles.facts}><dt>Review</dt><dd><code>{check.id}</code></dd><dt>Selected agent</dt><dd><code>{check.agentId}</code></dd><dt>Registry selection SHA-256</dt><dd><code>{check.requestedCandidateSha256}</code></dd>
      <dt>Resolved observation SHA-256</dt><dd><code>{check.candidateSha256}</code></dd><dt>Current review binding</dt><dd><code>{check.bindingSha256}</code></dd><dt>Private package</dt><dd><code>{check.packageId ?? "Not a skill package"}</code></dd>
      <dt>Operator recipe</dt><dd><code>{check.recipeId ?? "Not an executable recipe"}</code></dd></dl>
    <button type="button" disabled={disabled} onClick={refresh}>Refresh this review</button>
    {check.reasonCodes.length > 0 && <ul className={styles.warning}>{check.reasonCodes.map(reason => <li key={reason}>{readable(reason)}</li>)}</ul>}
    {check.status === "awaiting_measurement" && <div className={styles.notice}><h3>Host measurement required</h3><p>The configured operator must measure this exact review using the existing qualified supervisor and selected agent. Loading this page does not start a worker or execute a package.</p><pre>potbot discovery measure {check.id}</pre><p className={styles.help}>The operator supplies its normal workspace, manifest and native credentials. Keep the exact review identifier when requesting that action.</p></div>}
    {check.status === "measuring" && <p className={styles.notice}>A host measurement is in progress. The fixed probe checks prerequisite presence; it does not run the package or the declared dependency programs.</p>}
    <Requirements items={check.requirements} title="Requirements retained for this measurement" />
    {report ? <section className={styles.section}><h3>Measured runtime prerequisites</h3><div className={styles.badges}><span className={styles.badge}>{report.eligible ? "Declared prerequisites passed" : "Missing or unsupported prerequisites"}</span><span className={styles.badge}>{current ? "Current measurement" : "Measurement expired or invalidated"}</span></div>
      <dl className={styles.facts}><dt>Checked</dt><dd><time dateTime={report.checkedAt}>{new Date(report.checkedAt).toLocaleString()}</time></dd><dt>Expires</dt><dd><time dateTime={report.expiresAt}>{new Date(report.expiresAt).toLocaleString()}</time></dd><dt>Requirement SHA-256</dt><dd><code>{report.requirementsSha256}</code></dd></dl>
      <div className={styles.tableScroll}><table className={styles.table}><thead><tr><th scope="col">Clause</th><th scope="col">Observation</th><th scope="col">Result</th></tr></thead><tbody>{report.checks.map((item, index) => <tr key={`${item.kind}:${item.name}:${index}`}><td>{item.name}<small> · {readable(item.kind)}</small></td><td>{readable(item.reasonCode)}</td><td className={item.status === "passed" ? styles.positive : styles.warning}>{item.status}</td></tr>)}</tbody></table></div>
      <p className={styles.help}>Alternative binary and operating-system declarations are measured as one OR clause. This report does not authorize a task, invoke a tool, activate a skill or establish installed release readiness.</p>
      <details className={styles.details}><summary>Exact runtime and source bindings</summary><dl className={styles.facts}><dt>Source binding</dt><dd><code>{report.sourceBindingSha256}</code></dd>{Object.entries(report.bindings).map(([key, value]) => <Fragment key={key}><dt>{key}</dt><dd><code>{value}</code></dd></Fragment>)}</dl></details>
      {!current && check.status === "measured" && <p className={styles.warning}>A fresh exact review and host measurement are required before opening acquisition review. Source changes and the 120-second freshness limit can invalidate this evidence.</p>}
    </section> : <p className={styles.muted}>No completed prerequisite measurement is recorded.</p>}
    {canHandoff && <section className={styles.section}><h3>Open the existing acquisition review</h3><p>{check.packageId ? "Continue with this exact private skill package in the skill workshop." : "Create one source request for the existing approved executable recipe."} Installation and activation remain explicit decisions in that workflow.</p>
      {report && !report.eligible && <p className={styles.warning}>Missing or unsupported prerequisites will remain part of the review. Continuing does not make this package executable or install its dependencies.</p>}
      <label className={styles.check}><input type="checkbox" checked={acknowledged === identity} disabled={disabled} onChange={event => setAcknowledged(event.target.checked ? identity : "")} /><span>I reviewed the measured prerequisites and remaining limitations. Continue this exact version into its existing acquisition owner.</span></label>
      <button type="button" className={styles.primary} disabled={disabled || acknowledged !== identity} onClick={() => act("handoff")}>Open acquisition review</button>
    </section>}
    {target && <section className={styles.notice}><h3>Existing acquisition record retained</h3><p>The original handoff points to this exact owned record. Its installation, build, activation and rollback controls stay with the established owner.</p><a className={styles.link} href={target}>{check.target!.kind === "skill_package" ? "Review the private skill package" : "Review the executable acquisition"} ↗</a><p><code>{check.target!.id}</code></p></section>}
    {check.actions.includes("cancel") && <div className={styles.actions}><button type="button" disabled={disabled} onClick={() => act("cancel")}>Cancel this discovery review</button></div>}
    <details className={styles.details}><summary>Resolved candidate and provenance</summary><Observation value={check.candidate.observation} /></details>
  </article>;
}

function AdmissionActions({ kind, record, disabled, mutate }: { kind: "sources" | "publishers"; record: DiscoverySource | DiscoveryPublisher; disabled: boolean; mutate: DiscoveryMutate }) {
  const [acknowledged, setAcknowledged] = useState(""); const identity = `${kind}:${record.id}:${record.revision}:${record.bindingSha256}`;
  if (!record.actions.length) return null;
  return <><label className={styles.check}><input type="checkbox" disabled={disabled} checked={acknowledged === identity} onChange={event => setAcknowledged(event.target.checked ? identity : "")} /><span>I reviewed this exact {kind === "sources" ? "source configuration" : "publisher namespace claim"} and its current revision. Apply the selected operator decision to future discovery reviews and handoffs.</span></label>
    <div className={styles.actions}>{record.actions.map(action => <button type="button" key={action} className={action === "revoke" ? styles.danger : undefined} disabled={disabled || acknowledged !== identity} onClick={() => {
      const body = discoveryControlBody(record, action); if (!disabled && body && acknowledged === identity) mutate(`${DISCOVERY_ENDPOINT}/${kind}/${record.id}/${action}`, body);
    }}>{readable(action)} {kind === "sources" ? "source" : "publisher"}</button>)}</div></>;
}
export function SourceCard({ source, disabled, mutate, publishers }: { source: DiscoverySource; disabled: boolean; mutate: DiscoveryMutate; publishers: (id: string) => void }) {
  return <article className={styles.panel}><div className={styles.body}><div className={styles.row}><h3>{source.name}</h3><span className={styles.badge}>{source.state}</span></div><p>{readable(source.adapter)} · revision {source.revision} · {source.approvalValid ? "Valid operator decision" : "Approval unavailable"}</p>
    <dl className={styles.facts}><dt>Source</dt><dd><code>{source.id}</code></dd><dt>Configuration SHA-256</dt><dd><code>{source.configSha256}</code></dd><dt>Decision binding</dt><dd><code>{source.bindingSha256}</code></dd>
      <dt>Retained snapshot</dt><dd><code>{source.snapshotId ?? "No successful refresh retained"}</code></dd><dt>Snapshot SHA-256</dt><dd><code>{source.snapshotSha256 ?? "Not recorded"}</code></dd><dt>Refresh query</dt><dd>{source.query ?? "Not recorded"}</dd>
      <dt>Fetched / expires</dt><dd>{source.fetchedAt ? new Date(source.fetchedAt).toLocaleString() : "Not recorded"} / {source.expiresAt ? new Date(source.expiresAt).toLocaleString() : "Not recorded"}</dd></dl>
    {source.lastErrorCode && <p className={styles.warning}>{readable(source.lastErrorCode)}. The host operator owns source access and bounded refresh.</p>}
    <button type="button" onClick={() => publishers(source.id)}>Inspect this source&apos;s publishers</button><AdmissionActions kind="sources" record={source} disabled={disabled} mutate={mutate} />
  </div></article>;
}
export function PublisherCard({ publisher, sourceName, disabled, mutate }: { publisher: DiscoveryPublisher; sourceName: string; disabled: boolean; mutate: DiscoveryMutate }) {
  return <article className={styles.panel}><div className={styles.body}><div className={styles.row}><h3>{publisher.name}</h3><span className={styles.badge}>{publisher.state}</span></div><p>{sourceName} · revision {publisher.revision}</p>
    <dl className={styles.facts}><dt>Claim evidence</dt><dd>{readable(publisher.evidence)}</dd><dt>Operator approval</dt><dd>{publisher.approvalValid ? "Valid decision" : "Unavailable"}</dd><dt>Decision binding</dt><dd><code>{publisher.bindingSha256}</code></dd></dl>
    <p className={styles.help}>Admission applies to this namespace in this source. A registry claim is not an independent publisher signature or package-safety finding.</p>
    <AdmissionActions kind="publishers" record={publisher} disabled={disabled} mutate={mutate} /></div></article>;
}
