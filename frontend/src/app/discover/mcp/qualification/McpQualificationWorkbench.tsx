"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { UUID, projectQualificationSuites, projectQualificationSuiteEnvelope, projectQualificationAttempts, projectQualificationAttemptEnvelope, projectQualificationReportEnvelope, projectQualificationMutation,
  type QualificationSuite, type QualificationAttempt, type QualificationReport, type QualificationPage, type QualificationDecision } from "../../../../lib/mcp-qualification.ts";
import { MCP_QUALIFICATION_ENDPOINT, QUALIFICATION_ATTEMPT_LABELS, QUALIFICATION_MAX_POLLS, qualificationScopeInvalidated, qualificationJournalScope, qualificationCommandRoute,
  newQualificationCommand, validateQualificationCommand, qualificationAttemptNeedsMonitoring, qualificationAttemptMatchesSuite, qualificationReportMatchesSuite, qualificationReportMatchesAttempt } from "../../../../lib/mcp-qualification-client.ts";
import { WorkshopRequests, recoverAttempt, retainAttempt, clearAttempt, clearRejectedAttempt, type WorkshopAttempt, type RequestTicket } from "../../../../lib/skill-workshop-client.ts";
import { SuiteReview, QualificationAttemptReview, QualificationReportReview, readable } from "./McpQualificationViews.tsx";
import styles from "../../../skills/skills.module.css";

const headers = (workspaceId: string) => ({ "X-Potbot-Workspace": workspaceId });
const VISIBLE_LIMIT = 200;

