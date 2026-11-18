"use client";

import { useState, type FormEvent } from "react";
import { COMMIT, toolActionBody, type AcquisitionEvents, type ToolAction, type ToolOperation, type ToolRecipe } from "../../../lib/tool-acquisition.ts";
import { TOOL_ENDPOINT, TOOL_PHASE_LABELS, toolAvailability } from "../../../lib/tool-acquisition-client.ts";
import styles from "../../skills/skills.module.css";

export type ToolMutate = (path: string, body: Record<string, unknown>) => void;
const short = (value: string) => value.slice(0, 12);
const readable = (value: string) => value.replaceAll("_", " ");
const bytes = (value: number) => value < 1024 ? `${value} B` : value < 1024 ** 2 ? `${(value / 1024).toFixed(1)} KiB` : value < 1024 ** 3 ? `${(value / 1024 ** 2).toFixed(1)} MiB` : `${(value / 1024 ** 3).toFixed(1)} GiB`;

export function RecipeSummary({ recipe }: { recipe: ToolRecipe }) {
  return <><p>{recipe.description}</p><dl className={styles.facts}>
    <dt>Repository</dt><dd><code>{recipe.repository}</code></dd><dt>Recipe SHA-256</dt><dd><code>{recipe.recipeSha256}</code></dd>
    <dt>Builder</dt><dd>Verified rootfs executable · network denied · deadline {recipe.build.timeoutMs / 1000}s</dd>
    <dt>Command template</dt><dd><code>{recipe.build.commandSha256}</code></dd><dt>Enrolled agent</dt><dd><code>{recipe.agentId}</code></dd>
    <dt>Recipe approval</dt><dd>{recipe.approvalValid ? "Valid operator publication" : "Invalid — the operator must publish a current approved recipe"}</dd>
  </dl><details className={styles.details}><summary>Declared tests and outputs</summary><div className={styles.tableScroll}><table className={styles.table}>
    <thead><tr><th scope="col">Test</th><th scope="col">Kind</th><th scope="col">Deadline</th></tr></thead><tbody>{recipe.tests.map(test => <tr key={test.name}><td>{test.name}</td><td>{test.kind}</td><td>{test.timeoutMs / 1000}s</td></tr>)}</tbody></table></div>
    <h4>Build artifacts</h4><ul>{recipe.artifactPaths.map(path => <li key={path}><code>{path}</code></li>)}</ul>
    <h4>Dependency lockfiles</h4>{recipe.lockfilePaths.length ? <ul>{recipe.lockfilePaths.map(path => <li key={path}><code>{path}</code></li>)}</ul> : <p className={styles.help}>None declared by this recipe.</p>}
    <h4>Declared cleanup</h4><ul>{recipe.cleanupPaths.map(path => <li key={path}><code>{path}</code></li>)}</ul>
  </details></>;
}

export function SourceRequest({ recipes, disabled, mutate, nextCursor, loadMore, loading }: { recipes: ToolRecipe[]; disabled: boolean; mutate: ToolMutate; nextCursor: string | null; loadMore: () => void; loading: boolean }) {
  const [recipeId, setRecipeId] = useState(""), [commit, setCommit] = useState(""), [acknowledged, setAcknowledged] = useState(false), [notice, setNotice] = useState("");
  const recipe = recipes.find(item => item.id === recipeId);
  function submit(event: FormEvent) {
    event.preventDefault(); if (disabled || !recipe?.approvalValid || !acknowledged) return;
    if (!COMMIT.test(commit)) { setNotice("Enter the exact 40-character lowercase Git commit."); return; }
    setNotice(""); mutate(TOOL_ENDPOINT, { recipeId: recipe.id, commit, confirm: true });
  }
  return <section className={styles.body} aria-label="Request an exact tool source"><h3>Acquire a source</h3><p className={styles.help}>Choose a published operator recipe and an exact commit. The host inspects source bytes before requesting build approval.</p>
    <form className={styles.form} onSubmit={submit}><fieldset disabled={disabled} className={styles.form}>
      <label className={styles.field}>Operator recipe<select value={recipeId} onChange={event => { setRecipeId(event.target.value); setAcknowledged(false); setNotice(""); }}><option value="">Select a published recipe</option>
        {recipes.map(item => <option key={item.id} value={item.id} disabled={!item.approvalValid}>{item.name} · {item.slug}{!item.approvalValid ? " · approval invalid" : ""}</option>)}</select></label>
      {recipe && <><p className={styles.help}>{recipe.description}</p><code>{recipe.repository}</code><span className={styles.help}>Immutable recipe <code>{short(recipe.recipeSha256)}</code></span></>}
      <label className={styles.field}>Exact source commit<input value={commit} required minLength={40} maxLength={40} pattern="[a-f0-9]{40}" autoComplete="off" spellCheck={false} placeholder="40 lowercase hexadecimal characters" onChange={event => { setCommit(event.target.value); setAcknowledged(false); setNotice(""); }} /></label>
      <label className={styles.check}><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} /><span>Request inspection of this exact source. Building and activation each require a later approval.</span></label>
      <button type="submit" className={styles.primary} disabled={!recipe?.approvalValid || !COMMIT.test(commit) || !acknowledged}>Request source inspection</button>
    </fieldset></form><p className={styles.status} role="status">{notice}</p>
    {!recipes.length && <p className={styles.muted}>No operator recipe is published for this principal. Publish an immutable recipe from the enrolled host before requesting a source.</p>}
    {nextCursor && <button type="button" disabled={loading} onClick={loadMore}>Load more recipes</button>}
  </section>;
}

