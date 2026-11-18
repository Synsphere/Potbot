"use client";

import { useEffect, useState } from "react";
import { UUID } from "../../../lib/operation-workbench.ts";
import { mcpBindingSelection } from "../../../lib/mcp-discovery-client.ts";
import McpDiscoveryWorkbench from "./McpDiscoveryWorkbench.tsx";
import styles from "../../skills/skills.module.css";

type Workspace = { id: string; name: string };

export default function McpDiscoveryPage() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]), [workspaceId, setWorkspaceId] = useState(""), [initialBindingId, setInitialBindingId] = useState<string | null>(null);
  const [notice, setNotice] = useState("Loading authorized workspaces…");
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
      if (selected) { setWorkspaceId(selected.id); setInitialBindingId(mcpBindingSelection(window.location.search, selected.id)); setNotice(""); }
      else setNotice(known.length ? "Select an authorized workspace to inspect retained MCP catalogues." : "No authorized workspace is configured.");
    }).catch(() => { if (!request.signal.aborted) setNotice("Sign in to Potbot to inspect admitted MCP bindings."); });
    return () => request.abort();
  }, []);
  const query = workspaceId ? `?workspace=${encodeURIComponent(workspaceId)}` : "";
  return <main className={styles.shell}><header className={styles.header}><a className={styles.back} href={`/discover${query}`}>← Capability discovery</a>
    <div className={styles.eyebrow}>POTBOT / LIVE MCP CATALOGUES</div><h1>Inspect an admitted MCP installation</h1>
    <p>Review the retained tools, resources and prompts advertised by an exact installed binding. Reconcile changes through the configured host and keep every reference tied to its current schema and expiry.</p>
    <nav className={styles.links} aria-label="Related workspace controls"><a href={`/discover${query}`}>Registry discovery ↗</a><a href={`/tools${query}`}>Operations and controlled checks ↗</a><a href="/setup/operator">Operator health ↗</a></nav></header>
    <div className={styles.toolbar}><label>Workspace<select value={workspaceId} onChange={event => {
      setWorkspaceId(event.target.value); setInitialBindingId(null); setNotice(""); const url = new URL(window.location.href); url.searchParams.set("workspace", event.target.value); url.searchParams.delete("binding"); window.history.replaceState(null, "", url);
    }}><option value="" disabled>Select workspace</option>{workspaces.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><span className={styles.help}>Bindings and retained catalogue observations belong to this workspace and authenticated principal.</span></div>
    {notice && <p className={styles.status} role="status">{notice}</p>}{workspaceId && <McpDiscoveryWorkbench key={workspaceId} workspaceId={workspaceId} initialBindingId={initialBindingId} />}
    <footer className={styles.footer}><span>Advertised schemas describe capabilities. Existing admission and task grants govern their use.</span><a href={`/tools${query}`}>Inspect operation eligibility ↗</a></footer>
  </main>;
}
