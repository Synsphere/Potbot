"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { TASK_ORACLE_SCHEMA, UUID, parseMcpJSON, sameTaskValue, projectTaskOracle, projectTaskExecutionContext, projectTaskExecutionJobs, projectTaskExecutionJob, projectTaskExecutionEvents, projectTaskExecutionResult, projectTaskExecutionMutation, type TaskExecutionContext, type TaskExecutionJobs, type TaskExecutionJob, type TaskExecutionResult } from "../../lib/task-execution.ts";
import { taskExecutionBase, taskExecutionScopeMatches, taskExecutionScopeInvalidated, taskExecutionBindingMatchesContext, taskExecutionCanLaunch, taskExecutionNeedsMonitoring, taskExecutionJournalScope, taskExecutionAttemptRoute, newTaskExecutionAttempt, validateTaskExecutionAttempt, readTaskExecutionArtifact, TASK_EXECUTION_MAX_POLLS } from "../../lib/task-execution-client.ts";
import { EMPTY_FORM, PROVIDERS, buildJobSpec, type WorkbenchForm, type WorkbenchEvent } from "../../lib/operation-workbench.ts";
import { formFromRequest } from "../../lib/operation-workbench-session.ts";
import { WorkshopRequests, retainAttempt, recoverAttempt, clearAttempt, clearRejectedAttempt, type WorkshopAttempt, type RequestTicket } from "../../lib/skill-workshop-client.ts";
import { SelectedContextView, SelectedCheckView, SelectedOutcomeView, SelectedJSON } from "./SelectedTaskViews.tsx";
import styles from "./workbench.module.css";

type Props = { workspaceId: string; principalId: string; operationId: string; selectionId: string; initialJobId?: string | null; onScopeInvalidated: () => void };
type Review = { title: string; path: string; body: Record<string, unknown> };
const EMPTY_ORACLE = JSON.stringify({ schema: TASK_ORACLE_SCHEMA, assertions: [] }, null, 2);

