"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  EMPTY_FORM, PROVIDERS, UUID, buildJobSpec, checkUsable, mutationAttempt, projectContract,
  projectEvents, projectJobEnvelope, projectResult, terminal,
  type JobSpec, type MutationAttempt, type OperationContract, type ResultEnvelope,
  type WorkbenchEvent, type WorkbenchForm, type WorkbenchJob,
} from "../../lib/operation-workbench.ts";
import { readableCode } from "../../lib/operation-catalogue.ts";
import { clearSubmission, formFromRequest, readPendingSubmission, retainSubmission, submissionKey } from "../../lib/operation-workbench-session.ts";
import { MAX_ARTIFACT_DOWNLOAD_BYTES, readVerifiedArtifact } from "../../lib/operation-workbench-download.ts";
import styles from "./workbench.module.css";

type Props = { workspaceId: string; principalId: string; operationId: string };
const MAX_POLLS = 180, POLL_MS = 1500;
const headers = (workspace: string): HeadersInit => ({ "X-Potbot-Workspace": workspace });
const errorMessage = (status: number) => status === 409 ? "The check or task changed. Review the request and run a fresh check." :
  status === 404 ? "This job is unavailable for the selected operation and principal." :
  status === 400 ? "The service rejected this request. Review the task, target and typed input." : "The operation service is unavailable.";

