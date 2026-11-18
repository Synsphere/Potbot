"use client";

import { useEffect, useState } from "react";
import { UUID } from "../../../lib/tool-acquisition.ts";
import { acquisitionSelection } from "../../../lib/acquisition-handoff.ts";
import ToolWorkbench from "./ToolWorkbench.tsx";
import LegacyToolApprovals from "./LegacyToolApprovals.tsx";
import styles from "../../skills/skills.module.css";

type Workspace = { id: string; name: string };
export default function ToolAcquisitionPage() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]), [workspaceId, setWorkspaceId] = useState(""), [tab, setTab] = useState<"acquisition" | "legacy">("acquisition");
  const [initialOperationId, setInitialOperationId] = useState<string | null>(null);
  const [notice, setNotice] = useState("Loading authorized workspaces…");
  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/ui/bootstrap", { credentials: "same-origin", cache: "no-store", signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error("unavailable"); const value = await response.json(); if (controller.signal.aborted) return;
      if (!Array.isArray(value.workspaces) || value.workspaces.length > 12) throw new Error("invalid");
      const known: Workspace[] = value.workspaces.map((raw: unknown) => {
        if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("invalid"); const row = raw as Record<string, unknown>;
        if (typeof row.id !== "string" || !UUID.test(row.id) || typeof row.name !== "string" || !row.name.trim() || row.name.length > 80) throw new Error("invalid");
        return { id: row.id, name: row.name };
      });
      if (new Set(known.map(item => item.id)).size !== known.length) throw new Error("invalid"); setWorkspaces(known);
      const requested = new URLSearchParams(window.location.search).get("workspace"), selected = requested ? known.find(item => item.id === requested) : known[0];
      if (selected) { setWorkspaceId(selected.id); setInitialOperationId(acquisitionSelection(window.location.search, selected.id, "operation")); setNotice(""); } else setNotice(known.length ? "Select an authorized workspace to inspect executable acquisitions." : "No authorized workspace is configured.");
    }).catch(() => { if (!controller.signal.aborted) setNotice("Sign in to Potbot to open the tool acquisition workbench."); });
    return () => controller.abort();
  }, []);
  const query = workspaceId ? `?workspace=${encodeURIComponent(workspaceId)}` : "";
  return <main className={styles.shell}><header className={styles.header}><a className={styles.back} href={`/${query}`}>← Workspace</a>
    <div className={styles.eyebrow}>POTBOT / EXECUTABLE TOOL WORKBENCH</div><h1>Acquire, qualify and release a tool</h1>
    <p>Choose an operator recipe and exact source revision. Review the sandbox build, approve tested promotion, and follow the independent host checks through release or rollback.</p>
    <nav className={styles.links} aria-label="Related workspace controls"><a href={`/discover${query}`}>Discover and review capabilities ↗</a><a href={`/tools${query}`}>Operations and controlled checks ↗</a><a href={`/skills${query}`}>Portable skill packages ↗</a><a href="/setup/operator">Operator health ↗</a></nav></header>
    <div className={styles.toolbar}><label>Workspace<select value={workspaceId} onChange={event => {
      setWorkspaceId(event.target.value); setInitialOperationId(null); setNotice(""); const url = new URL(window.location.href); url.searchParams.set("workspace", event.target.value); url.searchParams.delete("operation"); window.history.replaceState(null, "", url);
    }}><option value="" disabled>Select workspace</option>{workspaces.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <span className={styles.help}>Source, build and host actions remain scoped to this workspace and authenticated principal.</span></div>
    <div className={styles.tabs}><button type="button" aria-pressed={tab === "acquisition"} onClick={() => setTab("acquisition")}>Acquisition workbench</button><button type="button" aria-pressed={tab === "legacy"} onClick={() => setTab("legacy")}>Existing tested approvals</button></div>
    {notice && <p className={styles.status} role="status">{notice}</p>}{workspaceId && (tab === "acquisition" ? <ToolWorkbench key={workspaceId} workspaceId={workspaceId} initialOperationId={initialOperationId} /> : <LegacyToolApprovals key={workspaceId} workspaceId={workspaceId} />)}
    <footer className={styles.footer}><span>Existing sandbox, lease, approval and release owners govern every executable change.</span><a href={`/tools${query}`}>Inspect current operation eligibility ↗</a></footer>
  </main>;
}
