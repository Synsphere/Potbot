"use client";

import { useEffect, useRef, useState } from "react";
import {
  projectHistory, projectPlan, skillInstallBody, skillControls, type AcquiredSkill, type HistoryEnvelope, type PackageEnvelope,
  type SkillAction, type SkillFile, type SkillManifest, type SkillPackage, type SkillPlan,
} from "../../lib/skill-acquisition.ts";
import { WorkshopRequests, readSkillResource } from "../../lib/skill-workshop-client.ts";
import styles from "./skills.module.css";

export type WorkshopScope = { workspaceId: string; principalId: string };
export type Mutate = (path: string, body: Record<string, unknown>) => void;
export const headers = (workspaceId: string) => ({ "X-Potbot-Workspace": workspaceId });
export const short = (value: string) => value.slice(0, 12);
export const size = (bytes: number) => bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
export const availability = (skill: AcquiredSkill) => skill.state === "revoked" ? "Revoked" : !skill.approvalValid ? "Approval invalid" : skill.enabled ? "Available to load" : "Disabled";

export function ManifestReview({ manifest, source }: { manifest: SkillManifest; source: unknown }) {
  return <>
    <div className={styles.badges}><span className={styles.badge}>Data package</span><span className={styles.badge}>{manifest.compatibility.status === "portable" ? "Portable format" : "Compatibility review required"}</span></div>
    {manifest.findings.length > 0 && <section aria-label="Package findings"><h3>Review findings</h3><ul className={styles.requirements}>{manifest.findings.map((finding, index) =>
      <li key={`${finding.code}:${index}`}><span className={finding.severity === "warning" ? styles.warning : styles.muted}>{finding.severity}</span><span>{finding.message}</span></li>)}</ul></section>}
    <section aria-label="Declared requirements"><h3>Declared requirements</h3>
      {manifest.requirements.length ? <ul className={styles.requirements}>{manifest.requirements.map((requirement, index) => <li key={index}>
        <span className={styles.badge}>Unverified</span><span><code>{requirement.kind}</code> · {requirement.name}</span>
      </li>)}</ul> : <p className={styles.muted}>No requirements were declared in the package. This does not establish runtime readiness.</p>}
      <p className={styles.help}>Dependencies are review information. Importing or activating a skill does not install tools, grant permissions, or execute scripts.</p>
    </section>
    {manifest.compatibility.unsupported.length > 0 && <section><h3>Unsupported behavior</h3><ul>{manifest.compatibility.unsupported.map((item, index) => <li key={index}><code>{item}</code></li>)}</ul>
      <p className={styles.help}>These declarations remain preserved as source data; Potbot does not activate them.</p></section>}
    <details className={styles.details}><summary>Source provenance</summary><pre>{JSON.stringify(source, null, 2)}</pre>
      <p className={styles.help}>Digests establish the exact imported bytes. They do not authenticate the publisher or establish trust in the instructions.</p></details>
    <details className={styles.details}><summary>Preserved source metadata</summary><pre>{JSON.stringify(manifest.metadata, null, 2)}</pre></details>
  </>;
}

