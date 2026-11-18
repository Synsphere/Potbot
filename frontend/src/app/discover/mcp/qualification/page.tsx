"use client";

import { useEffect, useState } from "react";
import { UUID } from "../../../../lib/operation-workbench.ts";
import { mcpSuiteSelection, mcpQualificationBindingSelection } from "../../../../lib/mcp-qualification-client.ts";
import McpQualificationWorkbench from "./McpQualificationWorkbench.tsx";
import styles from "../../../skills/skills.module.css";

type Workspace = { id: string; name: string };

export default function McpQualificationPage() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]), [workspaceId, setWorkspaceId] = useState(""), [initialSuiteId, setInitialSuiteId] = useState<string | null>(null);
  const [initialBindingId, setInitialBindingId] = useState<string | null>(null);
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
      if (selected) { setWorkspaceId(selected.id); setInitialSuiteId(mcpSuiteSelection(window.location.search, selected.id)); setInitialBindingId(mcpQualificationBindingSelection(window.location.search, selected.id)); setNotice(""); }
      else setNotice(known.length ? "Select an authorized workspace to inspect MCP qualification evidence." : "No authorized workspace is configured.");
    }).catch(() => { if (!request.signal.aborted) setNotice("Sign in to Potbot to inspect MCP qualification suites."); });
    return () => request.abort();
  }, []);
  const query = workspaceId ? `?workspace=${encodeURIComponent(workspaceId)}` : "";
  return <main className={styles.shell}><header className={styles.header}><a className={styles.back} href={`/discover/mcp${query}`}>← Admitted MCP catalogues</a>
    <div className={styles.eyebrow}>POTBOT / MCP TASK QUALIFICATION</div><h1>Review an installed tool’s task evidence</h1>
    <p>Inspect an admitted qualification suite, request one checked attempt and review its retained outcomes. The configured host runs each exact case through the existing execution owners.</p>
    <nav className={styles.links} aria-label="Related workspace controls"><a href={`/discover/mcp${query}`}>MCP catalogues ↗</a><a href={`/tools${query}`}>Operations and controlled checks ↗</a><a href="/setup/operator">Operator health ↗</a></nav></header>
    <div className={styles.toolbar}><label>Workspace<select value={workspaceId} onChange={event => {
      setWorkspaceId(event.target.value); setInitialSuiteId(null); setInitialBindingId(null); setNotice(""); const url = new URL(window.location.href); url.searchParams.set("workspace", event.target.value); url.searchParams.delete("suite"); url.searchParams.delete("binding"); window.history.replaceState(null, "", url);
    }}><option value="" disabled>Select workspace</option>{workspaces.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><span className={styles.help}>Suites and retained task evidence belong to this workspace and authenticated principal.</span></div>
    {notice && <p className={styles.status} role="status">{notice}</p>}{workspaceId && <McpQualificationWorkbench key={workspaceId} workspaceId={workspaceId} initialSuiteId={initialSuiteId} initialBindingId={initialBindingId} />}
    <footer className={styles.footer}><span>Qualification evidence applies to the exact reviewed installation and task cases.</span><a href={`/discover/mcp${query}`}>Inspect current MCP interfaces ↗</a></footer>
  </main>;
}
