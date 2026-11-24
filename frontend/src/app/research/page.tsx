"use client";

import { useEffect, useState } from "react";
import { UUID } from "../../lib/operation-workbench.ts";
import { EMPTY_RESEARCH_SELECTION, researchSelection, type ResearchSelection } from "../../lib/research-client.ts";
import ResearchWorkbench from "./ResearchWorkbench.tsx";
import styles from "../skills/skills.module.css";

type Workspace = { id: string; name: string };
export default function ResearchPage() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]), [workspaceId, setWorkspaceId] = useState("");
  const [initialSelection, setInitialSelection] = useState<ResearchSelection>(EMPTY_RESEARCH_SELECTION), [notice, setNotice] = useState("Loading authorized workspaces…");
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
      if (selected) { setWorkspaceId(selected.id); setInitialSelection(researchSelection(window.location.search, selected.id)); setNotice(""); }
      else setNotice(known.length ? "Select an authorized workspace to inspect research." : "No authorized workspace is configured.");
    }).catch(() => { if (!request.signal.aborted) setNotice("Sign in to Potbot to open research and cited evidence."); });
    return () => request.abort();
  }, []);
  const query = workspaceId ? `?workspace=${encodeURIComponent(workspaceId)}` : "";
  return <main className={styles.shell}><header className={styles.header}><a className={styles.back} href="/">← Workspace</a>
    <div className={styles.eyebrow}>POTBOT / RESEARCH &amp; CITED EVIDENCE</div><h1>Investigate a question. Inspect its sources.</h1>
    <p>Review the task and information need, approve an exact external request, then inspect retained sources and literal citation evidence before comparing claims.</p>
    <nav className={styles.links} aria-label="Related workspace controls"><a href={`/discover/tasks${query}`}>Task-aware capability shortlist ↗</a><a href={`/tools${query}`}>Operations and checked execution ↗</a><a href={`/context${query}`}>Workspace context ↗</a><a href={`/checked-reasoning${query}`}>Checked reasoning ↗</a></nav></header>
    <div className={styles.toolbar}><label>Workspace<select value={workspaceId} onChange={event => {
      setWorkspaceId(event.target.value); setInitialSelection({ ...EMPTY_RESEARCH_SELECTION }); setNotice(""); const url = new URL(window.location.href); url.searchParams.set("workspace", event.target.value); for (const key of ["episode", "state", "need", "run"]) url.searchParams.delete(key); window.history.replaceState(null, "", url);
    }}><option value="" disabled>Select workspace</option>{workspaces.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><span className={styles.help}>Research questions, outgoing queries and retained sources are shared within this workspace.</span></div>
    {notice && <p className={styles.status} role="status">{notice}</p>}{workspaceId && <ResearchWorkbench key={workspaceId} workspaceId={workspaceId} initialSelection={initialSelection} />}
    <footer className={styles.footer}><span>Citation checks establish exact source agreement or conflict. They do not automatically resolve an information need.</span><a href={`/setup/operator${query}`}>Inspect operator configuration ↗</a></footer>
  </main>;
}
