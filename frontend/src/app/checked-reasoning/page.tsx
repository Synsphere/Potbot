"use client";

import { useEffect, useRef, useState } from "react";
import { MAX_REASONING_PLAN, REASONING_ROLE_LABELS, guidanceConstraints, object, reasoningPlan,
  reasoningResult, reasoningStatus, type ReasoningResult, type ReasoningStatus } from "../../lib/checked-reasoning.ts";
import { projectOperationListing } from "../../lib/operation-catalogue.ts";
import { UUID } from "../../lib/operation-workbench.ts";
import { parseMcpJSON } from "../../lib/task-discovery.ts";
import styles from "../skills/skills.module.css";

type Workspace = { id: string; name: string };
type Action = "start" | "advance" | "guide" | "interrupt";
type Plan = { name: string; text: string; data: Record<string, unknown> };
const endpoint = "/api/ui/checked-reasoning";
const sameOrigin = { cache: "no-store", credentials: "same-origin" } as const;
const workspaceHeader = (id: string) => ({ "X-Potbot-Workspace": id });

function CheckedReasoningWorkbench({ workspace, initialRun }: { workspace: string; initialRun: string }) {
  const [runId, setRunId] = useState(initialRun);
  const [principal, setPrincipal] = useState("");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [status, setStatus] = useState<ReasoningStatus | null>(null);
  const [result, setResult] = useState<ReasoningResult | null>(null);
  const [guidanceDraft, setGuidanceDraft] = useState("");
  const [notice, setNotice] = useState("Reading workspace authority…");
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const pending = useRef(false);
  const planRead = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/ui/operations?limit=1", {
      ...sameOrigin, headers: workspaceHeader(workspace), signal: controller.signal,
    }).then(async response => {
      const value = response.ok && projectOperationListing(await response.json(), workspace);
      if (!value) throw new Error("Workspace identity unavailable");
      if (!controller.signal.aborted) {
        setPrincipal(value.principalId);
        setNotice("Enter a run ID or generate one for a new plan.");
      }
    }).catch(() => {
      if (!controller.signal.aborted) setNotice("Cannot verify the current workspace principal.");
    });
    return () => controller.abort();
  }, [workspace]);

  function selectRun(value: string) {
    setRunId(value);
    setGuidanceDraft("");
    setStatus(null);
    setResult(null);
    setUncertain(false);
    const url = new URL(window.location.href);
    url.searchParams.set("workspace", workspace);
    if (UUID.test(value)) url.searchParams.set("run", value);
    else url.searchParams.delete("run");
    window.history.replaceState(null, "", url);
  }

  async function selectPlan(file?: File) {
    const sequence = ++planRead.current;
    setPlan(null);
    if (!file) return;
    try {
      if (file.size < 2 || file.size > MAX_REASONING_PLAN) throw new Error("Invalid manifest size");
      const text = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      const data = reasoningPlan(parseMcpJSON(text));
      if (!data) throw new Error("Invalid plan shape");
      if (sequence === planRead.current) {
        setPlan({ name: file.name, text, data });
        setNotice("Review the selected plan before creating the run.");
      }
    } catch {
      if (sequence === planRead.current) setNotice("Choose an unambiguous UTF-8 JSON plan of up to 32 KiB with three ordered stages.");
    }
  }

  const canGuide = status?.state === "active" && (status.stage_index === 1 || status.stage_index === 2) &&
    status.decision_id === null && status.check_job_id === null && status.job_id === null && !status.operator_constraints;
  const unsavedGuidance = canGuide && guidanceDraft.length > 0;
  const constraints = guidanceConstraints(guidanceDraft.split("\n"));

  async function request(action: Action | "status" | "result") {
    if (pending.current || !UUID.test(runId) || !principal) return;
    const mutation = action !== "status" && action !== "result";
    const body = action === "start" && plan && !status ? { confirm: true, plan: plan.data } :
      action === "advance" && status?.state === "active" && !unsavedGuidance ? { confirm: true, expected_stage: status.stage_index } :
      action === "guide" && canGuide && constraints ? { confirm: true, expected_stage: status.stage_index, constraints } :
      action === "interrupt" && status?.state === "active" ? { confirm: true } : null;
    if (mutation) {
      if (!body || uncertain) return;
      const prompt = action === "start" ? "Save this exact plan under the selected run ID?" :
        action === "advance" ? "Advance the checked stage? An eligible job may use the configured model and budget." :
        action === "guide" ? "Save this guidance for the current stage? Advancing the stage may disclose these constraints to the configured model." :
          "Interrupt this run? An in-flight job may still require reconciliation.";
      if (!window.confirm(prompt)) return;
    }
    pending.current = true;
    setBusy(true);
    try {
      const response = await fetch(endpoint + "/" + runId + (action === "status" ? "" : "/" + action), {
        ...sameOrigin,
        headers: {
          ...workspaceHeader(workspace), "X-Potbot-Expected-Principal": principal,
          ...(mutation ? { "Content-Type": "application/json" } : {}),
        },
        ...(mutation ? { method: "POST", body: JSON.stringify(body) } : {}),
      });
      if (!response.ok) throw new Error("Request unavailable");
      const raw: unknown = await response.json();
      if (action === "result") {
        const original = reasoningResult(raw), final = status?.completed.at(-1);
        if (!original || !final || original.decision_id !== final.decision_id || original.output_sha256 !== final.output_sha256)
          throw new Error("Result scope changed");
        setResult(original);
      } else {
        const observed = reasoningStatus(raw, runId);
        if (!observed) throw new Error("Status scope changed");
        if (observed.stage_index !== status?.stage_index || observed.operator_constraints) setGuidanceDraft("");
        setStatus(observed);
        setUncertain(false);
      }
      setNotice(action === "start" ? "Plan saved. Review the stage before advancing." :
        action === "guide" ? "Guidance saved. Review the stage before advancing." : "Retained evidence refreshed.");
    } catch {
      if (mutation) {
        setUncertain(true);
        setNotice("The command was not confirmed. Inspect retained status before issuing another command.");
      } else {
        if (action === "status") { setStatus(null); setGuidanceDraft(""); }
        setResult(null);
        setNotice("Cannot read the selected run in this workspace.");
      }
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }

  const ready = Boolean(principal && UUID.test(runId) && !busy);
  const mayMutate = ready && !uncertain;
  return <div className={styles.layout}>
    <section className={styles.panel} aria-labelledby="reasoning-plan">
      <div className={styles.panelHeading}><h2 id="reasoning-plan">Run and plan</h2></div>
      <div className={styles.body}>
        <label className={styles.field}>Run ID
          <input autoComplete="off" spellCheck={false} maxLength={36} value={runId}
            disabled={busy} onChange={event => selectRun(event.target.value)} />
        </label>
        <div className={styles.actions}>
          <button type="button" disabled={busy || !principal} onClick={() => selectRun(crypto.randomUUID())}>Generate run ID</button>
          <button type="button" disabled={!ready} onClick={() => void request("status")}>Inspect run</button>
        </div>
        <fieldset disabled={busy || !!status || uncertain || !principal}>
          <label className={styles.field}>Plan JSON
            <input type="file" accept=".json,application/json" onChange={event => void selectPlan(event.target.files?.[0])} />
          </label>
          {plan && <details><summary>Review {plan.name}</summary><pre>{plan.text}</pre></details>}
          <div className={styles.actions}>
            <button type="button" className={styles.primary} disabled={!mayMutate || !plan || !!status}
              onClick={() => void request("start")}>Save plan</button>
          </div>
        </fieldset>
        <p role={uncertain ? "alert" : "status"} className={styles.status}>{notice}</p>
      </div>
    </section>
    <section className={styles.panel} aria-labelledby="reasoning-status">
      <div className={styles.panelHeading}><h2 id="reasoning-status">Stage status</h2></div>
      <div className={styles.body}>
        {status ? <>
          <p><strong>{status.state}</strong>{status.error_code && <> · {status.error_code.replaceAll("_", " ")}</>}</p>
          <ol>{REASONING_ROLE_LABELS.map((label, index) => <li key={label}
            aria-current={index === status.stage_index ? "step" : undefined}>
            {label} · {status.completed[index] ? "Completed" : index === status.stage_index ? status.state : "Pending"}
            {status.completed[index] && <details><summary>Stage receipt</summary>
              <dl className={styles.facts}>
                <dt>Decision</dt><dd><code>{status.completed[index].decision_id}</code></dd>
                <dt>Output SHA-256</dt><dd><code>{status.completed[index].output_sha256}</code></dd>
              </dl>
            </details>}
          </li>)}</ol>
          {(status.decision_id || status.check_job_id || status.job_id) && <dl className={styles.facts}>
            {status.decision_id && <><dt>Decision</dt><dd><code>{status.decision_id}</code></dd></>}
            {status.check_job_id && <><dt>Check job</dt><dd><code>{status.check_job_id}</code></dd></>}
            {status.job_id && <><dt>Model job</dt><dd><code>{status.job_id}</code></dd></>}
          </dl>}
          {status.operator_constraints && <section aria-label="Saved stage guidance">
            <strong>Saved stage guidance</strong>
            <ul>{status.operator_constraints.map(value => <li key={value}>{value}</li>)}</ul>
          </section>}
          {canGuide && <fieldset disabled={!mayMutate}>
            <label className={styles.field}>Operator guidance
              <textarea rows={3} maxLength={1027} value={guidanceDraft}
                onChange={event => setGuidanceDraft(event.target.value)}
                aria-describedby="reasoning-guidance-help" />
            </label>
            <p id="reasoning-guidance-help">Enter one to four distinct constraints, one per line,
              up to 256 UTF-8 bytes each. The checked stage may disclose them to the configured model.
              {unsavedGuidance && " Save or clear the draft before advancing."}</p>
            <button type="button" disabled={!constraints} onClick={() => void request("guide")}>Save stage guidance</button>
          </fieldset>}
          <div className={styles.actions}>
            {status.state === "active" && <>
              <button type="button" className={styles.primary} disabled={!mayMutate || unsavedGuidance}
                onClick={() => void request("advance")}>Advance checked stage</button>
              <button type="button" className={styles.danger} disabled={!mayMutate}
                onClick={() => void request("interrupt")}>Interrupt run</button>
            </>}
            {status.state === "completed" && <button type="button" disabled={!ready}
              onClick={() => void request("result")}>Read final result</button>}
          </div>
          {result && <section aria-label="Original final result">
            <p>{result.semantic_correctness === "verified_against_frozen_records"
              ? "Verified against frozen records" : "Semantic correctness has not been evaluated"}</p>
            <pre>{result.text}</pre>
          </section>}
        </> : <p>Inspect an existing run or save a new plan to see its stages.</p>}
      </div>
    </section>
  </div>;
}

