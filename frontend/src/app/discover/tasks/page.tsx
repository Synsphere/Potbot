"use client";

import { useEffect, useState } from "react";
import { UUID } from "../../../lib/task-discovery.ts";
import { taskDiscoverySelection } from "../../../lib/task-discovery-client.ts";
import TaskDiscoveryWorkbench from "./TaskDiscoveryWorkbench.tsx";
import styles from "../../skills/skills.module.css";

type Workspace = { id: string; name: string };
const EMPTY_SELECTION = { episode: null, state: null, search: null };
export default function TaskDiscoveryPage() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]), [workspaceId, setWorkspaceId] = useState("");
  const [initialSelection, setInitialSelection] = useState<{ episode: string | null; state: string | null; search: string | null }>(EMPTY_SELECTION), [notice, setNotice] = useState("Loading authorized workspaces…");
  useEffect(() => {
    const request = new AbortController();
    void fetch("/api/ui/bootstrap", { credentials: "same-origin", cache: "no-store", signal: request.signal }).then(async response => {
      if (!response.ok) throw new Error("unavailable"); const value = await response.json(); if (request.signal.aborted) return;
      if (!Array.isArray(value.workspaces) || value.workspaces.length > 12) throw new Error("invalid");
      const known: Workspace[] = value.workspaces.map((raw: unknown) => {
        if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("invalid"); const row = raw as Record<string, unknown>;
        if (typeof row.id !== "string" || !UUID.test(row.id) || typeof row.name !== "string" || !row.name.trim() || row.name.length > 80) throw new Error("invalid");
        return { id: row.id, name: row.name };
      });
      if (new Set(known.map(item => item.id)).size !== known.length) throw new Error("invalid"); setWorkspaces(known);
      const params = new URLSearchParams(window.location.search), requested = params.get("workspace"), selected = params.getAll("workspace").length > 1 ? undefined : requested ? known.find(item => item.id === requested) : known[0];
      if (selected) { setWorkspaceId(selected.id); setInitialSelection(taskDiscoverySelection(window.location.search, selected.id)); setNotice(""); }
      else setNotice(known.length ? "Select an authorized workspace to inspect task discovery." : "No authorized workspace is configured.");
    }).catch(() => { if (!request.signal.aborted) setNotice("Sign in to Potbot to open task-aware discovery."); });
    return () => request.abort();
  }, []);
  const query = workspaceId ? `?workspace=${encodeURIComponent(workspaceId)}` : "";
  return <main className={styles.shell}><header className={styles.header}><a className={styles.back} href={`/discover${query}`}>← Capability discovery</a>
    <div className={styles.eyebrow}>POTBOT / TASK-AWARE DISCOVERY</div><h1>Find a capability for this exact task</h1>
    <p>Review a task-bound shortlist across retained registry observations, installed operations and live MCP references. Inspect hard exclusions, typed compatibility and matching qualification evidence before retaining a selection.</p>
    <nav className={styles.links} aria-label="Related workspace controls"><a href={`/discover${query}`}>Registry and source review ↗</a><a href={`/tools${query}`}>Operations and controlled checks ↗</a><a href={`/discover/mcp${query}`}>Admitted MCP catalogues ↗</a><a href={`/discover/mcp/qualification${query}`}>MCP task qualification ↗</a></nav></header>
    <div className={styles.toolbar}><label>Workspace<select value={workspaceId} onChange={event => {
      setWorkspaceId(event.target.value); setInitialSelection(EMPTY_SELECTION); setNotice(""); const url = new URL(window.location.href); url.searchParams.set("workspace", event.target.value); for (const key of ["episode", "state", "search"]) url.searchParams.delete(key); window.history.replaceState(null, "", url);
    }}><option value="" disabled>Select workspace</option>{workspaces.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><span className={styles.help}>Searches, exact commands and evidence are scoped to this workspace and authenticated principal.</span></div>
    {notice && <p className={styles.status} role="status">{notice}</p>}{workspaceId && <TaskDiscoveryWorkbench key={workspaceId} workspaceId={workspaceId} initialSelection={initialSelection} />}
    <footer className={styles.footer}><span>Discovery and selection grant no execution permission. Existing policy, capability and budget owners remain authoritative.</span><a href={`/tools${query}`}>Inspect current operation eligibility ↗</a></footer>
  </main>;
}