export function ResourceBrowser({ scope, base, manifest, releaseSha256 }: { scope: WorkshopScope; base: string; manifest: SkillManifest; releaseSha256?: string }) {
  const requests = useRef(new WorkshopRequests());
  const [file, setFile] = useState<SkillFile | null>(null), [bytes, setBytes] = useState<Uint8Array<ArrayBuffer> | null>(null);
  const [text, setText] = useState<string | null>(null), [notice, setNotice] = useState(""), [busy, setBusy] = useState(false);
  useEffect(() => () => requests.current.reset(), []);
  function inspect(selected: SkillFile) {
    requests.current.reset(); const ticket = requests.current.open();
    setFile(selected); setBytes(null); setText(null); setBusy(true); setNotice("Reading the exact resource and verifying its SHA-256…");
    void (async () => {
      try {
        const result = await readSkillResource({ ...scope, base, file: selected, releaseSha256, ticket });
        if (!ticket.current()) return;
        let preview: string | null = null;
        try { const decoded = new TextDecoder("utf-8", { fatal: true }).decode(result); if (!decoded.includes("\u0000")) preview = decoded; } catch { /* Binary downloads stay binary. */ }
        setBytes(result); setText(preview); setNotice(`Verified ${size(result.length)} against the complete resource digest.`);
      } catch (error) { if (ticket.current()) setNotice(error instanceof Error ? error.message : "The resource could not be verified."); }
      finally { if (ticket.current()) setBusy(false); ticket.finish(); }
    })();
  }
  function download() {
    if (!bytes || !file) return;
    const url = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
    const link = document.createElement("a"); link.href = url; link.download = file.path.split("/").at(-1) ?? "skill-resource";
    document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <section className={styles.section} aria-label="Exact package resources"><h3>Package resources <span className={styles.muted}>({manifest.files.length})</span></h3>
    <p className={styles.help}>Inspect any file as data. Full files are read in bounded ranges and verified before preview or download.</p>
    <div className={styles.tableScroll}><table className={styles.table}><thead><tr><th scope="col">Path</th><th scope="col">Size</th><th scope="col">Content</th><th scope="col">Inspect</th></tr></thead>
      <tbody>{manifest.files.map(item => <tr key={item.path}><td><code>{item.path}</code>{item.executable_hint && <div className={styles.warning}>Script · data only</div>}</td><td>{size(item.size)}</td><td><code>{item.media_type}</code></td>
        <td><button type="button" onClick={() => inspect(item)} aria-pressed={file?.path === item.path}>Read</button></td></tr>)}</tbody></table></div>
    {file && <div aria-busy={busy}><h4>{file.path}</h4><code>{file.sha256}</code><p className={styles.status} role="status">{notice}</p>
      {bytes && <><button type="button" onClick={download}>Download verified file</button>{text !== null ? <pre>{text}</pre> : <p className={styles.muted}>Binary resource. Download it to inspect its original bytes.</p>}</>}
    </div>}
  </section>;
}

export function PlanReview({ scope, item, installed, disabled, mutate, revision }: { scope: WorkshopScope; item: SkillPackage; installed: AcquiredSkill[]; disabled: boolean; mutate: Mutate; revision: number }) {
  const [target, setTarget] = useState(""), [plan, setPlan] = useState<SkillPlan | null>(null), [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false), [notice, setNotice] = useState(""); const requests = useRef(new WorkshopRequests());
  const candidates = installed.filter(skill => skill.owned && skill.state === "active" && skill.name === item.name);
  useEffect(() => { requests.current.reset(); setPlan(null); setAcknowledged(false); setNotice(""); setBusy(false); }, [target, item.id, revision]);
  useEffect(() => () => requests.current.reset(), []);
  function review() {
    requests.current.reset(); const ticket = requests.current.open(); setBusy(true); setPlan(null); setAcknowledged(false); setNotice("Computing the exact package and revision plan…");
    void (async () => {
      try {
        const query = target ? `?skillId=${encodeURIComponent(target)}` : "";
        const response = await fetch(`/api/ui/skills/packages/${item.id}/plan${query}`, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: headers(scope.workspaceId) });
        if (!ticket.current()) return;
        if (!response.ok) throw new Error("The plan is unavailable. Refresh the skill list and review the current revision again.");
        const result = projectPlan(await response.json(), scope.workspaceId, item.id, target || undefined, scope.principalId);
        if (!ticket.current()) return;
        if (!result) throw new Error("The plan did not match this package and workspace.");
        setPlan(result.plan); setNotice("");
      } catch (error) { if (ticket.current()) setNotice(error instanceof Error ? error.message : "The plan could not be read."); }
      finally { if (ticket.current()) setBusy(false); ticket.finish(); }
    })();
  }
  return <section className={styles.section} aria-label="Review and approve package"><h3>Review installation</h3>
    <label className={styles.field}>Install or update<select value={target} disabled={disabled || busy} onChange={event => { requests.current.reset(); setPlan(null); setAcknowledged(false); setTarget(event.target.value); }}>
      <option value="">Install in my namespace</option>{candidates.map(skill => <option key={skill.id} value={skill.id}>Update {skill.name} · revision {skill.revision}</option>)}</select></label>
    <p className={styles.help}>An existing skill with this name will be planned as an update. Load more installed skills to choose an explicit update target.</p>
    <div className={styles.actions}><button type="button" onClick={review} disabled={disabled || busy}>Review exact plan</button></div>
    <p className={styles.status} role="status">{notice}</p>
    {plan && <><dl className={styles.facts}><dt>Action</dt><dd>{plan.action === "install" ? "Install disabled" : "Update and disable"}</dd><dt>Expected revision</dt><dd>{plan.expectedRevision}</dd>
      <dt>Source changed</dt><dd>{plan.sourceChanged ? "Yes — provenance is included in this approval" : "No"}</dd><dt>Release</dt><dd><code>{plan.releaseSha256}</code></dd>
      {plan.previousReleaseSha256 && <><dt>Previous release</dt><dd><code>{plan.previousReleaseSha256}</code></dd></>}
      <dt>Plan SHA-256</dt><dd><code>{plan.sha256}</code></dd></dl>
      <div className={styles.pair}>{(["added", "changed", "removed"] as const).map(kind => <div key={kind}><h4>{kind[0].toUpperCase() + kind.slice(1)} · {plan[kind].length}</h4>
        {plan[kind].length ? <ul>{plan[kind].map(path => <li key={path}><code>{path}</code></li>)}</ul> : <p className={styles.muted}>None</p>}</div>)}</div>
      <label className={styles.check}><input type="checkbox" checked={acknowledged} disabled={disabled} onChange={event => setAcknowledged(event.target.checked)} />
        <span>I reviewed the instructions, resources, provenance and declared requirements. Apply this exact plan and leave the skill disabled.</span></label>
      <div className={styles.actions}><button type="button" className={styles.primary} disabled={disabled || !acknowledged} onClick={() => mutate(`/api/ui/skills/packages/${item.id}/install`,
        skillInstallBody(plan))}>{plan.action === "install" ? "Approve and install disabled" : "Approve update and disable"}</button></div>
    </>}
  </section>;
}