export default function OperationWorkbench({ workspaceId, principalId, operationId }: Props) {
  const [contract, setContract] = useState<OperationContract | null>(null);
  const [form, setForm] = useState<WorkbenchForm>({ ...EMPTY_FORM });
  const [job, setJob] = useState<WorkbenchJob | null>(null), [checkJob, setCheckJob] = useState<WorkbenchJob | null>(null);
  const [checkedRequest, setCheckedRequest] = useState<JobSpec | null>(null);
  const [events, setEvents] = useState<WorkbenchEvent[]>([]), [result, setResult] = useState<ResultEnvelope | null>(null);
  const [busy, setBusy] = useState(false), [notice, setNotice] = useState("Reading the execution contract…");
  const [pending, setPending] = useState<MutationAttempt | null>(null), [uncertain, setUncertain] = useState(false);
  const [polling, setPolling] = useState(false), [resumeId, setResumeId] = useState("");
  const [downloading, setDownloading] = useState(false);
  const [monitorVersion, setMonitorVersion] = useState(0), [now, setNow] = useState(Date.now());
  const [recent, setRecent] = useState<{ id: string; type: string }[]>([]);
  const generation = useRef(0), mounted = useRef(true), requests = useRef(new Set<AbortController>());
  const mutation = useRef<MutationAttempt | null>(null), jobId = useRef<string | null>(null);
  const submitting = useRef(false);
  const latestJob = useRef<WorkbenchJob | null>(null);
  const eventCursor = useRef({ jobId: "", cursor: 0 });
  const downloadRequest = useRef<AbortController | null>(null);
  const scope = `${workspaceId}:${principalId}:${operationId}`;
  const scopeRef = useRef(scope); scopeRef.current = scope;
  const base = `/api/ui/operations/${operationId}`;
  const recoveryKey = submissionKey(workspaceId, principalId, operationId);
  const storage = () => { try { return window.sessionStorage; } catch { return undefined; } };

  const controller = useCallback(() => { const value = new AbortController(); requests.current.add(value); return value; }, []);
  const invalidate = useCallback(() => {
    generation.current++; for (const request of requests.current) request.abort(); requests.current.clear();
    downloadRequest.current = null;
    if (mounted.current) { setDownloading(false); setPolling(false); }
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; invalidate(); };
  }, [invalidate]);

  useEffect(() => {
    const retained = readPendingSubmission(recoveryKey, operationId, storage());
    if (!retained) return;
    mutation.current = retained.attempt; setPending(retained.attempt); setUncertain(true);
    if (retained.request) { setForm(formFromRequest(retained.request)); setCheckedRequest(retained.request); }
    if (retained.jobId) { jobId.current = retained.jobId; setResumeId(retained.jobId); }
    setNotice("A previous submission needs confirmation. Its exact request and key are retained; use Retry this request to recover its recorded job.");
  }, [recoveryKey, operationId]);

  useEffect(() => {
    const request = controller(), expected = generation.current, selectedScope = scope;
    void (async () => {
      try {
        const response = await fetch(`${base}/contract`, { credentials: "same-origin", cache: "no-store", headers: headers(workspaceId), signal: request.signal });
        if (!response.ok) throw new Error(errorMessage(response.status));
        const value = projectContract(await response.json(), workspaceId, operationId, principalId);
        if (!value) throw new Error("The execution contract did not match the selected principal and operation.");
        if (request.signal.aborted || expected !== generation.current || scopeRef.current !== selectedScope) return;
        setContract(value);
        if (!mutation.current) setNotice(value.supported ? "Choose an existing task and provide the exact execution input." : "This entry has no execution adapter in this workbench. Use its existing maintenance or acquisition flow.");
      } catch (error) {
        if (!request.signal.aborted && expected === generation.current && scopeRef.current === selectedScope)
          setNotice(error instanceof Error ? error.message : "The execution contract is unavailable.");
      } finally { requests.current.delete(request); }
    })();
    return () => { request.abort(); requests.current.delete(request); };
  }, [base, workspaceId, principalId, operationId, scope, controller]);

  useEffect(() => {
    if (!checkJob?.result?.operation_check) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [checkJob]);

  const monitor = useCallback((value: WorkbenchJob) => {
    jobId.current = value.id; latestJob.current = value; setJob(value); setEvents([]); setResult(null); setResumeId(value.id);
    eventCursor.current = { jobId: value.id, cursor: 0 };
    setRecent(prior => [{ id: value.id, type: value.job_type }, ...prior.filter(item => item.id !== value.id)].slice(0, 8));
    setMonitorVersion(count => count + 1);
  }, []);

  useEffect(() => {
    const id = jobId.current; if (!id) return;
    const request = controller(), expected = generation.current, selectedScope = scope;
    let timer: number | undefined, count = 0, cursor = eventCursor.current.jobId === id ? eventCursor.current.cursor : 0;
    const current = () => mounted.current && !request.signal.aborted && expected === generation.current && scopeRef.current === selectedScope && jobId.current === id;
    setPolling(true);
    const read = async () => {
      try {
        const [jobResponse, eventResponse] = await Promise.all([
          fetch(`${base}/jobs/${id}`, { credentials: "same-origin", cache: "no-store", headers: headers(workspaceId), signal: request.signal }),
          fetch(`${base}/jobs/${id}/events?after=${cursor}&limit=100`, { credentials: "same-origin", cache: "no-store", headers: headers(workspaceId), signal: request.signal }),
        ]);
        if (!current()) return;
        if (!jobResponse.ok || !eventResponse.ok) throw new Error(errorMessage(!jobResponse.ok ? jobResponse.status : eventResponse.status));
        const selected = projectJobEnvelope(await jobResponse.json(), workspaceId, operationId, id, principalId);
        const stream = projectEvents(await eventResponse.json(), workspaceId, operationId, id, cursor, principalId);
        if (!current()) return;
        if (!selected || !stream) throw new Error("A job response did not match the current operation and principal.");
        const observed = latestJob.current?.id === id && latestJob.current.update_token > selected.job.update_token ? latestJob.current : selected.job;
        latestJob.current = observed; setJob(observed);
        if (observed.job_type === "operation.check") setCheckJob(observed);
        cursor = stream.next_cursor;
        eventCursor.current = { jobId: id, cursor };
        setEvents(prior => [...prior, ...stream.items].slice(-200));
        count++;
        if (terminal(observed.state) && !stream.has_more) {
          const response = await fetch(`${base}/jobs/${id}/result`, { credentials: "same-origin", cache: "no-store", headers: headers(workspaceId), signal: request.signal });
          if (!current()) return;
          if (!response.ok) throw new Error(errorMessage(response.status));
          const output = projectResult(await response.json(), workspaceId, operationId, id, principalId);
          if (!current()) return;
          if (!output) throw new Error("The retained result did not match the current job.");
          if (output.job.update_token < observed.update_token) throw new Error("The retained result is older than the observed job revision.");
          latestJob.current = output.job; setResult(output); setJob(output.job); setPolling(false);
          if (output.job.job_type === "operation.check") {
            setCheckJob(output.job); setNow(Date.now());
            setNotice(output.job.result?.operation_check?.eligible ? "The worker check passed. Review the request and explicitly launch before it expires." : "The check completed. Resolve the reported blockers before checking again.");
          } else setNotice(output.job.state === "indeterminate" ? "The execution outcome is unresolved. Inspect its retained evidence before any further run." : `Job ${readableCode(output.job.state)}. Its retained result is shown below.`);
          return;
        }
        if (count >= MAX_POLLS) { setPolling(false); setNotice("Automatic monitoring paused after its bounded window. Resume monitoring to read this same job."); return; }
        timer = window.setTimeout(() => void read(), stream.has_more ? 150 : POLL_MS);
      } catch (error) {
        if (current()) { setPolling(false); setNotice((error instanceof Error ? error.message : "Job monitoring is unavailable.") + " Resume monitoring to read the same job; work has not been resubmitted."); }
      }
    };
    void read();
    return () => { request.abort(); requests.current.delete(request); if (timer !== undefined) window.clearTimeout(timer); };
  }, [monitorVersion, base, workspaceId, principalId, operationId, scope, controller]);

  const change = (key: keyof WorkbenchForm, value: string) => {
    if (busy || pending || submitting.current || mutation.current || (job && !terminal(job.state))) return;
    invalidate(); jobId.current = null; setPolling(false); setDownloading(false); setCheckJob(null); setCheckedRequest(null); setJob(null); setResult(null); setEvents([]);
    setForm(previous => ({ ...previous, [key]: value }));
    setNotice("The request changed. A new worker check is required.");
  };

  async function submit(attempt: MutationAttempt) {
    if (submitting.current || (mutation.current && (mutation.current.key !== attempt.key || mutation.current.path !== attempt.path || mutation.current.body !== attempt.body))) return;
    submitting.current = true;
    invalidate();
    const request = controller(), expected = generation.current, selectedScope = scope;
    mutation.current = attempt; setPending(attempt); setUncertain(false); setBusy(true);
    retainSubmission(recoveryKey, attempt, storage());
    setNotice(attempt.path.endsWith("/checks") ? "Queueing the worker readiness check…" : attempt.path.endsWith("/cancel") ? "Requesting cancellation…" : "Queueing the explicitly approved run…");
    try {
      const response = await fetch(attempt.path, { method: "POST", credentials: "same-origin", cache: "no-store", signal: request.signal,
        headers: { ...headers(workspaceId), "Content-Type": "application/json", "Idempotency-Key": attempt.key }, body: attempt.body });
      if (request.signal.aborted || !mounted.current || expected !== generation.current || scopeRef.current !== selectedScope) return;
      if (!response.ok) {
        if ([400, 401, 403, 404, 409].includes(response.status)) {
          clearSubmission(recoveryKey, attempt.key, storage());
          mutation.current = null; setPending(null); setUncertain(false);
          if (response.status === 409) { setCheckJob(null); setCheckedRequest(null); }
        } else setUncertain(true);
        throw new Error(errorMessage(response.status));
      }
      const value = projectJobEnvelope(await response.json(), workspaceId, operationId, attempt.path.endsWith("/cancel") ? jobId.current ?? undefined : undefined, principalId);
      if (request.signal.aborted || !mounted.current || expected !== generation.current || scopeRef.current !== selectedScope) return;
      if (!value) throw new Error("The submission response could not be verified.");
      clearSubmission(recoveryKey, attempt.key, storage());
      mutation.current = null; setPending(null); setUncertain(false);
      if (value.job.job_type === "operation.check") setCheckJob(value.job);
      if (attempt.path.endsWith("/runs")) { setCheckJob(null); setCheckedRequest(null); }
      monitor(value.job); setNotice(value.job.cancellation_requested ? "Cancellation requested. Waiting for the worker's confirmed terminal outcome." : "Job queued. Monitoring the existing worker queue.");
    } catch (error) {
      if (request.signal.aborted || !mounted.current || expected !== generation.current || scopeRef.current !== selectedScope) return;
      if (mutation.current) setUncertain(true);
      setNotice((error instanceof Error ? error.message : "The submission response is unavailable.") + (mutation.current ? " The request key and exact payload are retained. Use Retry this request to recover the same submission." : ""));
    } finally {
      requests.current.delete(request);
      submitting.current = false;
      if (mounted.current && expected === generation.current && scopeRef.current === selectedScope) setBusy(false);
    }
  }

  function check() {
    if (!contract || busy || submitting.current || mutation.current || pending) return;
    try {
      const spec = buildJobSpec(contract, form);
      invalidate(); setCheckJob(null); setResult(null); setEvents([]); jobId.current = null; setCheckedRequest(spec);
      void submit(mutationAttempt(`${base}/checks`, spec));
    } catch (error) { setNotice(error instanceof Error ? error.message : "Review the request fields."); }
  }
  function launch() {
    if (!contract || !checkedRequest || !checkJob || !checkUsable(checkJob, operationId) || pending || busy || submitting.current || mutation.current) return;
    let current: JobSpec; try { current = buildJobSpec(contract, form); } catch { setCheckJob(null); return; }
    if (JSON.stringify(current) !== JSON.stringify(checkedRequest)) { setCheckJob(null); setNotice("The input changed. Run a new check."); return; }
    if (!window.confirm(`Run ${contract.name} for task ${checkedRequest.episode_id}?\n\nThis queues the checked operation and may use the task's approved resources or contact the selected model provider.`)) return;
    void submit(mutationAttempt(`${base}/runs`, { checkJobId: checkJob.id, request: checkedRequest, confirm: true }));
  }
  function cancel() {
    if (!job || terminal(job.state) || busy || pending || submitting.current || mutation.current || !window.confirm("Request cancellation of this job? The worker must confirm that execution has stopped.")) return;
    void submit(mutationAttempt(`${base}/jobs/${job.id}/cancel`, { confirm: true }));
  }
  async function resume(id: string) {
    if (!UUID.test(id) || busy || pending || submitting.current || mutation.current) { setNotice("Enter a canonical UUID for a job belonging to this operation."); return; }
    invalidate(); setCheckJob(null); setCheckedRequest(null); setEvents([]); setResult(null); setJob(null); setBusy(true);
    const request = controller(), expected = generation.current, selectedScope = scope;
    try {
      const response = await fetch(`${base}/jobs/${id}`, { credentials: "same-origin", cache: "no-store", headers: headers(workspaceId), signal: request.signal });
      if (!response.ok) throw new Error(errorMessage(response.status));
      const value = projectJobEnvelope(await response.json(), workspaceId, operationId, id, principalId);
      if (!value) throw new Error("The selected job does not match this operation and principal.");
      if (!request.signal.aborted && expected === generation.current && scopeRef.current === selectedScope) {
        monitor(value.job); setNotice("Resumed monitoring the existing job. No execution was submitted.");
      }
    } catch (error) { if (!request.signal.aborted && expected === generation.current) setNotice(error instanceof Error ? error.message : "The job is unavailable."); }
    finally { requests.current.delete(request); if (expected === generation.current && scopeRef.current === selectedScope) setBusy(false); }
  }

  async function download() {
    const artifact = result?.artifact, id = job?.id;
    if (!artifact || !id || downloading || downloadRequest.current || artifact.bytes > MAX_ARTIFACT_DOWNLOAD_BYTES) return;
    const request = controller(), expected = generation.current, selectedScope = scope;
    downloadRequest.current = request;
    const current = () => mounted.current && !request.signal.aborted && expected === generation.current && scopeRef.current === selectedScope && jobId.current === id;
    setDownloading(true); setNotice("Reading and verifying the owned result artifact…");
    try {
      const bytes = await readVerifiedArtifact({ workspaceId, principalId, operationId, jobId: id, artifact, signal: request.signal, current,
        progress: (received, total) => setNotice(`Reading result artifact: ${received.toLocaleString()} / ${total.toLocaleString()} bytes.`) });
      if (!current()) return;
      const url = URL.createObjectURL(new Blob([bytes.buffer], { type: artifact.mediaType }));
      const link = document.createElement("a"); link.href = url; link.download = `potbot-result-${id}.${artifact.mediaType.startsWith("application/json") ? "json" : "bin"}`;
      link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setNotice("The complete result artifact was verified and downloaded.");
    } catch (error) { if (current()) setNotice(error instanceof Error ? error.message : "The result artifact is unavailable."); }
    finally { requests.current.delete(request); if (downloadRequest.current === request) downloadRequest.current = null; if (current()) setDownloading(false); }
  }

  const active = Boolean(job && !terminal(job.state)), locked = busy || Boolean(pending) || active;
  const eligible = Boolean(checkedRequest && checkUsable(checkJob, operationId, now) && !locked);
  const report = checkJob?.result?.operation_check;
  const field = (name: keyof WorkbenchForm) => ({ value: form[name], onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => change(name, event.target.value) });

  return <section className={styles.workbench} aria-labelledby="workbench-title">
    <div className={styles.heading}><span>CHECK → RUN → INSPECT</span><h3 id="workbench-title">Operation workbench</h3>
      <p>Choose a task, check the exact request on the configured worker, then explicitly launch it.</p></div>
    <p role="status" aria-live="polite" className={styles.notice}>{notice}</p>
    {contract?.supported && <>
      <form onSubmit={event => { event.preventDefault(); check(); }}>
        <fieldset disabled={locked} className={styles.fields}>
          <legend>Execution request</legend>
          <label>Existing task UUID<input {...field("taskId")} placeholder="Task with an active contract" autoComplete="off" maxLength={36} required /></label>
          {contract.jobType === "cog.execute" ? <>
            <p className={styles.model}>Invocation target <code>{contract.executionReference}</code></p>
            <label>Enrolled agent UUID<input {...field("agentId")} maxLength={36} autoComplete="off" required /></label>
            <label>Repository state UUID<input {...field("repositoryStateId")} maxLength={36} autoComplete="off" required /></label>
            <label>Installed profile SHA-256<input {...field("profileSha256")} maxLength={64} autoComplete="off" required /></label>
            <label>Input type reference<input {...field("typeRef")} placeholder="syn://type/…" maxLength={270} autoComplete="off" required /></label>
            <label>Typed value (JSON)<textarea {...field("value")} rows={6} spellCheck={false} maxLength={16_384} /></label>
            <p className={styles.help}>Use the enrolled agent, repository state, installed profile and typed input declared by the operation. The worker verifies grants, source, dependencies and current admission.</p>
          </> : <>
            <div className={styles.pair}><label>Provider<select {...field("provider")}>{PROVIDERS.map(provider => <option key={provider}>{provider}</option>)}</select></label>
              <label>Task role<select {...field("role")}><option value="reasoning">Reasoning</option><option value="coding">Coding</option></select></label></div>
            <p className={styles.model}>Selected model <code>{contract.reference.replace(/^model:/, "")}</code></p>
            <label>Prompt<textarea {...field("prompt")} rows={6} maxLength={16_000} required /></label>
            <div className={styles.pair}><label>Maximum output tokens<input type="number" min={1} max={131072} {...field("maxOutputTokens")} required /></label>
              <label>Timeout (seconds)<input type="number" min={1} max={3600} {...field("timeoutSeconds")} required /></label></div>
            <p className={styles.help}>The check verifies provider policy, credential availability and task budget without sending this prompt for generation. Launch uses the selected provider and the task's approved limits.</p>
          </>}
        </fieldset>
        <div className={styles.actions}><button type="submit" disabled={!contract || locked}>Check on worker</button>
          <button type="button" className={styles.launch} disabled={!eligible} onClick={launch}>Launch checked request</button></div>
      </form>
      {checkedRequest && <details className={styles.details}><summary>Review exact checked request</summary><pre>{JSON.stringify(checkedRequest, null, 2)}</pre></details>}
      {report && <section className={styles.checkReport} aria-label="Worker eligibility result">
        <h4>{report.eligible ? "Worker check passed" : "Execution blocked"}</h4>
        <p>{Date.parse(report.expiresAt) > now ? `Valid until ${new Date(report.expiresAt).toLocaleTimeString()}` : "This check has expired. Run a fresh check."}</p>
        <ul>{report.checks.map(check => <li key={check.code}><span className={styles[check.status]}>{readableCode(check.status)}</span>{readableCode(check.code)}</li>)}</ul>
        <p className={styles.help}>The check is specific to this request and task. Runtime admission is checked again before dispatch.</p>
        <details className={styles.details}><summary>Check identities and bindings</summary><pre>{JSON.stringify(report.bindings, null, 2)}</pre><code>{report.bindingSha256}</code></details>
      </section>}
    </>}
    {contract && !contract.supported && <div className={styles.unsupported}><h4>Execution is unavailable for this entry</h4>
      <ul>{contract.reasonCodes.map(reason => <li key={reason}>{readableCode(reason)}</li>)}</ul>
      <a href={`/setup/tools?workspace=${encodeURIComponent(workspaceId)}`}>Open existing tool maintenance ↗</a></div>}
    {pending && uncertain && <aside className={styles.uncertain} role="alert"><strong>Submission outcome is unknown</strong>
      <p>Retrying uses the same request key and exact payload to recover its recorded job. Keep this recovery key until the job is confirmed.</p><code>{pending.key}</code>
      <button type="button" disabled={busy} onClick={() => void submit(pending)}>Retry this request</button></aside>}
    {job && <section className={styles.job} aria-label="Owned operation job">
      <div className={styles.jobHeading}><h4>{job.job_type === "operation.check" ? "Readiness check" : "Operation run"}</h4><span>{readableCode(job.state)}</span></div>
      <code>{job.id}</code><p className={styles.help}>Task <code>{job.episode_id}</code></p>
      {job.progress.stage && <p>{readableCode(job.progress.stage)}{job.progress.completed !== undefined && ` · ${job.progress.completed} / ${job.progress.total}`}</p>}
      {job.error_code && <p role="alert">{readableCode(job.error_code)}</p>}
      {job.state === "indeterminate" && <p role="alert">The worker could not establish the final effect. Use the existing reconciliation path before starting further work.</p>}
      <div className={styles.actions}><button type="button" disabled={busy || Boolean(pending) || polling} onClick={() => setMonitorVersion(value => value + 1)}>{polling ? "Monitoring…" : "Resume monitoring"}</button>
        <button type="button" disabled={!active || busy || Boolean(pending) || job.cancellation_requested} onClick={cancel}>{job.cancellation_requested ? "Cancellation requested" : "Cancel job"}</button></div>
      {events.length > 0 && <details className={styles.details}><summary>Recent job events ({events.length})</summary>
        <ol className={styles.events}>{events.map(event => <li key={event.id}><time>{new Date(event.recorded_at).toLocaleTimeString()}</time><span>{readableCode(event.event_type)}{event.data.stage ? ` · ${readableCode(event.data.stage)}` : ""}</span>{event.data.text && <pre>{event.data.text}</pre>}</li>)}</ol></details>}
      {result && <section className={styles.result}><h4>Retained result</h4>
        {result.output ? <pre tabIndex={0}>{result.output}</pre> : <p className={styles.help}>No retained text output.</p>}
        {result.truncated && <p className={styles.help}>This bounded preview is truncated. Use the retained artifact identity for the complete result.</p>}
        {result.artifactText !== null && <><h4>Native result preview</h4><pre tabIndex={0}>{result.artifactText}</pre>{result.artifactTruncated && <p className={styles.help}>The native result preview is truncated. Download the verified artifact for the complete result.</p>}</>}
        {result.artifact && <><dl><dt>Artifact</dt><dd><code>{result.artifact.id}</code></dd><dt>SHA-256</dt><dd><code>{result.artifact.sha256}</code></dd><dt>Size / type</dt><dd>{result.artifact.bytes.toLocaleString()} bytes · {result.artifact.mediaType}</dd></dl>
          <button type="button" disabled={downloading || result.artifact.bytes > MAX_ARTIFACT_DOWNLOAD_BYTES} onClick={() => void download()}>{downloading ? "Verifying download…" : "Download verified result"}</button>
          {result.artifact.bytes > MAX_ARTIFACT_DOWNLOAD_BYTES && <p className={styles.help}>This artifact exceeds the browser's 16 MiB download bound. Retrieve it through the owned artifact CLI.</p>}</>}
        {job.result && Object.keys(job.result).some(key => key !== "operation_check") && <details className={styles.details}><summary>Invocation, usage and result references</summary><pre>{JSON.stringify(Object.fromEntries(Object.entries(job.result).filter(([key]) => key !== "operation_check")), null, 2)}</pre></details>}
      </section>}
    </section>}
    <details className={styles.details}><summary>Resume an existing job</summary><p className={styles.help}>Only this principal's jobs for the selected operation are available. Reading a job does not submit it again.</p>
      <div className={styles.resume}><input aria-label="Existing job UUID" value={resumeId} onChange={event => setResumeId(event.target.value)} maxLength={36} placeholder="Job UUID" autoComplete="off" />
        <button type="button" disabled={busy || Boolean(pending) || !UUID.test(resumeId)} onClick={() => void resume(resumeId)}>Inspect job</button></div>
      {recent.length > 0 && <ul className={styles.recent}>{recent.map(item => <li key={item.id}><button type="button" disabled={busy || Boolean(pending)} onClick={() => void resume(item.id)}>{readableCode(item.type)} <code>{item.id}</code></button></li>)}</ul>}
    </details>
  </section>;
}