export function ToolDetail({ operation, disabled, mutate, history, historyBusy, historyNotice, loadEvents, refresh, workspaceId }: {
  operation: ToolOperation; disabled: boolean; mutate: ToolMutate; history: AcquisitionEvents | null; historyBusy: boolean; historyNotice: string;
  loadEvents: (reset?: boolean) => void; refresh: () => void; workspaceId: string;
}) {
  const [acknowledgements, setAcknowledgements] = useState<Record<string, string>>({});
  const identity = `${operation.id}:${operation.revision}:${operation.bindingSha256}`;
  const reviewed = acknowledgements.review === identity, activation = acknowledgements.activation === identity, rollback = acknowledgements.rollback === identity,
    repin = acknowledgements.repin === identity, reconcile = acknowledgements.reconcile === identity;
  const setCheck = (name: string, checked: boolean) => setAcknowledgements(prior => ({ ...prior, [name]: checked ? identity : "" }));
  const permitted = (action: ToolAction) => operation.allowedActions.includes(action) && toolActionBody(operation, action) !== null;
  const act = (action: ToolAction) => { const body = toolActionBody(operation, action); if (!disabled && body) mutate(`${TOOL_ENDPOINT}/${operation.id}/${action}`, body); };
  const review = operation.review;
  return <article className={styles.body}><span className={styles.eyebrow}>EXECUTABLE TOOL ACQUISITION</span><h2>{operation.recipe.name}</h2>
    <div className={styles.badges}><span className={styles.badge}>{TOOL_PHASE_LABELS[operation.phase]}</span><span className={styles.badge}>{toolAvailability(operation)}</span><span className={styles.badge}>Revision {operation.revision}</span></div>
    <p className={styles.muted}>{operation.kind === "install" ? "First installation" : operation.kind === "upgrade" ? "Upgrade" : "Source inspection determines whether an installation already exists"} · {operation.slug}</p>
    <dl className={styles.facts}><dt>Source</dt><dd><code>{operation.repository}</code></dd><dt>Requested commit</dt><dd><code>{operation.newCommit}</code></dd>
      <dt>Predecessor</dt><dd>{operation.kind === null ? "Not yet verified" : operation.oldCommit ? <code>{operation.oldCommit}</code> : "Verified absence — no prior installation"}</dd>
      <dt>Operation</dt><dd><code>{operation.id}</code></dd><dt>Current binding</dt><dd><code>{operation.bindingSha256}</code></dd></dl>
    {operation.errorCode && <p className={styles.warning} role="alert">{readable(operation.errorCode)}. The recorded outcome remains {readable(operation.phase)}; refresh or use an authorized recovery action below.</p>}
    {operation.hostAction && <p className={styles.notice}>Host action: <strong>{readable(operation.hostAction.action)}</strong> · {readable(operation.hostAction.status)}. The independent operator worker owns this action.</p>}
    {operation.phase === "indeterminate" && <p className={styles.uncertain}>An effect may have occurred without a confirmed receipt. The host must reconcile the existing operation; the workflow does not automatically repeat it.</p>}
    <div className={styles.actions}><button type="button" onClick={refresh} disabled={disabled}>Refresh current state</button><a className={styles.link} href={`/tools?workspace=${encodeURIComponent(workspaceId)}`}>Operations and controlled checks ↗</a></div>

    <details className={styles.details}><summary>Published operator recipe</summary><RecipeSummary recipe={operation.recipe} /></details>
    <section className={styles.section}><h3>Source inspection</h3>{operation.source ? <><dl className={styles.facts}>
      <dt>Source inventory</dt><dd>{operation.source.files} files · {bytes(operation.source.bytes)}</dd><dt>Git tree</dt><dd><code>{operation.source.git_tree}</code></dd>
      <dt>Tree SHA-256</dt><dd><code>{operation.source.tree_sha256}</code></dd><dt>Source receipt</dt><dd><code>{operation.source.source_sha256}</code></dd>
      <dt>Retained storage</dt><dd><code>{operation.source.storage_sha256}</code></dd></dl>
      <p><a className={styles.link} href={`${operation.repository.slice(0, -4)}/tree/${operation.newCommit}`} target="_blank" rel="noopener noreferrer">Review this exact GitHub commit ↗</a></p>
      <p className={styles.help}>These identities pin the fetched bytes. Source inspection does not execute the repository or establish that a build will pass.</p>
    </> : <p className={styles.muted}>No verified source receipt is recorded yet.</p>}</section>

    <section className={styles.section}><h3>Exact build review</h3>{review ? <>
      <dl className={styles.facts}><dt>Review SHA-256</dt><dd><code>{review.sha256}</code></dd><dt>Command SHA-256</dt><dd><code>{review.commandSha256}</code></dd>
        <dt>Sandbox image</dt><dd><code>{review.imageDigest}</code></dd><dt>Qualification</dt><dd><code>{review.qualificationSha256}</code></dd><dt>Guest build directory</dt><dd><code>{review.buildRoot}</code></dd></dl>
      <h4>Exact operator command arguments</h4><pre>{JSON.stringify(review.buildArgv, null, 2)}</pre>
      <p className={styles.help}>Arguments are shown as a JSON array for review. The verified sandbox builder consumes this exact command with network access denied.</p>
      <h4>Source dependency locks</h4>{review.lockfiles.length ? <div className={styles.tableScroll}><table className={styles.table}><thead><tr><th scope="col">Path</th><th scope="col">SHA-256</th></tr></thead>
        <tbody>{review.lockfiles.map(([path, hash]) => <tr key={path}><td><code>{path}</code></td><td><code>{hash}</code></td></tr>)}</tbody></table></div> : <p className={styles.help}>The recipe declares no dependency lockfiles. Review its build requirements before approval.</p>}
      {permitted("approve-build") && <><label className={styles.check}><input type="checkbox" checked={reviewed} disabled={disabled} onChange={event => setCheck("review", event.target.checked)} />
        <span>I reviewed the exact source, predecessor, operator command, lockfiles and runtime identity. Authorize this sandbox build and its declared tests; activation requires another decision.</span></label>
        <div className={styles.actions}><button type="button" className={styles.primary} disabled={disabled || !reviewed} onClick={() => act("approve-build")}>Approve exact sandbox build</button></div></>}
    </> : <p className={styles.muted}>The host has not published a complete source and execution review.</p>}</section>

    <section className={styles.section}><h3>Build and qualification evidence</h3>{operation.build ? <dl className={styles.facts}>
      <dt>Built inventory</dt><dd>{operation.build.files} entries · {bytes(operation.build.bytes)}</dd><dt>Build tree</dt><dd><code>{operation.build.treeSha256}</code></dd>
      <dt>Isolation identity</dt><dd><code>{operation.build.isolationIdentity}</code></dd><dt>Dispatched command</dt><dd><code>{operation.build.commandSha256}</code></dd>
    </dl> : <p className={styles.muted}>No completed sandbox build receipt is recorded.</p>}
      {operation.tests.length > 0 && <div className={styles.tableScroll}><table className={styles.table}><thead><tr><th scope="col">Test</th><th scope="col">Kind</th><th scope="col">Measured time</th><th scope="col">Result</th></tr></thead>
        <tbody>{operation.tests.map(test => <tr key={test.name}><td>{test.name}</td><td>{test.kind}</td><td>{test.elapsedMs} ms</td><td className={test.passed ? styles.positive : styles.warning}>{test.passed ? "Passed" : "Failed"}</td></tr>)}</tbody></table></div>}
      <dl className={styles.facts}><dt>Approved suite</dt><dd><code>{operation.suiteSha256}</code></dd><dt>Test evidence</dt><dd><code>{operation.evidenceSha256 ?? "Not recorded"}</code></dd></dl>
      {permitted("approve") && <label className={styles.check}><input type="checkbox" checked={activation} disabled={disabled} onChange={event => setCheck("activation", event.target.checked)} />
        <span>Approve promotion of this exact tested candidate. The host will hold changed executables until runtime pin verification and installed postflight complete.</span></label>}
      <div className={styles.actions}>{permitted("approve") && <button type="button" className={styles.primary} disabled={disabled || !activation} onClick={() => act("approve")}>{operation.kind === "install" ? "Approve tested installation" : "Approve tested upgrade"}</button>}
        {permitted("reject") && <button type="button" disabled={disabled} onClick={() => act("reject")}>Reject tested candidate</button>}</div>
    </section>

    <section className={styles.section}><h3>Host activation and release</h3><dl className={styles.facts}>
      <dt>Release state</dt><dd>{operation.releaseState === "withdrawn" ? "First installation removed; absence verified" : readable(operation.releaseState)}</dd><dt>Generation</dt><dd>{operation.releaseGeneration || "Not established"}</dd>
      <dt>Release binding</dt><dd><code>{operation.releaseBindingSha256 ?? "Not recorded"}</code></dd><dt>Release evidence</dt><dd><code>{operation.releaseEvidenceSha256 ?? "Not recorded"}</code></dd></dl>
      {(operation.phase === "awaiting_repin" || permitted("repin")) && <div className={styles.notice}><strong>Host pin review required.</strong>
        <p>The host operator must independently review and apply the executable pin proposal, then restart with the approved runtime manifest. This page requests verification of that readback; it cannot edit executable pins or start a second workspace watcher.</p>
        {operation.kind === "install" && <p>If this first installation has been rolled back, the host must retire this slug&apos;s executable pins before verifying withdrawal to absence.</p>}</div>}
      {permitted("repin") && <><label className={styles.check}><input type="checkbox" checked={repin} disabled={disabled} onChange={event => setCheck("repin", event.target.checked)} /><span>The host operator reviewed and applied the pin proposal and restarted with the approved manifest. Request independent readback.</span></label>
        <button type="button" disabled={disabled || !repin} onClick={() => act("repin")}>Request host pin verification</button></>}
      {permitted("postflight") && <><p className={styles.notice}>The exact runtime pins have been read back. The installed candidate remains held until the original approved test suite passes postflight.</p>
        <button type="button" className={styles.primary} disabled={disabled} onClick={() => act("postflight")}>Request installed postflight</button></>}
      {operation.installedState === "released" && <p className={styles.notice}>A passing release decision is recorded for these exact bits and pins. Use the operations workbench for a fresh task-specific eligibility check; normal grants and execution policy still apply.</p>}
      {operation.phase === "withdrawn" && <p className={styles.notice}>The first installation was rolled back to independently verified absence. Its source, build, tests and control history remain retained.</p>}
    </section>

    {(permitted("rollback") || permitted("reconcile") || permitted("cancel")) && <section className={styles.section}><h3>Recovery and rollback</h3>
      {permitted("rollback") && <><label className={styles.check}><input type="checkbox" checked={rollback} disabled={disabled} onChange={event => setCheck("rollback", event.target.checked)} />
        <span>{operation.kind === "install" ? "Roll back this first installation to absence. The host must retire its executable pins and verify removal." : `Restore the exact retained predecessor ${short(operation.oldCommit!)}. The restored version stays held until pin verification and postflight complete.`}</span></label>
        <button type="button" className={styles.danger} disabled={disabled || !rollback} onClick={() => act("rollback")}>{operation.kind === "install" ? "Request rollback to absence" : "Request predecessor rollback"}</button></>}
      {permitted("reconcile") && <><label className={styles.check}><input type="checkbox" checked={reconcile} disabled={disabled} onChange={event => setCheck("reconcile", event.target.checked)} /><span>The host operator independently reconciled the interrupted workspace lease. Request readback of this existing operation&apos;s outcome.</span></label>
        <button type="button" disabled={disabled || !reconcile} onClick={() => act("reconcile")}>Request operator reconciliation</button></>}
      {permitted("cancel") && <div className={styles.actions}><button type="button" disabled={disabled} onClick={() => act("cancel")}>Cancel acquisition</button></div>}
    </section>}

    <section className={styles.section}><h3>Control history</h3><p className={styles.help}>Ordered, bounded control evidence for this operation. Raw process output, private host paths and worker credentials are excluded.</p>
      <p className={styles.status} role="status">{historyNotice}</p>{history && history.items.length > 0 ? <ol className={styles.history}>{history.items.map(event => <li key={event.revision}>
        <div className={styles.row}><strong>Revision {event.revision} · {readable(event.action)}</strong><time dateTime={event.recordedAt}>{new Date(event.recordedAt).toLocaleString()}</time></div>
        <p>{readable(event.priorState)} → {readable(event.nextState)}</p><code>{event.evidenceSha256}</code></li>)}</ol> : <p className={styles.muted}>No control events are loaded.</p>}
      <div className={styles.actions}><button type="button" disabled={historyBusy} onClick={() => loadEvents(false)}>{history?.hasMore ? "Load next history page" : "Read new control events"}</button>
        <button type="button" disabled={historyBusy} onClick={() => loadEvents(true)}>Read history from the beginning</button></div>
    </section>
  </article>;
}
