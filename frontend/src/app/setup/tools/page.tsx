"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { releaseStatus, upgradeListing, type UpgradeRecord } from "../../../lib/tool-upgrades.ts";
import styles from "../apis/setup.module.css";

type Workspace = { id: string; name: string };
const headers = (id: string): HeadersInit => ({ "X-Potbot-Workspace": id });

export default function ToolUpgradeSetup() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceId, setWorkspaceId] = useState("");
  const [items, setItems] = useState<UpgradeRecord[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("Loading approved workspaces…");
  const generation = useRef(0), currentWorkspace = useRef("");
  const readRequest = useRef<AbortController | null>(null), mutationRequest = useRef<AbortController | null>(null);

  const refresh = useCallback(async (workspace: string) => {
    const request = new AbortController(), expected = ++generation.current;
    readRequest.current?.abort(); readRequest.current = request;
    setItems([]); setNotice("Loading tool test results…");
    try {
      const r = await fetch("/api/ui/tools/upgrades", { credentials: "same-origin", cache: "no-store", headers: headers(workspace), signal: request.signal });
      if (!r.ok) throw new Error("unavailable");
      const data = await r.json();
      if (request.signal.aborted || expected !== generation.current || currentWorkspace.current !== workspace) return;
      const parsed = upgradeListing(data, workspace);
      if (data.workspaceId !== workspace || !parsed) throw new Error("invalid");
      setItems(parsed.items); setNotice("");
    } catch { if (!request.signal.aborted && expected === generation.current && currentWorkspace.current === workspace) setNotice("Tool upgrade records are unavailable. Check operator login and backend migrations."); }
  }, []);

  useEffect(() => {
    let active = true;
    fetch("/api/ui/bootstrap", { credentials: "same-origin", cache: "no-store" })
      .then(async r => { if (!r.ok) throw new Error("login"); return r.json(); })
      .then(data => {
        if (!active || !Array.isArray(data.workspaces)) return;
        const known = (data.workspaces as Workspace[]).filter(w => typeof w.id === "string" && typeof w.name === "string");
        setWorkspaces(known);
        const requested = new URLSearchParams(window.location.search).get("workspace");
        const selected = requested ? known.find(workspace => workspace.id === requested) : known[0];
        if (selected) { currentWorkspace.current = selected.id; setWorkspaceId(selected.id); void refresh(selected.id); }
        else setNotice(known.length ? "Select an authorized workspace to review tool upgrades." : "No authorized workspace configured.");
      }).catch(() => { if (active) setNotice("Sign in to the operator workspace to manage tool upgrades."); });
    return () => { active = false; generation.current++; readRequest.current?.abort(); mutationRequest.current?.abort(); };
  }, [refresh]);

  async function decide(entry: UpgradeRecord, action: "approve" | "reject") {
    if (busy || !workspaceId || entry.workspaceId !== workspaceId || entry.state !== "awaiting_approval") return;
    const verb = action === "approve" ? "approve activation of" : "reject";
    if (!window.confirm(`Do you want to ${verb} ${entry.slug} from ${entry.oldCommit.slice(0, 12)} to ${entry.newCommit.slice(0, 12)}?\n\nS9 verified ${entry.passedTests} tests. Tested evidence: ${entry.evidenceSha256}`)) return;
    setBusy(true); setNotice("Recording operator decision…");
    const selectedWorkspace = workspaceId, request = new AbortController(); mutationRequest.current = request;
    try {
      const response = await fetch(`/api/ui/tools/upgrades/${entry.id}/${action}`, {
        method: "POST", credentials: "same-origin", signal: request.signal,
        headers: { ...headers(workspaceId), "Content-Type": "application/json", "Origin": window.location.origin },
        body: JSON.stringify({ revision: entry.revision, evidenceSha256: entry.evidenceSha256, confirm: true }),
      });
      if (request.signal.aborted || currentWorkspace.current !== selectedWorkspace) return;
      if (!response.ok) throw new Error("cannot approve");
      await refresh(selectedWorkspace);
      if (request.signal.aborted || currentWorkspace.current !== selectedWorkspace) return;
      setNotice(action === "approve" ? "Upgrade approved. The host operator must verify the installed version and complete postflight before release." : "Upgrade rejected.");
    } catch { if (!request.signal.aborted && currentWorkspace.current === selectedWorkspace) setNotice("Approval failed or the evidence changed. Refresh and review before retrying."); }
    finally { if (!request.signal.aborted && currentWorkspace.current === selectedWorkspace) setBusy(false); }
  }

  return <main className={styles.shell}>
    <header className={styles.header}>
      <a href="/" className={styles.back}>← Return to Potbot</a>
      <span className={styles.kicker}>OPERATOR SETUP / SHARED TOOLS</span>
      <h1>Tool testing and upgrades</h1>
      <p>Review S9-tested source revisions and explicitly approve promotion. The browser cannot run installers or change executable hashes; an independent host worker enforces the upgrade.</p>
      <a className={styles.back} href={`/tools${workspaceId ? `?workspace=${encodeURIComponent(workspaceId)}` : ""}`}>View operation readiness and evidence ↗</a>
    </header>
    <section className={styles.toolbar} aria-label="Authorized workspace">
      <label>Workspace
        <select value={workspaceId} disabled={busy} onChange={e => {
          const selected = e.target.value; currentWorkspace.current = selected; setWorkspaceId(selected); void refresh(selected);
          const url = new URL(window.location.href); url.searchParams.set("workspace", selected); window.history.replaceState(null, "", url);
        }}>
          {!workspaceId && <option value="">Choose a workspace</option>}
          {workspaces.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
        </select>
      </label>
      <button type="button" disabled={!workspaceId || busy} onClick={() => void refresh(workspaceId)}>Reload test results</button>
    </section>
    <p role="status" className={styles.status}>{notice}</p>
    <section className={styles.cards} aria-label="Tested tool upgrades">
      {items.map(item => <article className={styles.card} key={item.id}>
        <div className={styles.cardHead}><h2>{item.slug}</h2><span className={styles.pill}>{item.state.replace(/_/g, " ")}</span></div>
        <p className={styles.subtle}>Revision {item.oldCommit.slice(0, 12)} → {item.newCommit.slice(0, 12)}</p>
        <p>Passed tests: <strong>{item.passedTests}</strong></p>
        <ul>{item.tests.map(test => <li key={test.name}>{test.name} · {test.kind} · {test.elapsedMs} ms · Passed</li>)}</ul>
        <p className={styles.subtle}>Test evidence SHA-256: <code>{item.evidenceSha256}</code></p>
        <p className={styles.subtle}>Test suite: <code>{item.suiteSha256.slice(0, 20)}</code></p>
        {item.state === "awaiting_approval" && <div className={styles.actions}>
          <button type="button" disabled={busy} onClick={() => void decide(item, "approve")}>Approve upgrade</button>
          <button type="button" disabled={busy} onClick={() => void decide(item, "reject")}>Reject</button>
        </div>}
        {item.state === "awaiting_repin" && (!item.releaseState || item.releaseState === "none") && <p className={styles.subtle}>Activation completed; awaiting host-reviewed runtime executable pins.</p>}
        <p role={releaseStatus(item).attention ? "alert" : undefined} className={styles.subtle}>
          <strong>{releaseStatus(item).label}.</strong> {releaseStatus(item).detail}
        </p>
        {item.state === "indeterminate" && <p role="alert">Operator reconciliation required. No automatic retry will occur.</p>}
      </article>)}
      {!items.length && !notice && <p>No tested upgrades have been published for this workspace.</p>}
    </section>
    <footer className={styles.footer}>S10 staging and S9 tests happen inside the qualified workspace. Approvals are stored durably and executed by the operator, not by this page.</footer>
  </main>;
}
