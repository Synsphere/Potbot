"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  OPERATION_KINDS, OPERATION_STATES, READINESS_LABELS, catalogueSearch, exactAmount,
  operationReason, orderedOperationReasons, projectOperationDetail, projectOperationListing, readableCode,
  type Operation, type OperationDetail, type OperationKind, type OperationListing, type OperationState,
} from "../../lib/operation-catalogue.ts";
import styles from "./tools.module.css";
import OperationWorkbench from "./OperationWorkbench.tsx";

type Workspace = { id: string; name: string };
const workspaceHeaders = (id: string): HeadersInit => ({ "X-Potbot-Workspace": id });
const short = (value: string) => value.slice(0, 12);
const attention = (state: OperationState) => ["policy_disabled", "degraded", "indeterminate"].includes(state);

function Readiness({ state }: { state: OperationState }) {
  return <span className={`${styles.badge} ${styles[state]}`}>{READINESS_LABELS[state]}</span>;
}

function Detail({ item, workspaceId, principalId, snapshot }: { item: Operation; workspaceId: string; principalId: string; snapshot: string }) {
  const accounting = item.accounting;
  return <article className={styles.detail} aria-label={`Details for ${item.name}`}>
    <div className={styles.detailHeading}>
      <span className={styles.eyebrow}>{item.kind}</span>
      <h2>{item.name}</h2>
      <div className={styles.badges}><Readiness state={item.state} />
        {item.updateAvailable === true && <span className={styles.update}>Update available</span>}</div>
    </div>
    <OperationWorkbench key={`${workspaceId}:${principalId}:${item.id}:${snapshot}`} workspaceId={workspaceId} principalId={principalId} operationId={item.id} />
    {item.kind === "skill" && <section className={styles.detailSection}><h3>Skill packages</h3><p className={styles.muted}>Review imported instructions and resources, exact releases, activation and rollback in the skill workshop.</p>
      <a className={styles.linkButton} href={`/skills?workspace=${encodeURIComponent(workspaceId)}`}>Open skill workshop <span aria-hidden="true">↗</span></a></section>}
    <section className={styles.detailSection} aria-labelledby="readiness-title">
      <h3 id="readiness-title">Readiness and holds</h3>
      {item.reasons.length ? <ul className={styles.reasons}>{orderedOperationReasons(item.reasons).map(reason => <li key={reason}>
        <span>{operationReason(reason)}</span><code>{reason}</code>
      </li>)}</ul> : <p className={styles.muted}>No readiness explanation was supplied. A catalogue entry does not authorize execution.</p>}
      <dl className={styles.facts}>
        <div><dt>Version</dt><dd>{item.version ?? "Not recorded"}</dd></div>
        <div><dt>Update availability</dt><dd>{item.updateAvailable === null ? "Unknown" : item.updateAvailable ? "Update recorded" : "No update recorded"}</dd></div>
      </dl>
    </section>
    {item.release && <section className={styles.detailSection} aria-labelledby="release-title">
      <h3 id="release-title">Recorded tool release</h3>
      <dl className={styles.facts}>
        <div><dt>Approval / activation</dt><dd>{readableCode(item.release.state)}</dd></div>
        <div><dt>Postflight release</dt><dd>{readableCode(item.release.releaseState)}</dd></div>
        <div><dt>Generation</dt><dd>{item.release.generation ?? "Not established"}</dd></div>
      </dl>
      <p className={styles.muted}>This is the retained release record. The host checks current pins, permissions and postflight evidence again at admission.</p>
      <a className={styles.linkButton} href={`/setup/tools?workspace=${encodeURIComponent(workspaceId)}`}>Open tool acquisition and release review <span aria-hidden="true">↗</span></a>
      <details className={styles.identity}><summary>Release evidence and identity</summary>
        <dl className={styles.facts}>
          <div><dt>Upgrade operation</dt><dd><code>{item.release.operationId}</code></dd></div>
          <div><dt>Binding SHA-256</dt><dd><code>{item.release.bindingSha256 ?? "Not recorded"}</code></dd></div>
          <div><dt>Evidence SHA-256</dt><dd><code>{item.release.evidenceSha256 ?? "Not recorded"}</code></dd></div>
        </dl>
      </details>
    </section>}
    <section className={styles.detailSection} aria-labelledby="invocations-title">
      <h3 id="invocations-title">Recent linked invocations <span>{item.recentInvocations.length} / 5</span></h3>
      {item.recentInvocations.length ? <ol className={styles.invocations}>{item.recentInvocations.map(run => <li key={`${run.source}:${run.id}`}>
        <div><strong>{readableCode(run.source)}</strong><span>{readableCode(run.state)}</span></div>
        <time dateTime={run.recordedAt}>{new Date(run.recordedAt).toLocaleString()}</time>
        <code>{run.id}</code>
        {run.reservationId && <small>Reservation <code>{run.reservationId}</code></small>}
      </li>)}</ol> : <p className={styles.muted}>No recent invocations are linked to this entry. This does not establish that the operation has never run.</p>}
    </section>
    <section className={styles.detailSection} aria-labelledby="accounting-title">
      <h3 id="accounting-title">Recent linked accounting</h3>
      <p className={styles.muted}>Existing ledger records for the linked invocations only. These are partial observations, not lifetime totals or a new charge.</p>
      <dl className={styles.accountingCounts}>
        <div><dt>Reservations</dt><dd>{accounting.reservationIds.length}</dd></div>
        <div><dt>Settled</dt><dd>{accounting.settledCount}</dd></div>
        <div><dt>Unresolved</dt><dd>{accounting.indeterminateCount}</dd></div>
      </dl>
      {accounting.totals.length ? <div className={styles.tableScroll}><table className={styles.usageTable}>
        <caption className={styles.srOnly}>Recorded settled amounts by original unit and provenance</caption>
        <thead><tr><th scope="col">Unit</th><th scope="col">Amount</th><th scope="col">Basis</th></tr></thead>
        <tbody>{accounting.totals.map(amount => <tr key={`${amount.unit}:${amount.provenance}`}>
          <td><code>{amount.unit}</code></td><td>{exactAmount(amount.value)}</td><td>{readableCode(amount.provenance)}</td>
        </tr>)}</tbody>
      </table></div> : <p className={styles.muted}>No linked settled usage is recorded. Cost and usage are unknown; missing values are not zero.</p>}
      {accounting.reservationIds.length > 0 && <details className={styles.identity}><summary>Reservation references</summary>
        <ul>{accounting.reservationIds.map(id => <li key={id}><code>{id}</code></li>)}</ul>
      </details>}
    </section>
    <section className={styles.detailSection} aria-labelledby="provenance-title">
      <h3 id="provenance-title">Provenance</h3>
      <dl className={styles.facts}>
        <div><dt>Operation reference</dt><dd><code>{item.reference}</code></dd></div>
        <div><dt>Fingerprint SHA-256</dt><dd><code>{item.fingerprintSha256 ?? "Not recorded"}</code></dd></div>
        {item.contextObjectId && <div><dt>Context object</dt><dd><code>{item.contextObjectId}</code></dd></div>}
      </dl>
      <ul className={styles.sources}>{item.sources.map((source, index) => <li key={`${source.family}:${source.recordId}:${index}`}>
        <strong>{readableCode(source.family)}</strong><code>{source.recordId}</code>
        <small>Revision {source.revision ?? "not recorded"}</small>
        {source.sha256 && <code>{source.sha256}</code>}
      </li>)}</ul>
      <details className={styles.identity}><summary>Catalogue identities</summary>
        <dl className={styles.facts}>
          <div><dt>Entry SHA-256</dt><dd><code>{item.id}</code></dd></div>
          <div><dt>Snapshot SHA-256</dt><dd><code>{snapshot}</code></dd></div>
        </dl>
      </details>
    </section>
  </article>;
}

