"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { graphList, graphStatus, type GraphList, type GraphStatus } from "../../lib/atomic-graphs.ts";

type Workspace = { id: string; name: string };
const wsHeader = (id: string) => ({ "X-Potbot-Workspace": id });
const terminal = (outcome: string) => ["succeeded", "interrupted", "blocked", "needs_reconciliation"].includes(outcome);

export default function AtomicGraphWorkbench() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspace, setWorkspace] = useState("");
  const [history, setHistory] = useState<GraphList | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [status, setStatus] = useState<GraphStatus | null>(null);
  const [notice, setNotice] = useState("Loading authorized workspaces…");
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const generation = useRef(0);
  const pending = useRef(false);

  useEffect(() => {
    let active = true;
    void fetch("/api/ui/bootstrap", { cache: "no-store" })
      .then(async response => {
        if (!response.ok) throw new Error("Workspace authentication is unavailable");
        const body: unknown = await response.json();
        const item = body && typeof body === "object" ? body as { workspaces?: unknown } : null;
        if (!Array.isArray(item?.workspaces) || item.workspaces.some(w =>
          !w || typeof w.id !== "string" || typeof w.name !== "string")) throw new Error("Invalid workspace list");
        return item.workspaces as Workspace[];
      })
      .then(items => { if (active) { setWorkspaces(items); setWorkspace(items[0]?.id ?? ""); setNotice(items.length ? "Choose a graph to inspect." : "No configured workspaces."); } })
      .catch(() => { if (active) setNotice("Cannot verify authorized workspaces."); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const epoch = ++generation.current;
    const controller = new AbortController();
    setSelected(null); setStatus(null); setHistory(null); setUncertain(false);
    if (!workspace) return () => controller.abort();
    void fetch("/api/ui/atomic-graphs?limit=25", {
      cache: "no-store", headers: wsHeader(workspace), signal: controller.signal,
    }).then(async response => {
      if (!response.ok) throw new Error("Graph history unavailable");
      const page = graphList(await response.json(), workspace);
      if (!page) throw new Error("Graph ownership or history format changed");
      if (!controller.signal.aborted && generation.current === epoch) {
        setHistory(page); setNotice(page.items.length ? "Select a graph." : "No admitted atomic graphs.");
      }
    }).catch(() => { if (!controller.signal.aborted && generation.current === epoch) setNotice("Cannot read verified graph history."); });
    return () => controller.abort();
  }, [workspace, refresh]);

  const loadStatus = useCallback(async (id: string, signal?: AbortSignal): Promise<GraphStatus> => {
    const response = await fetch(`/api/ui/atomic-graphs/${id}`, {
      cache: "no-store", headers: wsHeader(workspace), signal,
    });
    if (!response.ok) throw new Error("Graph status unavailable or scope changed");
    const value = graphStatus(await response.json(), id);
    if (!value) throw new Error("Graph status did not match the selected graph");
    return value;
  }, [workspace]);

  useEffect(() => {
    if (!selected || !workspace) return;
    const controller = new AbortController();
    let sequence = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const observed = await loadStatus(selected, controller.signal);
        if (controller.signal.aborted) return;
        setStatus(observed); sequence++;
        if (!terminal(observed.outcome) && sequence < 900) timer = setTimeout(() => void tick(), 3000);
      } catch {
        if (!controller.signal.aborted) setNotice("Monitoring paused. Refresh the graph before taking further action.");
      }
    };
    void tick();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [selected, workspace, loadStatus, refresh]);

  async function action(kind: "tick" | "interrupt") {
    if (!selected || !workspace || !history || pending.current ||
        !history.principal_id || !status || status.state === "interrupted") return;
    if (kind === "interrupt" && !window.confirm("Interrupt this graph? In-flight Cog effects may still be unresolved.")) return;
    if (kind === "tick" && !window.confirm("Submit up to 8 already checked eligible operations?")) return;
    pending.current = true; setBusy(true); setUncertain(false);
    const id = selected;
    try {
      const response = await fetch(`/api/ui/atomic-graphs/${id}/${kind}`, {
        method: "POST", cache: "no-store", credentials: "same-origin",
        headers: { ...wsHeader(workspace), "Content-Type": "application/json",
          "X-Potbot-Expected-Principal": history.principal_id, "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify(kind === "tick" ? { confirm: true, limit: 8 } : { confirm: true }),
      });
      if (!response.ok) throw new Error(`Graph operation rejected (${response.status}).`);
      setNotice(kind === "tick" ? "A scheduler pass was requested. Read the committed job states for its outcome." :
        "Dispatch is fenced. Existing effects may need reconciliation.");
      setStatus(await loadStatus(id));
    } catch {
      setUncertain(true);
      setNotice("The command outcome is uncertain. Inspect the graph and its jobs before issuing another command. No automatic retry will occur.");
    } finally { pending.current = false; setBusy(false); }
  }

  const options = history?.items || [];
  return <main style={{ maxWidth: 1080, margin: "1.5rem auto", padding: "1rem", lineHeight: 1.5 }}>
    <h1>Atomic graph scheduler</h1>
    <p>Monitor checked operation graphs, dependencies, worker jobs and resource holds. This view does not grant a Cog new authority.</p>
    <label htmlFor="atomic-workspace">Workspace </label>
    <select id="atomic-workspace" value={workspace} onChange={event => setWorkspace(event.target.value)}>
      {workspaces.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
    </select>{" "}
    <button type="button" onClick={() => setRefresh(value => value + 1)} disabled={!workspace || busy}>Refresh history</button>
    <p role="status">{notice}</p>
    {uncertain && <p role="alert">The last mutation has not been confirmed; check retained job evidence before attempting it again.</p>}
    <section aria-label="Admitted graphs">
      <h2>Graph history</h2>
      {!options.length && <p>No graphs in the current page.</p>}
      <ul>{options.map(item => <li key={item.graph_id}>
        <button type="button" onClick={() => { setSelected(item.graph_id); setStatus(null); }}
          aria-pressed={selected === item.graph_id}>{item.objective.slice(0, 180)}</button>
        {" · "}{item.state} · {new Date(item.created_at).toLocaleString()}
      </li>)}</ul>
      {history?.has_more && <p>More graphs exist. Use the CLI with the retained cursor to inspect older pages.</p>}
    </section>
    {selected && <section aria-label="Selected graph">
      <h2>Graph {selected.slice(0, 12)}</h2>
      {status ? <>
        <p>Outcome: <strong>{status.outcome.replaceAll("_", " ")}</strong> · maximum parallel: {status.max_parallel}</p>
        <p>Manifest: <code>{status.manifest_hash}</code></p>
        <button type="button" disabled={busy || status.state !== "active" || uncertain} onClick={() => void action("tick")}>Run checked scheduler tick</button>{" "}
        <button type="button" disabled={busy || status.state !== "active" || uncertain} onClick={() => void action("interrupt")}>Interrupt graph</button>
        <p>{status.outcome === "needs_reconciliation" ? "A worker outcome is indeterminate. Resource locks remain held until independently reconciled." : ""}</p>
        <div style={{ overflowX: "auto" }}><table style={{ borderCollapse: "collapse", width: "100%" }}>
          <thead><tr><th align="left">Leaf</th><th align="left">Operation</th><th align="left">Dependencies</th><th align="left">State</th><th align="left">Job / resources</th></tr></thead>
          <tbody>{status.nodes.map(node => <tr key={node.node_id}>
            <td>{node.node_id}</td><td>{node.operation_name}</td>
            <td>{node.dependencies.join(", ") || "Independent"}</td>
            <td>{node.state}{node.possible_effect ? " · possibly dispatched" : ""}</td>
            <td>{node.job_id ? <code>{node.job_id}</code> : "Unlaunched"} · {node.resource_holds} locks</td>
          </tr>)}</tbody>
        </table></div>
      </> : <p>Reading selected graph…</p>}
    </section>}
  </main>;
}