function ScopedWorkbench({ initial, initialSuiteId, initialBindingId, invalidateScope }: { initial: QualificationPage<QualificationSuite>; initialSuiteId: string | null; initialBindingId: string | null; invalidateScope: () => void }) {
  const scope = initial;
  const [suites, setSuites] = useState(initial), [listCurrent, setListCurrent] = useState(true), [listBusy, setListBusy] = useState(false), [listNotice, setListNotice] = useState("");
  const [selected, setSelected] = useState<string | null>(initialSuiteId), [suite, setSuite] = useState<QualificationSuite | null>(null), [suiteCurrent, setSuiteCurrent] = useState(false), [detailNotice, setDetailNotice] = useState("");
  const [attempts, setAttempts] = useState<QualificationPage<QualificationAttempt> | null>(null), [historyBusy, setHistoryBusy] = useState(false), [historyNotice, setHistoryNotice] = useState("");
  const [attempt, setAttempt] = useState<QualificationAttempt | null>(null), [attemptCurrent, setAttemptCurrent] = useState(false);
  const [report, setReport] = useState<QualificationReport | null>(null), [reportCurrent, setReportCurrent] = useState(false), [reportBusy, setReportBusy] = useState(false), [reportNotice, setReportNotice] = useState("");
  const [filter, setFilter] = useState(initialBindingId ?? ""), [filterDirty, setFilterDirty] = useState(false), [openId, setOpenId] = useState(initialSuiteId ?? "");
  const [pending, setPending] = useState<WorkshopAttempt | null>(null), [recovered, setRecovered] = useState(false), [durable, setDurable] = useState<boolean | null>(null), [busy, setBusy] = useState(false), [notice, setNotice] = useState("");
  const [live, setLive] = useState(true), [pollNotice, setPollNotice] = useState(""), [visibility, setVisibility] = useState(0);
  const listRequests = useRef(new WorkshopRequests()), suiteRequests = useRef(new WorkshopRequests()), historyRequests = useRef(new WorkshopRequests()), attemptRequests = useRef(new WorkshopRequests()), reportRequests = useRef(new WorkshopRequests()), mutationRequests = useRef(new WorkshopRequests());
  const selectedRef = useRef<string | null>(initialSuiteId), suiteRef = useRef<QualificationSuite | null>(null), suitesRef = useRef(initial), attemptsRef = useRef<QualificationPage<QualificationAttempt> | null>(null), attemptRef = useRef<QualificationAttempt | null>(null), reportRef = useRef<QualificationReport | null>(null);
  const filterRef = useRef(initialBindingId), pendingRef = useRef<WorkshopAttempt | null>(null), journal = useRef(""), busyRef = useRef(false), pollCount = useRef(0), terminalRefresh = useRef<(identity: string) => void>(() => {});

  const abortReads = useCallback(() => { for (const ref of [listRequests, suiteRequests, historyRequests, attemptRequests, reportRequests]) ref.current.reset(); }, []);
  const leaveScope = useCallback(() => { abortReads(); mutationRequests.current.reset(); invalidateScope(); }, [abortReads, invalidateScope]);
  useEffect(() => {
    journal.current = qualificationJournalScope(scope.workspaceId, scope.principalId); const ticket = mutationRequests.current.open();
    void recoverAttempt(journal.current, validateQualificationCommand).then(value => { if (ticket.current()) { pendingRef.current = value; setPending(value); setRecovered(true); } }).finally(ticket.finish);
    const changed = () => setVisibility(value => value + 1); document.addEventListener("visibilitychange", changed);
    return () => { document.removeEventListener("visibilitychange", changed); abortReads(); mutationRequests.current.reset(); };
  }, [scope.workspaceId, scope.principalId, abortReads]);

  const read = useCallback(async (path: string, ticket: RequestTicket): Promise<unknown> => {
    const response = await fetch(MCP_QUALIFICATION_ENDPOINT + path, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: headers(scope.workspaceId) });
    if (!ticket.current()) return null;
    if (qualificationScopeInvalidated(response.status, null, scope.workspaceId, scope.principalId)) { leaveScope(); return null; }
    if (!response.ok) throw new Error(response.status === 409 ? "The qualification record changed. Refresh its current exact revision." : "The retained record is unavailable. Refresh before making a decision.");
    const value = await response.json(); if (!ticket.current()) return null;
    if (qualificationScopeInvalidated(response.status, value, scope.workspaceId, scope.principalId)) { leaveScope(); return null; }
    return value;
  }, [scope.workspaceId, scope.principalId, leaveScope]);

  const loadSuites = useCallback((append = false) => {
    const previous = suitesRef.current, cursor = append ? previous.nextCursor : null, binding = filterRef.current; if (append && !cursor) return;
    listRequests.current.reset(); const ticket = listRequests.current.open(); setListBusy(true); if (!append) setListCurrent(false); setListNotice("Reading retained suites…");
    void (async () => { try {
      const raw = await read(`/suites?limit=25${binding ? "&bindingId=" + binding : ""}${cursor ? "&after=" + cursor : ""}`, ticket); if (!ticket.current() || filterRef.current !== binding) return;
      const result = projectQualificationSuites(raw, scope.workspaceId, scope.principalId, binding ?? undefined); if (!result || append && result.items.some(item => previous.items.some(old => old.id === item.id))) throw new Error("Invalid or overlapping suite page. Refresh the list.");
      const next = append ? { ...result, items: [...previous.items, ...result.items].slice(0, VISIBLE_LIMIT) } : result;
      suitesRef.current = next; setSuites(next); setListCurrent(true); setListNotice(next.items.length ? "" : "No admitted qualification suites match this binding filter.");
      const current = suiteRef.current, observed = result.items.find(item => item.id === selectedRef.current);
      if (current && observed && observed.revision >= current.revision && JSON.stringify(observed) !== JSON.stringify(current)) {
        suiteRef.current = observed; setSuite(observed); setSuiteCurrent(false); setAttemptCurrent(false); reportRequests.current.reset(); setReportCurrent(false); setReportBusy(false);
        setDetailNotice("The selected suite changed. Refresh it before reviewing another decision or current report.");
      }
    } catch (error) { if (ticket.current()) { setListCurrent(false); setListNotice(error instanceof Error ? error.message : "Suites are unavailable."); } }
    finally { if (ticket.current()) setListBusy(false); ticket.finish(); } })();
  }, [read, scope.workspaceId, scope.principalId]);

  const loadReport = useCallback((identity: string, fromAttempt = false) => {
    const owner = suiteRef.current, selectedAttempt = fromAttempt ? attemptRef.current : null; if (!owner || selectedRef.current !== owner.id || fromAttempt && selectedAttempt?.reportId !== identity) return;
    reportRequests.current.reset(); const ticket = reportRequests.current.open(); setReportBusy(true); setReportCurrent(false); setReportNotice("Reading retained report evidence…");
    void (async () => { try {
      const raw = await read("/reports/" + identity, ticket); if (!ticket.current() || selectedRef.current !== owner.id || !suiteRef.current) return;
      const result = projectQualificationReportEnvelope(raw, scope.workspaceId, identity, scope.principalId);
      if (!result || !qualificationReportMatchesSuite(result.report, suiteRef.current) || selectedAttempt && (!attemptRef.current || attemptRef.current.id !== selectedAttempt.id || !qualificationReportMatchesAttempt(result.report, attemptRef.current, suiteRef.current))) throw new Error("The report differs from the exact selected suite or attempt. Refresh its retained owner.");
      reportRef.current = result.report; setReport(result.report); setReportCurrent(true); setReportNotice("");
    } catch (error) { if (ticket.current()) setReportNotice(error instanceof Error ? error.message : "Report unavailable."); }
    finally { if (ticket.current()) setReportBusy(false); ticket.finish(); } })();
  }, [read, scope.workspaceId, scope.principalId]);

  const loadAttempt = useCallback((identity: string, monitor = false) => {
    attemptRequests.current.reset(); const ticket = attemptRequests.current.open(), owner = selectedRef.current; if (!monitor) setAttemptCurrent(false);
    void (async () => { try {
      const raw = await read("/attempts/" + identity, ticket); if (!ticket.current() || selectedRef.current !== owner || !suiteRef.current) return;
      const result = projectQualificationAttemptEnvelope(raw, scope.workspaceId, identity, scope.principalId); if (!result || !qualificationAttemptMatchesSuite(result.attempt, suiteRef.current)) throw new Error("The attempt differs from the selected suite and its ordered case identities.");
      const previous = attemptRef.current; if (previous?.id === identity && previous.revision > result.attempt.revision) throw new Error("A newer attempt revision is already retained. Refresh its current state.");
      attemptRef.current = result.attempt; setAttempt(result.attempt); setAttemptCurrent(true);
      setAttempts(previous => { if (!previous) return previous; const next = { ...previous, items: previous.items.map(item => item.id === identity ? result.attempt : item) }; attemptsRef.current = next; return next; });
      if (reportRef.current && (reportRef.current.id !== result.attempt.reportId || reportRef.current.sha256 !== result.attempt.reportSha256)) { reportRequests.current.reset(); reportRef.current = null; setReport(null); setReportCurrent(false); setReportBusy(false); }
      if (result.attempt.reportId) loadReport(result.attempt.reportId, true);
      if (!qualificationAttemptNeedsMonitoring(result.attempt)) {
        const changed = previous?.id === identity && qualificationAttemptNeedsMonitoring(previous); setLive(false);
        setPollNotice(changed ? "The attempt is terminal. Current suite and report evidence are being reread." : "The retained attempt is terminal.");
        if (changed) terminalRefresh.current(result.attempt.suiteId);
      }
    } catch (error) { if (ticket.current()) { setAttemptCurrent(false); setLive(false); setPollNotice(error instanceof Error ? error.message : "Attempt status unavailable."); } }
    finally { ticket.finish(); } })();
  }, [read, scope.workspaceId, scope.principalId, loadReport]);

  const loadHistory = useCallback((append = false) => {
    const owner = selectedRef.current; if (!owner) return; const previous = attemptsRef.current, cursor = append ? previous?.nextCursor : null; if (append && !cursor) return;
    historyRequests.current.reset(); const ticket = historyRequests.current.open(); setHistoryBusy(true); setHistoryNotice("Reading attempt history…");
    void (async () => { try {
      const raw = await read(`/attempts?suiteId=${owner}&limit=25${cursor ? "&after=" + cursor : ""}`, ticket); if (!ticket.current() || selectedRef.current !== owner || !suiteRef.current) return;
      const result = projectQualificationAttempts(raw, scope.workspaceId, scope.principalId, owner); if (!result || result.items.some(item => !qualificationAttemptMatchesSuite(item, suiteRef.current!)) || append && result.items.some(item => previous?.items.some(old => old.id === item.id))) throw new Error("Invalid, changed or overlapping attempt page. Refresh history.");
      const next = append && previous ? { ...result, items: [...previous.items, ...result.items].slice(0, VISIBLE_LIMIT) } : result; attemptsRef.current = next; setAttempts(next); setHistoryNotice(next.items.length ? "" : "No qualification attempts retained for this suite.");
    } catch (error) { if (ticket.current()) setHistoryNotice(error instanceof Error ? error.message : "Attempt history unavailable."); }
    finally { if (ticket.current()) setHistoryBusy(false); ticket.finish(); } })();
  }, [read, scope.workspaceId, scope.principalId]);

  const loadSuite = useCallback((identity: string) => {
    suiteRequests.current.reset(); reportRequests.current.reset(); const ticket = suiteRequests.current.open(); setSuiteCurrent(false); setReportCurrent(false); setReportBusy(false); setDetailNotice("Reading the exact suite…");
    void (async () => { try {
      const raw = await read("/suites/" + identity, ticket); if (!ticket.current() || selectedRef.current !== identity) return;
      const result = projectQualificationSuiteEnvelope(raw, scope.workspaceId, identity, scope.principalId); if (!result || filterRef.current && result.suite.bindingId !== filterRef.current) throw new Error("The suite response is invalid or differs from the selected binding filter.");
      if (suiteRef.current && result.suite.revision < suiteRef.current.revision) throw new Error("A newer suite revision is already retained. Refresh its current state.");
      suiteRef.current = result.suite; setSuite(result.suite); setSuiteCurrent(true); setDetailNotice("");
      setSuites(previous => { const next = { ...previous, items: previous.items.map(item => item.id === identity ? result.suite : item) }; suitesRef.current = next; return next; });
      loadHistory();
      const attemptId = result.suite.activeAttemptId ?? result.suite.lastAttemptId;
      if (attemptId && attemptRef.current === null) loadAttempt(attemptId);
      else if (reportRef.current) loadReport(reportRef.current.id, attemptRef.current?.reportId === reportRef.current.id);
      else if (attemptRef.current?.reportId) loadReport(attemptRef.current.reportId, true);
    } catch (error) { if (ticket.current()) setDetailNotice(error instanceof Error ? error.message : "Suite unavailable."); }
    finally { ticket.finish(); } })();
  }, [read, scope.workspaceId, scope.principalId, loadHistory, loadAttempt, loadReport]);
  useEffect(() => { terminalRefresh.current = identity => { if (identity === selectedRef.current) { loadSuite(identity); loadSuites(); } }; }, [loadSuite, loadSuites]);

  const clearSelection = useCallback(() => {
    for (const ref of [suiteRequests, historyRequests, attemptRequests, reportRequests]) ref.current.reset();
    selectedRef.current = null; setSelected(null); suiteRef.current = null; setSuite(null); setSuiteCurrent(false); setDetailNotice("");
    attemptsRef.current = null; setAttempts(null); setHistoryBusy(false); setHistoryNotice(""); attemptRef.current = null; setAttempt(null); setAttemptCurrent(false);
    reportRef.current = null; setReport(null); setReportCurrent(false); setReportBusy(false); setReportNotice(""); setLive(true); pollCount.current = 0; setPollNotice("");
  }, []);
  const select = useCallback((identity: string) => {
    clearSelection(); selectedRef.current = identity; setSelected(identity); setOpenId(identity);
    const url = new URL(window.location.href); url.searchParams.set("workspace", scope.workspaceId); url.searchParams.set("suite", identity); window.history.replaceState(null, "", url); loadSuite(identity);
  }, [scope.workspaceId, clearSelection, loadSuite]);
  useEffect(() => { if (initialSuiteId) select(initialSuiteId); }, [initialSuiteId, select]);

  useEffect(() => {
    if (!live || !attempt || !qualificationAttemptNeedsMonitoring(attempt) || busy || document.hidden) return;
    if (pollCount.current >= QUALIFICATION_MAX_POLLS) { setLive(false); setPollNotice("Automatic status reads paused after 30 checks. Resume when the host is ready."); return; }
    const timer = setTimeout(() => { pollCount.current++; loadAttempt(attempt.id, true); }, 3000); return () => clearTimeout(timer);
  }, [attempt, live, busy, visibility, loadAttempt]);

  const submit = useCallback(async (original: WorkshopAttempt, recovering: boolean) => {
    if (busyRef.current || !recovered) return; busyRef.current = true; setBusy(true); abortReads(); setListCurrent(false); setSuiteCurrent(false); setAttemptCurrent(false); setReportCurrent(false);
    const ticket = mutationRequests.current.open(); let stored = original;
    try {
      if (!recovering) { const saved = await retainAttempt(journal.current, original); if (!ticket.current()) return; setDurable(saved); }
      else { stored = (await recoverAttempt(journal.current, validateQualificationCommand)) ?? original; if (stored.key !== original.key || stored.body !== original.body || stored.path !== original.path) throw new Error("The retained request differs from the selected recovery."); }
      if (!ticket.current()) return; pendingRef.current = stored; setPending(stored); setNotice("Submitting the exact reviewed request…");
      const response = await fetch(stored.path, { method: "POST", credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: { ...headers(scope.workspaceId), "Content-Type": "application/json", "Idempotency-Key": stored.key }, body: stored.body });
      if (!ticket.current()) return;
      if (qualificationScopeInvalidated(response.status, null, scope.workspaceId, scope.principalId)) { leaveScope(); return; }
      if (!response.ok) {
        if (await clearRejectedAttempt(journal.current, stored.key, response.status, recovering)) { if (!ticket.current()) return; pendingRef.current = null; setPending(null); setNotice("The request was rejected. Refresh the current record and review a new decision."); }
        else setNotice("The outcome is uncertain. Retain this exact request and retry it explicitly to recover its receipt."); return;
      }
      const raw = await response.json(); if (!ticket.current()) return;
      if (qualificationScopeInvalidated(response.status, raw, scope.workspaceId, scope.principalId)) { leaveScope(); return; }
      const path = qualificationCommandRoute(stored.path), result = path && projectQualificationMutation(raw, scope.workspaceId, path, JSON.parse(stored.body), scope.principalId);
      if (!result) throw new Error("The response did not match this exact request. Its original recovery record is retained.");
      await clearAttempt(journal.current, stored.key); if (!ticket.current()) return;
      pendingRef.current = null; setPending(null); setDurable(null); setNotice("Exact request confirmed. Reading its current retained state…");
      if ("attempt" in result && result.attempt.suiteId === selectedRef.current) { attemptRef.current = result.attempt; setAttempt(result.attempt); setAttemptCurrent(false); pollCount.current = 0; setLive(qualificationAttemptNeedsMonitoring(result.attempt)); setPollNotice(""); loadAttempt(result.attempt.id); }
    } catch (error) { if (ticket.current()) setNotice(error instanceof Error ? error.message : "The outcome is uncertain. Retain the exact request before retrying."); }
    finally { if (ticket.current()) { busyRef.current = false; setBusy(false); loadSuites(); const owner = selectedRef.current; if (owner) loadSuite(owner); } ticket.finish(); }
  }, [recovered, abortReads, scope.workspaceId, scope.principalId, leaveScope, loadSuites, loadSuite, loadAttempt]);
  const mutate = useCallback((path: string[], body: QualificationDecision) => {
    if (busyRef.current || pendingRef.current || !recovered) return;
    void submit(newQualificationCommand(MCP_QUALIFICATION_ENDPOINT + "/" + path.join("/"), body), false);
  }, [recovered, submit]);
  const locked = busy || pending !== null || !recovered;

  return <>
    {pending && <section className={styles.uncertain} aria-label="Unresolved qualification request"><h3>Recover the original request</h3><p>Keep the same path, body and key to learn whether this decision was committed. Loading this journal does not resubmit it.</p><code>{pending.path}</code><code>{pending.key}</code><details><summary>Inspect original request body</summary><pre>{pending.body}</pre></details>
      {durable === false && <p>Persistent browser storage is unavailable. This tab retains a temporary recovery copy; preserve the displayed request before leaving.</p>}<button disabled={busy || !recovered} onClick={() => void submit(pending, true)}>Retry the identical request</button></section>}
    {notice && <p className={styles.status} role="status">{notice}</p>}
    <div className={styles.layout}><aside className={styles.panel}><header className={styles.panelHeading}><h2>Admitted suites</h2><p>Inspect retained cases and their latest task evidence.</p></header><div className={styles.body}>
      <form className={styles.form} onSubmit={event => { event.preventDefault(); if (filter && !UUID.test(filter)) return; clearSelection(); filterRef.current = filter || null; setFilterDirty(false); suitesRef.current = { ...scope, items: [], nextCursor: null }; setSuites(suitesRef.current);
        const url = new URL(window.location.href); url.searchParams.delete("suite"); if (filter) url.searchParams.set("binding", filter); else url.searchParams.delete("binding"); window.history.replaceState(null, "", url); loadSuites();
      }}><label className={styles.field}>Filter by MCP binding ID<input value={filter} onChange={event => { setFilter(event.target.value); setFilterDirty(true); }} placeholder="Optional binding UUID" /></label><button disabled={busy || !!filter && !UUID.test(filter)}>Apply binding filter</button></form>
      {filterDirty && <p className={styles.help}>Apply this filter to start a new suite page.</p>}<button disabled={listBusy || busy} onClick={() => loadSuites()}>Refresh suites</button>
      <form className={styles.form} onSubmit={event => { event.preventDefault(); if (UUID.test(openId)) select(openId); }}><label className={styles.field}>Open exact suite ID<input value={openId} onChange={event => setOpenId(event.target.value)} placeholder="Suite UUID" /></label><button disabled={!UUID.test(openId) || busy}>Open retained suite</button></form>{listNotice && <p className={styles.status}>{listNotice}</p>}
    </div><ul className={styles.cards}>{suites.items.map(item => <li key={item.id}><button disabled={busy || !listCurrent} className={selected === item.id ? styles.selected : undefined} onClick={() => select(item.id)}><strong>{item.name}</strong><span className={styles.badges}><span className={styles.badge}>{readable(item.state)}</span><span className={styles.badge}>{item.cases.length} cases</span></span><small>{item.toolName} · {readable(item.transport)}</small></button></li>)}</ul>
      <div className={styles.body}>{suites.nextCursor && <button disabled={listBusy || busy || !listCurrent || suites.items.length >= VISIBLE_LIMIT} onClick={() => loadSuites(true)}>Load more suites</button>}{suites.items.length >= VISIBLE_LIMIT && <p className={styles.help}>This view holds 200 suites. Filter by binding or open an exact suite ID to inspect another.</p>}</div>
    </aside><article className={styles.panel}><div className={styles.body}>{!selected ? <div className={styles.empty}><h2>Select an admitted qualification suite</h2><p>The configured host admits an exact private case suite for an existing MCP installation and task. Its safe case identities and retained reports appear here.</p></div> : <>
      <div className={styles.actions}><button disabled={busy} onClick={() => loadSuite(selected)}>Refresh selected suite</button></div>{detailNotice && <p className={styles.status} role="status">{detailNotice}</p>}
      {suite && <><SuiteReview suite={suite} locked={locked} current={suiteCurrent} mutate={mutate} />
        <section className={styles.section} aria-label="Qualification history"><h3>Qualification history</h3><button disabled={historyBusy || busy} onClick={() => loadHistory()}>Refresh history</button>{historyNotice && <p className={styles.status}>{historyNotice}</p>}
          <ul className={styles.history}>{attempts?.items.map(item => <li key={item.id}><div className={styles.row}><strong>{QUALIFICATION_ATTEMPT_LABELS[item.status]}</strong><button disabled={busy} onClick={() => { attemptRef.current = null; setAttempt(null); reportRequests.current.reset(); reportRef.current = null; setReport(null); setReportCurrent(false); pollCount.current = 0; setLive(true); setPollNotice(""); loadAttempt(item.id); }}>Inspect attempt</button></div><time>{new Date(item.createdAt).toLocaleString()}</time><p><code>{item.id}</code> · {item.completedCases}/{item.caseCount} finished</p></li>)}</ul>
          {attempts?.nextCursor && <button disabled={historyBusy || busy || attempts.items.length >= VISIBLE_LIMIT} onClick={() => loadHistory(true)}>Load more attempts</button>}
        </section>
        {attempt && <><QualificationAttemptReview attempt={attempt} locked={locked} current={attemptCurrent} mutate={mutate} /><div className={styles.actions}><button disabled={busy} onClick={() => loadAttempt(attempt.id)}>Refresh attempt</button>{qualificationAttemptNeedsMonitoring(attempt) && <button onClick={() => { pollCount.current = 0; setLive(value => !value); setPollNotice(""); }}>{live ? "Pause automatic status reads" : "Resume bounded status reads"}</button>}{attempt.reportId && <button disabled={busy || reportBusy || !attemptCurrent} onClick={() => loadReport(attempt.reportId!, true)}>Inspect attempt report</button>}</div>{pollNotice && <p className={styles.status} role="status">{pollNotice}</p>}</>}
        {suite.lastReportId && <div className={styles.actions}><button disabled={busy || reportBusy || !suiteCurrent} onClick={() => loadReport(suite.lastReportId!)}>Inspect latest retained suite report</button></div>}
        {reportNotice && <p className={styles.status} role="status">{reportNotice}</p>}{report && <><QualificationReportReview report={report} suite={suite} current={reportCurrent && suiteCurrent} /><button disabled={busy || reportBusy} onClick={() => loadReport(report.id, attempt?.reportId === report.id)}>Refresh report</button></>}
      </>}
    </>}</div></article></div>
  </>;
}