export function PackageView({ scope, value, installed, disabled, mutate, revision }: { scope: WorkshopScope; value: PackageEnvelope; installed: AcquiredSkill[]; disabled: boolean; mutate: Mutate; revision: number }) {
  const item = value.package;
  return <article className={styles.body}><span className={styles.eyebrow}>PRIVATE IMPORT</span><h2>{item.name}</h2><p>{item.description}</p>
    <dl className={styles.facts}><dt>Inventory</dt><dd>{item.files} files · {size(item.bytes)}</dd><dt>Release SHA-256</dt><dd><code>{item.releaseSha256}</code></dd><dt>Source SHA-256</dt><dd><code>{item.sourceSha256}</code></dd></dl>
    <ManifestReview manifest={item.manifest} source={item.source} />
    <details className={styles.details} open><summary>SKILL.md preview</summary><pre>{value.content ?? "Read SKILL.md from the resources below."}</pre>
      {value.contentTruncated && <p className={styles.warning}>Preview limited to 64 KiB. Read the complete SKILL.md resource below before approving.</p>}</details>
    <ResourceBrowser key={item.id} scope={scope} base={`/api/ui/skills/packages/${item.id}`} manifest={item.manifest} />
    <PlanReview scope={scope} item={item} installed={installed} disabled={disabled} mutate={mutate} revision={revision} />
  </article>;
}