export default function CheckedReasoningPage() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selection, setSelection] = useState({ workspace: "", run: "" });
  const [notice, setNotice] = useState("Reading authorized workspaces…");
  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/ui/bootstrap", { ...sameOrigin, signal: controller.signal }).then(async response => {
      const raw = response.ok ? object(await response.json()) : null;
      if (!raw || !Array.isArray(raw.workspaces) || raw.workspaces.length > 12) throw new Error("Workspace list unavailable");
      const workspaces = raw.workspaces.map(value => {
        const row = object(value);
        if (!row || typeof row.id !== "string" || !UUID.test(row.id) ||
            typeof row.name !== "string" || !row.name || row.name.length > 80) throw new Error("Invalid workspace");
        return { id: row.id, name: row.name };
      });
      if (controller.signal.aborted) return;
      const query = new URLSearchParams(window.location.search);
      const requested = query.get("workspace"), run = query.get("run") ?? "";
      const workspace = requested === null ? workspaces[0] :
        query.getAll("workspace").length === 1 ? workspaces.find(item => item.id === requested) : undefined;
      setWorkspaces(workspaces);
      setSelection({ workspace: workspace?.id ?? "",
        run: requested && workspace && query.getAll("run").length === 1 && UUID.test(run) ? run : "" });
      setNotice(workspaces.length ? "" : "No authorized workspace is configured.");
    }).catch(() => {
      if (!controller.signal.aborted) setNotice("Cannot read authorized workspaces.");
    });
    return () => controller.abort();
  }, []);

  return <main className={styles.shell}>
    <header className={styles.header}>
      <a className={styles.back} href="/">← Workspace</a>
      <h1>Checked reasoning</h1>
      <p>Save a reviewed investigation plan, advance one checked stage at a time and inspect the retained result.</p>
    </header>
    <div className={styles.toolbar}><label>Workspace
      <select value={selection.workspace} onChange={event => {
        const workspace = event.target.value;
        const url = new URL(window.location.href);
        url.searchParams.set("workspace", workspace);
        url.searchParams.delete("run");
        window.history.replaceState(null, "", url);
        setSelection({ workspace, run: "" });
      }}>
        <option value="" disabled>Select workspace</option>
        {workspaces.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>
    </label></div>
    {notice && <p className={styles.status} role="status">{notice}</p>}
    {selection.workspace && <CheckedReasoningWorkbench key={selection.workspace}
      workspace={selection.workspace} initialRun={selection.run} />}
  </main>;
}