export default function McpQualificationWorkbench({ workspaceId, initialSuiteId, initialBindingId }: { workspaceId: string; initialSuiteId: string | null; initialBindingId: string | null }) {
  const [initial, setInitial] = useState<QualificationPage<QualificationSuite> | null>(null), [notice, setNotice] = useState("Loading admitted qualification suites…"), [reload, setReload] = useState(0), [paused, setPaused] = useState(false);
  const invalidateScope = useCallback(() => { setInitial(null); setPaused(true); setNotice("The authenticated scope changed or access was denied. Reopen the workspace before inspecting private records."); }, []);
  useEffect(() => {
    if (paused) return; const request = new AbortController(); setInitial(null); setNotice("Loading admitted qualification suites…");
    void fetch(MCP_QUALIFICATION_ENDPOINT + "/suites?limit=25" + (initialBindingId ? "&bindingId=" + initialBindingId : ""), { credentials: "same-origin", cache: "no-store", headers: headers(workspaceId), signal: request.signal }).then(async response => {
      if (!response.ok) throw new Error("unavailable"); const raw = await response.json(); if (request.signal.aborted) return; const result = projectQualificationSuites(raw, workspaceId, undefined, initialBindingId ?? undefined);
      if (!result) throw new Error("invalid"); setInitial(result); setNotice("");
    }).catch(() => { if (!request.signal.aborted) setNotice("Retained MCP qualification is unavailable. Check this workspace’s operator access and backend configuration."); });
    return () => request.abort();
  }, [workspaceId, initialBindingId, reload, paused]);
  return initial ? <ScopedWorkbench key={`${initial.workspaceId}:${initial.principalId}`} initial={initial} initialSuiteId={initialSuiteId} initialBindingId={initialBindingId} invalidateScope={invalidateScope} /> :
    <section className={styles.panel}><div className={styles.body}><p className={styles.status} role="status">{notice}</p><button onClick={() => { setPaused(false); setReload(value => value + 1); }}>Reload authorized suites</button></div></section>;
}