export function InstalledView({ scope, skill, disabled, mutate, onRefresh, onReviewPackage }: { scope: WorkshopScope; skill: AcquiredSkill; disabled: boolean; mutate: Mutate; onRefresh: () => void; onReviewPackage: () => void }) {
  const [history, setHistory] = useState<HistoryEnvelope | null>(null), [notice, setNotice] = useState(""), [busy, setBusy] = useState(false);
  const [release, setRelease] = useState(""), [activateAcknowledged, setActivateAcknowledged] = useState(false), [revokeAcknowledged, setRevokeAcknowledged] = useState(false);
  const requests = useRef(new WorkshopRequests());
  const controls = skillControls(skill);
  useEffect(() => () => requests.current.reset(), []);
  function loadHistory(after?: string) {
    requests.current.reset(); const ticket = requests.current.open(); setBusy(true); setNotice("Reading retained releases and control history…");
    void (async () => {
      try {
        const response = await fetch(`/api/ui/skills/${skill.id}/history?limit=25${after ? `&after=${after}` : ""}`, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: headers(scope.workspaceId) });
        if (!ticket.current()) return;
        if (!response.ok) throw new Error("The history is unavailable for this owner and workspace.");
        const result = projectHistory(await response.json(), scope.workspaceId, skill.id, scope.principalId, Number(after ?? 0));
        if (!ticket.current()) return;
        if (!result || result.skill.revision !== skill.revision) throw new Error("The skill changed while history was loading. Refresh its current revision.");
        setHistory(prior => after && prior ? { ...result, releases: [...new Map([...prior.releases, ...result.releases].map(item => [item.releaseSha256, item])).values()], events: [...prior.events, ...result.events] } : result);
        setNotice("");
      } catch (error) { if (ticket.current()) setNotice(error instanceof Error ? error.message : "The history could not be read."); }
      finally { if (ticket.current()) setBusy(false); ticket.finish(); }
    })();
  }
  const action = (name: SkillAction) => mutate(`/api/ui/skills/${skill.id}/${name}`, { expectedRevision: skill.revision, releaseSha256: name === "rollback" ? release : null, confirm: true });
  return <article className={styles.body}><span className={styles.eyebrow}>{skill.owned ? "MY INSTALLED SKILL" : "SHARED WORKSPACE SKILL"}</span><h2>{skill.name}</h2><p>{skill.manifest.description}</p>
    <div className={styles.badges}><span className={styles.badge}>{availability(skill)}</span><span className={styles.badge}>Revision {skill.revision}</span></div>
    <p className={styles.notice}>{skill.state === "revoked" ? "This skill has been revoked. Its owner can inspect the retained evidence, but it cannot be activated, updated or loaded again." : skill.enabled && skill.approvalValid ?
      "The approved package can be loaded as Context data by authorized agents in this workspace. Scripts and declared hooks remain inert." : "This package is retained for review. It is unavailable for agent loading until its owner explicitly activates a valid approved revision."}</p>
    <dl className={styles.facts}><dt>Context name</dt><dd><code>{skill.contextName}</code></dd><dt>Context object</dt><dd><code>{skill.contextObjectId}</code></dd><dt>Owner</dt><dd><code>{skill.ownerPrincipalId}</code></dd>
      <dt>Release SHA-256</dt><dd><code>{skill.releaseSha256}</code></dd><dt>Source SHA-256</dt><dd><code>{skill.sourceSha256}</code></dd><dt>Approval</dt><dd>{skill.approvalValid ? "Valid for this recorded control revision" : "Invalid — activation or update requires a new operator review"}</dd></dl>
    {skill.owned && skill.state === "active" && <section className={styles.section}><h3>Control data availability</h3>
      {!skill.approvalValid && <><p className={styles.warning}>This approval is invalid. Activation, disable and rollback require a valid preceding approval. Review the current package as a fresh update, install it disabled, then activate the new approved revision.</p><button type="button" disabled={disabled} onClick={onReviewPackage}>Review current package for a fresh approval</button></>}
      {controls.activate && <label className={styles.check}><input type="checkbox" checked={activateAcknowledged} disabled={disabled} onChange={event => setActivateAcknowledged(event.target.checked)} />
        <span>Allow authorized agents to load this exact approved package as Context data.</span></label>}
      <div className={styles.actions}>{!skill.enabled ? <button type="button" className={styles.primary} disabled={disabled || !controls.activate || !activateAcknowledged} onClick={() => action("activate")}>Activate for data loading</button> :
        <button type="button" disabled={disabled || !controls.disable} onClick={() => action("disable")}>Disable data loading</button>}<button type="button" onClick={onRefresh} disabled={disabled}>Refresh current revision</button></div>
      <p className={styles.help}>Every action checks revision {skill.revision}. Updates and rollbacks leave the skill disabled until you activate it again.</p>
    </section>}
    <ManifestReview manifest={skill.manifest} source={skill.source} />
    <ResourceBrowser key={`${skill.id}:${skill.revision}:${skill.releaseSha256}`} scope={scope} base={`/api/ui/skills/${skill.id}`} releaseSha256={skill.releaseSha256} manifest={skill.manifest} />
    {skill.owned && <section className={styles.section}><h3>Retained releases and history</h3><p className={styles.help}>Each approval and availability change is retained with its exact source identity. Load the complete history to see every prior release.</p>
      {!history && <button type="button" disabled={busy} onClick={() => loadHistory()}>Read release history</button>}<p className={styles.status} role="status">{notice}</p>
      {history && <><ol className={styles.history}>{history.events.map(event => <li key={event.revision}><div className={styles.row}><strong>Revision {event.revision} · {event.action}</strong><time dateTime={event.createdAt}>{new Date(event.createdAt).toLocaleString()}</time></div>
        <p>{event.state === "revoked" ? "Revoked" : event.enabled ? "Data loading enabled" : "Data loading disabled"} · release <code>{short(event.releaseSha256)}</code></p><code>Source {event.sourceSha256}</code>
        <details className={styles.details}><summary>Recorded source</summary><pre>{JSON.stringify(event.source, null, 2)}</pre></details></li>)}</ol>
        {history.nextCursor && <button type="button" disabled={busy} onClick={() => loadHistory(history.nextCursor!)}>Load more history</button>}
        {skill.state === "active" && history.releases.some(item => !item.current) && <div className={styles.section}><h4>Rollback to a retained release</h4>
          <label className={styles.field}>Approved previous release<select value={release} disabled={disabled || !controls.rollback} onChange={event => setRelease(event.target.value)}><option value="">Choose an exact release</option>
            {history.releases.filter(item => !item.current).map(item => <option key={item.releaseSha256} value={item.releaseSha256}>{short(item.releaseSha256)} · {new Date(item.createdAt).toLocaleString()}</option>)}</select></label>
          {release && <details className={styles.details}><summary>Review selected release manifest and provenance</summary><pre>{JSON.stringify(history.releases.find(item => item.releaseSha256 === release), null, 2)}</pre></details>}
          <div className={styles.actions}><button type="button" disabled={disabled || !controls.rollback || !release} onClick={() => action("rollback")}>Rollback and leave disabled</button></div></div>}
      </>}
    </section>}
    {skill.owned && skill.state === "active" && <section className={styles.section}><h3>Revoke this skill</h3><label className={styles.check}><input type="checkbox" checked={revokeAcknowledged} disabled={disabled} onChange={event => setRevokeAcknowledged(event.target.checked)} />
      <span>Permanently revoke this installed skill. It cannot be activated, updated or rolled back again; its review history remains retained.</span></label>
      <div className={styles.actions}><button type="button" className={styles.danger} disabled={disabled || !revokeAcknowledged} onClick={() => action("revoke")}>Revoke skill permanently</button></div></section>}
  </article>;
}