export default function SelectedTaskWorkbench({ workspaceId, principalId, operationId, selectionId, initialJobId = null, onScopeInvalidated }: Props) {
  const base = taskExecutionBase(selectionId), [context, setContext] = useState<TaskExecutionContext | null>(null), [contextBusy, setContextBusy] = useState(false), [contextNotice, setContextNotice] = useState("");
  const [history, setHistory] = useState<TaskExecutionJobs | null>(null), [historyBusy, setHistoryBusy] = useState(false), [historyNotice, setHistoryNotice] = useState("");
  const [entry, setEntry] = useState<TaskExecutionJob | null>(null), [result, setResult] = useState<TaskExecutionResult | null>(null), [events, setEvents] = useState<WorkbenchEvent[]>([]), [jobBusy, setJobBusy] = useState(false), [jobNotice, setJobNotice] = useState("");
  const [form, setForm] = useState<WorkbenchForm>({ ...EMPTY_FORM }), [acceptance, setAcceptance] = useState(EMPTY_ORACLE), [resumeId, setResumeId] = useState(initialJobId ?? "");
  const [review, setReview] = useState<Review | null>(null), [pending, setPending] = useState<WorkshopAttempt | null>(null), [durable, setDurable] = useState<boolean | null>(null), [recovered, setRecovered] = useState(false), [busy, setBusy] = useState(false), [notice, setNotice] = useState("");
  const [live, setLive] = useState(true), [visibility, setVisibility] = useState(0), [pollNotice, setPollNotice] = useState(""), [now, setNow] = useState(Date.now()), [downloading, setDownloading] = useState(false);
  const contexts = useRef(new WorkshopRequests()), lists = useRef(new WorkshopRequests()), details = useRef(new WorkshopRequests()), mutations = useRef(new WorkshopRequests()), downloads = useRef(new WorkshopRequests());
  const pendingRef = useRef<WorkshopAttempt | null>(null), busyRef = useRef(false), journal = useRef(""), historyRef = useRef<TaskExecutionJobs | null>(null), selectedRef = useRef<string | null>(initialJobId), entryRef = useRef<TaskExecutionJob | null>(null), pollCount = useRef(0), eventCursor = useRef({ id: initialJobId, after: 0 });
  const abortReads = useCallback(() => { contexts.current.reset(); lists.current.reset(); details.current.reset(); downloads.current.reset(); }, []);
  const leaveScope = useCallback(() => { abortReads(); mutations.current.reset(); onScopeInvalidated(); }, [abortReads, onScopeInvalidated]);
  const read = useCallback(async (path: string, ticket: RequestTicket): Promise<unknown> => {
    const response = await fetch(path, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: { "X-Potbot-Workspace": workspaceId } });
    if (!ticket.current()) return null;
    if (taskExecutionScopeInvalidated(response.status, null, workspaceId, principalId)) { leaveScope(); return null; }
    if (!response.ok) throw new Error(response.status === 409 ? "The selected task, source or checked evidence changed. Refresh current context; retained job history remains separate." : "The selected task record is unavailable.");
    const raw = await response.json(); if (!ticket.current()) return null;
    if (taskExecutionScopeInvalidated(response.status, raw, workspaceId, principalId)) { leaveScope(); return null; } return raw;
  }, [workspaceId, principalId, leaveScope]);

  const loadContext = useCallback(() => {
    contexts.current.reset(); const ticket = contexts.current.open(); setContextBusy(true); setContext(null); setContextNotice("Reading the selected task and current operation authority…");
    void (async () => { try {
      const raw = await read(`${base}/context`, ticket); if (!ticket.current()) return;
      const view = projectTaskExecutionContext(raw, workspaceId, selectionId, principalId, operationId); if (!view) throw new Error("The context does not match this exact selection, operation and principal.");
      setContext(view); setContextNotice(""); setForm(value => ({ ...value, taskId: view.taskBinding.episodeId, repositoryStateId: view.taskBinding.repositoryStateId }));
    } catch (error) { if (ticket.current()) setContextNotice(error instanceof Error ? error.message : "Selected task context could not be read."); }
    finally { if (ticket.current()) setContextBusy(false); ticket.finish(); } })();
  }, [base, read, workspaceId, selectionId, principalId, operationId]);

  const loadHistory = useCallback((append = false) => {
    const previous = historyRef.current, cursor = append ? previous?.nextCursor : null; if (append && !cursor) return;
    lists.current.reset(); const ticket = lists.current.open(); setHistoryBusy(true); setHistoryNotice("Reading this selection’s check and run history…");
    void (async () => { try {
      const raw = await read(`${base}/jobs?${new URLSearchParams({ limit: "25", ...(cursor ? { after: cursor } : {}) })}`, ticket); if (!ticket.current()) return;
      const page = projectTaskExecutionJobs(raw, workspaceId, selectionId, principalId, operationId);
      if (!page || cursor && page.nextCursor === cursor || previous && !taskExecutionScopeMatches(previous, page!)) throw new Error("The selected job history changed scope or did not advance safely.");
      if (append && page.items.length && page.items.every(item => previous?.items.some(old => old.job.id === item.job.id))) throw new Error("The retained job page repeated previously read records.");
      const items = append && previous ? [...new Map([...previous.items, ...page.items].map(item => [item.job.id, item])).values()] : page.items;
      const next = { ...page, items: items.slice(-200) }; historyRef.current = next; setHistory(next); setHistoryNotice(items.length > 200 ? "Showing the last 200 loaded jobs. Refresh to return to the first page." : page.hasMore ? "More retained jobs are available." : "All jobs in this bounded history are shown.");
    } catch (error) { if (ticket.current()) setHistoryNotice(error instanceof Error ? error.message : "Selected history could not be refreshed. The cursor has not advanced."); }
    finally { if (ticket.current()) setHistoryBusy(false); ticket.finish(); } })();
  }, [base, read, workspaceId, selectionId, principalId, operationId]);

  const loadJob = useCallback(async (id: string, fill = false) => {
    details.current.reset(); const ticket = details.current.open(); setJobBusy(true); setJobNotice("Reading the exact selected job and its evidence…"); setReview(null);
    try {
      const raw = await read(`${base}/jobs/${id}`, ticket); if (!ticket.current() || selectedRef.current !== id) return;
      const next = projectTaskExecutionJob(raw, workspaceId, selectionId, id, principalId, operationId); if (!next) throw new Error("The job does not match this exact selection, task and operation.");
      if (entryRef.current?.job.id === id && next.job.update_token < entryRef.current.job.update_token) throw new Error("A stale job response cannot replace newer retained evidence.");
      const previous = entryRef.current; entryRef.current = next; setEntry(next);
      if (fill) { setForm(formFromRequest(next.binding.request)); setAcceptance(JSON.stringify(next.binding.acceptance, null, 2)); }
      const after = eventCursor.current.id === id ? eventCursor.current.after : 0;
      const eventRaw = await read(`${base}/jobs/${id}/events?after=${after}&limit=100`, ticket); if (!ticket.current() || selectedRef.current !== id) return;
      const page = projectTaskExecutionEvents(eventRaw, workspaceId, selectionId, id, after, principalId, operationId); if (!page || !taskExecutionScopeMatches(next, page) || !sameTaskValue(next.binding, page.binding) || page.job.update_token < next.job.update_token) throw new Error("Job events do not match the selected owner, immutable binding or retained cursor.");
      eventCursor.current = { id, after: page.next_cursor }; setEvents(items => [...new Map([...items, ...page.items].map(item => [item.id, item])).values()].slice(-500));
      if (next.job.job_type !== "operation.check") {
        const resultRaw = await read(`${base}/jobs/${id}/result`, ticket); if (!ticket.current() || selectedRef.current !== id) return;
        const output = projectTaskExecutionResult(resultRaw, workspaceId, selectionId, id, principalId, operationId);
        if (!output || !taskExecutionScopeMatches(next, output) || !sameTaskValue(next.binding, output.binding) || output.result.job.update_token < next.job.update_token) throw new Error("The result or verification does not match the exact selected job binding.");
        setResult(output);
      } else setResult(null);
      setJobNotice(page.has_more ? "More retained events are available; refresh the selected job to continue the bounded event cursor." : "");
      if (!taskExecutionNeedsMonitoring(next) && (!previous || taskExecutionNeedsMonitoring(previous))) { loadContext(); loadHistory(); }
    } catch (error) { if (ticket.current() && selectedRef.current === id) { setEntry(null); entryRef.current = null; setResult(null); setJobNotice(error instanceof Error ? error.message : "The selected job could not be verified. Refresh before another decision."); } }
    finally { if (ticket.current()) setJobBusy(false); ticket.finish(); }
  }, [base, read, workspaceId, selectionId, principalId, operationId, loadContext, loadHistory]);

  const selectJob = useCallback((id: string, fill = true) => {
    if (!UUID.test(id)) return; downloads.current.reset(); setDownloading(false); selectedRef.current = id; entryRef.current = null; eventCursor.current = { id, after: 0 }; pollCount.current = 0;
    setEntry(null); setResult(null); setEvents([]); setResumeId(id); setLive(true); setPollNotice("");
    const url = new URL(window.location.href); url.searchParams.set("workspace", workspaceId); url.searchParams.set("operation", operationId); url.searchParams.set("selection", selectionId); url.searchParams.set("job", id); window.history.replaceState(null, "", url); void loadJob(id, fill);
  }, [workspaceId, operationId, selectionId, loadJob]);

  useEffect(() => {
    journal.current = taskExecutionJournalScope(workspaceId, principalId, selectionId); const ticket = mutations.current.open();
    void recoverAttempt(journal.current, value => validateTaskExecutionAttempt(value, selectionId)).then(value => { if (ticket.current()) { pendingRef.current = value; setPending(value); setRecovered(true); } ticket.finish(); });
    loadContext(); loadHistory(); if (initialJobId) selectJob(initialJobId);
    const changed = () => setVisibility(value => value + 1); document.addEventListener("visibilitychange", changed);
    return () => { document.removeEventListener("visibilitychange", changed); abortReads(); mutations.current.reset(); };
  }, [workspaceId, principalId, selectionId, initialJobId, loadContext, loadHistory, selectJob, abortReads]);
  useEffect(() => { if (!entry || !live || !taskExecutionNeedsMonitoring(entry) || document.visibilityState === "hidden") return; if (pollCount.current >= TASK_EXECUTION_MAX_POLLS) { setPollNotice("Monitoring paused after 30 refresh cycles. Refresh explicitly to continue."); return; } const id = entry.job.id, timer = window.setTimeout(() => { pollCount.current++; void loadJob(id); }, 5000); return () => window.clearTimeout(timer); }, [entry, live, visibility, loadJob]);
  useEffect(() => { if (!entry?.job.result?.operation_check) return; const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, [entry]);

  function send(attempt: WorkshopAttempt, retry = false) {
    if (!recovered || busyRef.current || !validateTaskExecutionAttempt(attempt, selectionId) || pendingRef.current && (!retry || !sameTaskValue(pendingRef.current, attempt))) return;
    busyRef.current = true; pendingRef.current = attempt; setPending(attempt); setBusy(true); setReview(null); setLive(false); setNotice("Retaining and sending this exact selected-operation command…"); const ticket = mutations.current.open(), journalScope = journal.current;
    void (async () => { try {
      const persisted = await retainAttempt(journalScope, attempt); if (!ticket.current()) return; setDurable(persisted);
      const response = await fetch(attempt.path, { method: "POST", credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: { "X-Potbot-Workspace": workspaceId, "Content-Type": "application/json", "Idempotency-Key": attempt.key }, body: attempt.body }); if (!ticket.current()) return;
      if (!response.ok) {
        const cleared = await clearRejectedAttempt(journalScope, attempt.key, response.status, retry); if (!ticket.current()) return; if (cleared) { pendingRef.current = null; setPending(null); }
        if (taskExecutionScopeInvalidated(response.status, null, workspaceId, principalId)) { leaveScope(); return; }
        throw new Error(cleared ? "The command was rejected. Refresh current task and job evidence before reviewing a new request." : "The outcome remains uncertain. Explicitly retry this exact command to recover the original job.");
      }
      const raw = await response.json(); if (!ticket.current()) return; if (taskExecutionScopeInvalidated(response.status, raw, workspaceId, principalId)) { leaveScope(); return; }
      const route = taskExecutionAttemptRoute(attempt.path)!, receipt = projectTaskExecutionMutation(raw, workspaceId, route, parseMcpJSON(attempt.body) as Record<string, unknown>, principalId, operationId);
      if (!receipt) throw new Error("The response did not verify this exact command and binding. Keep the retained request and explicitly retry it.");
      await clearAttempt(journalScope, attempt.key); if (!ticket.current()) return; pendingRef.current = null; setPending(null); setDurable(null); setNotice("The original job receipt was verified. Reading its current state and selected task context separately…"); loadContext(); loadHistory(); selectJob(receipt.job.id);
    } catch (error) { if (ticket.current()) setNotice(error instanceof Error ? error.message : "The command outcome is uncertain. Keep its exact bytes and key."); }
    finally { if (ticket.current()) { busyRef.current = false; setBusy(false); } ticket.finish(); } })();
  }

  function check() {
    if (!context?.current || !context.supported || !context.operation) return;
    try { const request = buildJobSpec(context.operation, form), oracle = projectTaskOracle(parseMcpJSON(acceptance)); if (!oracle) throw new Error("Declare 1–16 bounded literal assertions with valid RFC 6901 pointers before checking.");
      const body = { selectionSha256: context.selectionSha256, request, acceptance: oracle, confirm: true }; if (!validateTaskExecutionAttempt(newTaskExecutionAttempt(`${base}/checks`, body), selectionId)) throw new Error("The exact request exceeds its selected-operation contract.");
      setLive(false); setReview({ title: "Check this exact selected task request", path: `${base}/checks`, body }); setNotice("");
    } catch (error) { setNotice(error instanceof Error ? error.message : "Review the exact input and declared assertions."); }
  }
  function draftMatches(): boolean { try { return !!context?.operation && !!entry && sameTaskValue(buildJobSpec(context.operation, form), entry.binding.request) && sameTaskValue(projectTaskOracle(parseMcpJSON(acceptance)), entry.binding.acceptance); } catch { return false; } }
  const launched = !!entry && history?.items.some(item => item.job.operation_check_id === entry.job.id);
  const locked = busy || !!pending || !recovered, active = !!entry && taskExecutionNeedsMonitoring(entry);
  const canLaunch = !!context && !!entry && !contextBusy && !jobBusy && !locked && !launched && draftMatches() && taskExecutionCanLaunch(context, entry, now);
  function launch() { if (!canLaunch || !entry) return; setLive(false); setReview({ title: "Launch the exact checked input and assertions", path: `${base}/runs`, body: { checkJobId: entry.job.id, bindingSha256: entry.binding.sha256, confirm: true } }); }
  async function download() {
    if (!result?.result.artifact || !entry) return; const output = result, id = entry.job.id; downloads.current.reset(); const ticket = downloads.current.open(); setDownloading(true);
    try { const bytes = await readTaskExecutionArtifact({ scope: output, bindingSha256: output.binding.sha256, jobId: id, artifact: output.result.artifact!, signal: ticket.signal, current: () => ticket.current() && selectedRef.current === id }); if (!ticket.current()) return;
      const url = URL.createObjectURL(new Blob([bytes], { type: output.result.artifact!.mediaType })), link = document.createElement("a"); link.href = url; link.download = `potbot-selected-result-${id}`; link.click(); URL.revokeObjectURL(url); setNotice("The complete owned artifact passed SHA-256 verification before download.");
    } catch (error) { if (ticket.current()) setNotice(error instanceof Error ? error.message : "The selected artifact could not be verified."); }
    finally { if (ticket.current()) setDownloading(false); ticket.finish(); }
  }
  const field = (name: keyof WorkbenchForm) => ({ value: form[name], onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => { setForm(value => ({ ...value, [name]: event.target.value })); setReview(null); } });

  return <section className={styles.workbench} aria-label="Selected task execution"><header className={styles.heading}><span>SELECTED TASK · CHECK · LAUNCH · VERIFY</span><h3>Execute this selected operation</h3><p>Review exact input and fixed result assertions, check on the configured worker, then explicitly launch the checked binding.</p></header>
    {notice && <p className={styles.notice} role="status">{notice}</p>}{contextNotice && <p className={styles.notice} role="status">{contextNotice}</p>}
    <div className={styles.actions}><button disabled={contextBusy || busy} onClick={loadContext}>Refresh current selection context</button><a href={`/discover/tasks?${new URLSearchParams({ workspace: workspaceId, ...(context ? { episode: context.taskBinding.episodeId, state: context.taskBinding.repositoryStateId, search: context.selection.searchId } : {}) })}`}>Return to task discovery ↗</a></div>
    {context && <SelectedContextView context={context} />}
    {pending && <section className={styles.uncertain}><h4>Recover the original selected command</h4><p>Its check, launch or cancellation may already be retained. Reading this page never retries it automatically. Retry uses the same exact bytes and key, including after a changed task or expired check.</p>{durable === false && <p>Durable browser storage is unavailable. Keep this tab open until the original receipt is resolved.</p>}<SelectedJSON title="Exact unresolved request" value={pending} /><button disabled={busy || !recovered} onClick={() => send(pending, true)}>Retry exact command</button></section>}
    {context?.supported && context.operation && <form onSubmit={event => { event.preventDefault(); check(); }}><fieldset className={styles.fields} disabled={locked || active || contextBusy || !context.current}><legend>Exact request for the selected task</legend>
      {context.operation.jobType === "cog.execute" ? <><p className={styles.model}>Installed invocation target <code>{context.operation.executionReference}</code></p><label>Enrolled agent UUID<input {...field("agentId")} maxLength={36} required autoComplete="off" /></label><label>Installed profile SHA-256<input {...field("profileSha256")} maxLength={64} required autoComplete="off" /></label><label>Canonical input type<input {...field("typeRef")} maxLength={270} placeholder="syn://type/…" required autoComplete="off" /></label><label>Typed value (JSON)<textarea {...field("value")} rows={6} maxLength={16384} spellCheck={false} /></label></> : <><div className={styles.pair}><label>Provider<select {...field("provider")}>{PROVIDERS.map(provider => <option key={provider}>{provider}</option>)}</select></label><label>Model role<select {...field("role")}><option value="reasoning">reasoning</option><option value="coding">coding</option></select></label></div><p className={styles.model}>Selected model <code>{context.operation.reference}</code></p><label>Prompt<textarea {...field("prompt")} rows={6} maxLength={16384} required /></label><div className={styles.pair}><label>Maximum output tokens<input {...field("maxOutputTokens")} inputMode="numeric" maxLength={6} /></label><label>Timeout seconds<input {...field("timeoutSeconds")} inputMode="numeric" maxLength={4} /></label></div></>}
      <label>Declared result assertions (JSON)<textarea value={acceptance} onChange={event => { setAcceptance(event.target.value); setReview(null); }} rows={9} maxLength={65536} spellCheck={false} /></label>
      <p className={styles.help}>Declare 1–16 literal <code>equals</code>, <code>contains</code> or <code>length_equals</code> assertions. Use <code>/text</code> for complete hosted text or <code>/value/…</code> for typed Cog output. Expected values are fixed before the check. A preview or execution success flag cannot satisfy these assertions.</p>
      <button type="submit">Review exact worker check</button>
    </fieldset></form>}
    {context && !context.supported && <section className={styles.unsupported}><h4>No execution adapter for this selection</h4><p>Continue through its existing acquisition, MCP or operation review owner. This surface creates no alternate provider dispatch route.</p></section>}
    {review && <section className={styles.uncertain} aria-label="Review exact selected command"><h4>{review.title}</h4><p>{review.path.endsWith("/runs") ? "Launch uses only the original checked request and assertions. It may consume the task’s approved resources and contact the explicitly selected provider." : review.path.endsWith("/cancel") ? "Cancellation requests a stop; only the existing worker can confirm that execution ended." : "The worker checks the exact selected task and request. This command does not authorize a later launch."}</p><pre>{JSON.stringify(review.body, null, 2)}</pre>{review.path.endsWith("/runs") && entry && <SelectedJSON title="Original checked input and fixed assertions" value={entry.binding} />}<div className={styles.actions}><button className={styles.launch} disabled={locked || review.path.endsWith("/runs") && !canLaunch} onClick={() => send(newTaskExecutionAttempt(review.path, review.body))}>Confirm exact command</button><button disabled={busy} onClick={() => setReview(null)}>Back to review</button></div></section>}
    <section className={styles.job}><div className={styles.jobHeading}><h4>Selected check / run history</h4><button disabled={historyBusy} onClick={() => loadHistory()}>Refresh history</button></div><p className={styles.help} role="status">{historyNotice}</p><ul className={styles.recent}>{history?.items.map(item => <li key={item.job.id}><button disabled={busy} aria-pressed={entry?.job.id === item.job.id} onClick={() => selectJob(item.job.id)}>{item.job.job_type} · {item.job.state}<code>{item.job.id}</code></button></li>)}</ul>{history && history.items.length === 0 && <p className={styles.help}>No selected check or run has been retained.</p>}{history?.hasMore && <button disabled={historyBusy} onClick={() => loadHistory(true)}>Load more selected jobs</button>}
      <div className={styles.resume}><input aria-label="Exact selected job UUID" value={resumeId} onChange={event => setResumeId(event.target.value)} maxLength={36} placeholder="Check or run job UUID" /><button disabled={busy || !UUID.test(resumeId)} onClick={() => selectJob(resumeId)}>Inspect exact job</button><button disabled={locked || !UUID.test(resumeId)} onClick={() => { setLive(false); setReview({ title: "Cancel this exact selected job", path: `${base}/jobs/${resumeId}/cancel`, body: { confirm: true } }); }}>Review cancellation by job UUID</button></div><p className={styles.help}>An exact job can still be cancelled if current task authority or positive binding evidence is unavailable. The server verifies ownership; cancellation supplies no execution or verification grant.</p>
    </section>
    {jobNotice && <p className={styles.notice} role="status">{jobNotice}</p>}{selectedRef.current && <div className={styles.actions}><button disabled={jobBusy} onClick={() => { pollCount.current = 0; setPollNotice(""); void loadJob(selectedRef.current!); }}>Refresh selected job</button></div>}
    {entry && <section className={styles.job} aria-label="Selected job evidence"><div className={styles.jobHeading}><h4>{entry.job.job_type}</h4><span>{entry.job.state}</span></div><code>{entry.job.id}</code>
      {active && <><p className={styles.help}>Monitoring reads retained state and events. It pauses when hidden and after 30 refresh cycles; it never resubmits a check or run.</p><button onClick={() => { setLive(value => !value); pollCount.current = 0; setPollNotice(""); }}>{live ? "Pause monitoring" : "Monitor retained progress"}</button>{pollNotice && <p className={styles.help}>{pollNotice}</p>}<div className={styles.actions}><button disabled={locked || jobBusy} onClick={() => { setLive(false); setReview({ title: "Cancel this exact selected job", path: `${base}/jobs/${entry.job.id}/cancel`, body: { confirm: true } }); }}>Review cancellation</button></div></>}
      {entry.job.error_code && <p className={styles.blocked}><code>{entry.job.error_code}</code></p>}
      {entry.job.job_type === "operation.check" && <><SelectedCheckView entry={entry} /><div className={styles.actions}><button className={styles.launch} disabled={!canLaunch} onClick={launch}>Review launch of checked request</button><button disabled={locked || jobBusy || !context || !taskExecutionBindingMatchesContext(entry.binding, context)} onClick={() => selectJob(entry.job.id)}>Use this checked input and assertions</button></div>{launched && <p className={styles.help}>A retained run already references this check. Inspect that run; this check is not a fresh launch grant.</p>}{!draftMatches() && <p className={styles.help}>The current form differs from this check. Review a new check or restore this exact checked input and assertions.</p>}</>}
      <SelectedJSON title="Immutable selection, implementation, input and assertion binding" value={entry.binding} />
      {events.length > 0 && <details className={styles.details}><summary>Retained job events ({events.length})</summary><ol className={styles.events}>{events.map(item => <li key={item.id}><time>{item.recorded_at}</time><span>{item.event_type}{item.data.stage ? ` · ${item.data.stage}` : ""}</span>{item.data.text && <pre>{item.data.text}</pre>}</li>)}</ol></details>}
      {result && <SelectedOutcomeView result={result} downloading={downloading} download={() => void download()} />}
    </section>}
  </section>;
}