export default function OperationCataloguePage() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]), [workspaceId, setWorkspaceId] = useState("");
  const [draft, setDraft] = useState(""), [query, setQuery] = useState("");
  const [kind, setKind] = useState<OperationKind | "">(""), [state, setState] = useState<OperationState | "">("");
  const [after, setAfter] = useState<string | null>(null), [history, setHistory] = useState<(string | null)[]>([]);
  const [listing, setListing] = useState<OperationListing | null>(null), [detail, setDetail] = useState<OperationDetail | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null), [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false), [detailBusy, setDetailBusy] = useState(false), [paused, setPaused] = useState(false);
  const [notice, setNotice] = useState("Loading authorized workspaces…"), [detailNotice, setDetailNotice] = useState("");
  const listGeneration = useRef(0), detailGeneration = useRef(0), currentWorkspace = useRef("");
  const listRequest = useRef<AbortController | null>(null), detailRequest = useRef<AbortController | null>(null);
  const pageScope = useRef<{ principalId: string; snapshotSha256: string } | null>(null);

  const invalidate = useCallback(() => {
    listGeneration.current++; detailGeneration.current++;
    listRequest.current?.abort(); detailRequest.current?.abort();
    setListing(null); setDetail(null); setSelectedId(null); setDetailNotice(""); setDetailBusy(false);
  }, []);
  const restart = useCallback(() => {
    invalidate(); pageScope.current = null; setAfter(null); setHistory([]); setPaused(false); setRefresh(value => value + 1);
  }, [invalidate]);
  const changed = useCallback(() => {
    invalidate(); pageScope.current = null; setAfter(null); setHistory([]); setPaused(true); setBusy(false);
    setNotice("The catalogue changed while you were reading it. Refresh operations to start a new snapshot.");
  }, [invalidate]);

  useEffect(() => {
    const request = new AbortController();
    fetch("/api/ui/bootstrap", { credentials: "same-origin", cache: "no-store", signal: request.signal })
      .then(async response => { if (!response.ok) throw new Error("unavailable"); return response.json(); })
      .then(value => {
        if (request.signal.aborted) return;
        if (!Array.isArray(value.workspaces) || value.workspaces.length > 12) throw new Error("invalid");
        const known: Workspace[] = value.workspaces.map((entry: unknown) => {
          if (!entry || typeof entry !== "object" || Array.isArray(entry)) throw new Error("invalid");
          const row = entry as Record<string, unknown>;
          if (typeof row.id !== "string" || !/^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(row.id) ||
              typeof row.name !== "string" || !row.name.trim() || row.name.length > 80) throw new Error("invalid");
          return { id: row.id, name: row.name };
        });
        if (new Set(known.map(entry => entry.id)).size !== known.length) throw new Error("invalid");
        setWorkspaces(known);
        const requested = new URLSearchParams(window.location.search).get("workspace");
        const selected = requested ? known.find(entry => entry.id === requested) : known[0];
        if (selected) { currentWorkspace.current = selected.id; setWorkspaceId(selected.id); }
        else setNotice(known.length ? "Select an authorized workspace to inspect its operations." : "No authorized workspace is configured.");
      }).catch(() => { if (!request.signal.aborted) setNotice("Sign in to Potbot to load your authorized workspace catalogue."); });
    return () => request.abort();
  }, []);

  useEffect(() => {
    if (!workspaceId || paused) return;
    const request = new AbortController(), generation = ++listGeneration.current;
    listRequest.current = request;
    setBusy(true); setNotice("Loading operations…"); setListing(null); setDetail(null); setSelectedId(null);
    const search = catalogueSearch({ q: query, kind, state, after, limit: 25 });
    void (async () => {
      try {
        const response = await fetch(`/api/ui/operations?${search}`, { credentials: "same-origin", cache: "no-store",
          headers: workspaceHeaders(workspaceId), signal: request.signal });
        if (request.signal.aborted || generation !== listGeneration.current || currentWorkspace.current !== workspaceId) return;
        if (response.status === 409) { changed(); return; }
        if (!response.ok) throw new Error("unavailable");
        const result = projectOperationListing(await response.json(), workspaceId);
        if (request.signal.aborted || generation !== listGeneration.current || currentWorkspace.current !== workspaceId) return;
        if (!result) throw new Error("invalid");
        if (pageScope.current && (result.principalId !== pageScope.current.principalId || result.snapshotSha256 !== pageScope.current.snapshotSha256)) { changed(); return; }
        pageScope.current = { principalId: result.principalId, snapshotSha256: result.snapshotSha256 };
        setListing(result); setNotice("");
      } catch { if (!request.signal.aborted && generation === listGeneration.current) setNotice("Operations are unavailable. Refresh after checking the workspace connection. No readiness is inferred from missing data."); }
      finally { if (!request.signal.aborted && generation === listGeneration.current) setBusy(false); }
    })();
    return () => request.abort();
  }, [workspaceId, query, kind, state, after, refresh, paused, changed]);

  useEffect(() => {
    if (!selectedId || !listing || paused) return;
    const request = new AbortController(), generation = ++detailGeneration.current;
    detailRequest.current = request; setDetail(null); setDetailBusy(true); setDetailNotice("");
    void (async () => {
      try {
        const response = await fetch(`/api/ui/operations/${selectedId}`, { credentials: "same-origin", cache: "no-store",
          headers: workspaceHeaders(workspaceId), signal: request.signal });
        if (request.signal.aborted || generation !== detailGeneration.current || currentWorkspace.current !== workspaceId) return;
        if (response.status === 409) { changed(); return; }
        if (!response.ok) throw new Error("unavailable");
        const result = projectOperationDetail(await response.json(), workspaceId, selectedId);
        if (request.signal.aborted || generation !== detailGeneration.current || currentWorkspace.current !== workspaceId) return;
        if (!result) throw new Error("invalid");
        if (result.principalId !== listing.principalId || result.snapshotSha256 !== listing.snapshotSha256) { changed(); return; }
        setDetail(result);
      } catch { if (!request.signal.aborted && generation === detailGeneration.current) setDetailNotice("This operation could not be read in the current workspace. Select it again or refresh the catalogue."); }
      finally { if (!request.signal.aborted && generation === detailGeneration.current) setDetailBusy(false); }
    })();
    return () => request.abort();
  }, [selectedId, listing, workspaceId, paused, changed]);

  const selectWorkspace = (id: string) => {
    currentWorkspace.current = id; restart(); setWorkspaceId(id);
    const url = new URL(window.location.href); url.searchParams.set("workspace", id);
    window.history.replaceState(null, "", url);
  };
  const selectOperation = (id: string) => {
    detailGeneration.current++; detailRequest.current?.abort(); setDetail(null); setDetailNotice(""); setDetailBusy(false);
    setSelectedId(id === selectedId ? null : id);
  };
  const page = (cursor: string | null, previous: boolean) => {
    invalidate(); setHistory(values => previous ? values.slice(0, -1) : [...values, after]); setAfter(cursor);
  };

  return <main className={styles.shell}>
    <header className={styles.header}>
      <a className={styles.back} href="/">← Workspace</a>
      <div className={styles.titleLine}><div><span className={styles.eyebrow}>POTBOT / OPERATIONS</span><h1>Inspect, check and run.</h1></div><span className={styles.readOnly}>Operation workbench</span></div>
      <p>Explore tools, models and workflows together. Check an exact task request on its worker, explicitly launch supported operations, and follow their durable results.</p>
    </header>
    <section className={styles.controls} aria-label="Catalogue filters">
      <div className={styles.controlTop}>
        <label>Workspace<select value={workspaceId} onChange={event => selectWorkspace(event.target.value)}>
          {!workspaceId && <option value="">Choose a workspace</option>}
          {workspaces.map(workspace => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}
        </select></label>
        <button className={styles.refresh} type="button" disabled={!workspaceId || busy} onClick={restart}>↻ Refresh operations</button>
      </div>
      <form className={styles.filterForm} onSubmit={event => { event.preventDefault(); restart(); setQuery(draft.trim()); }}>
        <label className={styles.search}>Search operations<input type="search" maxLength={160} value={draft} onChange={event => setDraft(event.target.value)} placeholder="Name or operation reference" /></label>
        <label>Kind<select value={kind} onChange={event => { restart(); setKind(event.target.value as OperationKind | ""); }}>
          <option value="">All kinds</option>{OPERATION_KINDS.map(value => <option key={value} value={value}>{value}</option>)}
        </select></label>
        <label>Readiness<select value={state} onChange={event => { restart(); setState(event.target.value as OperationState | ""); }}>
          <option value="">All states</option>{OPERATION_STATES.map(value => <option key={value} value={value}>{READINESS_LABELS[value]}</option>)}
        </select></label>
        <button className={styles.searchButton} disabled={!workspaceId || busy}>Search</button>
      </form>
    </section>
    <p className={styles.scopeNote}>Readiness describes retained observations. Execution still requires current operator policy and host checks. Recent accounting is partial; unavailable evidence stays unknown.</p>
    <div className={styles.status} role="status" aria-live="polite">{notice || (listing ? `${listing.items.length} entries on this page · snapshot ${short(listing.snapshotSha256)}` : "")}</div>
    <div className={styles.content}>
      <section className={styles.listPanel} aria-label="Operation catalogue" aria-busy={busy}>
        <div className={styles.listHeader}><h2>Operations</h2>{listing && <span>{listing.items.filter(item => attention(item.state)).length} need attention on this page</span>}</div>
        {listing && listing.items.length > 0 ? <ul className={styles.operationList}>{listing.items.map(item => <li key={item.id}>
          <button className={`${styles.operation} ${selectedId === item.id ? styles.selected : ""}`} type="button" aria-pressed={selectedId === item.id} onClick={() => selectOperation(item.id)}>
            <div className={styles.rowTitle}><strong>{item.name}</strong><span className={styles.kind}>{item.kind}</span></div>
            <div className={styles.badges}><Readiness state={item.state} />{item.updateAvailable === true && <span className={styles.update}>Update available</span>}</div>
            <span className={styles.rowReason}>{item.reasons.length ? operationReason(orderedOperationReasons(item.reasons)[0]) : "Inspect the recorded identity and evidence."}</span>
            <span className={styles.rowVersion}>{item.version ? `Version ${item.version}` : "Version not recorded"}<span aria-hidden="true">↗</span></span>
          </button>
        </li>)}</ul> : <div className={styles.empty}>
          <span className={styles.emptyMark} aria-hidden="true">◎</span>
          <h3>{busy ? "Reading the workspace…" : listing ? "No matching operations" : paused ? "A fresh snapshot is needed" : "No catalogue loaded"}</h3>
          <p>{listing ? "Try a different name, kind or readiness filter. An empty result does not prove that every runtime has been discovered." : "Workspace records will appear here once the authenticated catalogue is available."}</p>
        </div>}
        <nav className={styles.pagination} aria-label="Catalogue pages">
          <button type="button" disabled={!history.length || busy || paused} onClick={() => page(history.at(-1) ?? null, true)}>← Previous</button>
          <span>Page {history.length + 1}</span>
          <button type="button" disabled={!listing?.nextCursor || busy || paused} onClick={() => page(listing?.nextCursor ?? null, false)}>Next →</button>
        </nav>
      </section>
      <aside className={styles.detailPanel} aria-label="Selected operation" aria-busy={detailBusy}>
        {detail ? <Detail item={detail.item} workspaceId={workspaceId} principalId={detail.principalId} snapshot={detail.snapshotSha256} /> : <div className={styles.empty}>
          <span className={styles.emptyMark} aria-hidden="true">↗</span>
          <h2>{detailBusy ? "Reading operation details…" : "Follow the evidence"}</h2>
          <p role="status">{detailNotice || "Select an operation to inspect readiness reasons, provenance, release records and recent linked accounting."}</p>
        </div>}
      </aside>
    </div>
    <footer className={styles.footer}><span>One view over existing workspace records.</span><a href={`/discover${workspaceId ? `?workspace=${encodeURIComponent(workspaceId)}` : ""}`}>Capability discovery ↗</a><a href={`/discover/mcp${workspaceId ? `?workspace=${encodeURIComponent(workspaceId)}` : ""}`}>Admitted MCP catalogues ↗</a><a href={`/skills${workspaceId ? `?workspace=${encodeURIComponent(workspaceId)}` : ""}`}>Skill workshop ↗</a><a href={`/setup/tools${workspaceId ? `?workspace=${encodeURIComponent(workspaceId)}` : ""}`}>Tool upgrade review ↗</a><a href="/setup/operator">Operator health ↗</a></footer>
  </main>;
}
