"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { CAPABILITIES, EFFECTS, PLATFORMS } from "../../../lib/discovery.ts";
import { UUID, EMPTY_TASK_CONSTRAINTS, projectTaskEnvelope, projectTaskSearches, projectTaskMutation, validateTaskMutation, parseMcpJSON, sameTaskValue,
  type TaskDiscoveryPage, type TaskDiscoveryContext, type TaskDiscoverySearch, type TaskDiscoveryResult, type TaskDiscoverySelection, type TaskDiscoveryConstraints, type RankedTaskCandidate } from "../../../lib/task-discovery.ts";
import { TASK_DISCOVERY_ENDPOINT as BASE, taskDiscoveryScopeInvalidated, taskDiscoveryJournalScope, taskDiscoveryAttemptRoute, newTaskDiscoveryAttempt, validateTaskDiscoveryAttempt,
  taskDiscoveryNeedsMonitoring, taskResultMatches, taskSelectionMatches, taskContextMatches, taskCanSelect } from "../../../lib/task-discovery-client.ts";
import { WorkshopRequests, retainAttempt, recoverAttempt, clearAttempt, clearRejectedAttempt, type RequestTicket, type WorkshopAttempt } from "../../../lib/skill-workshop-client.ts";
import { TaskContextView, TaskJSON, TaskResultView } from "./TaskDiscoveryViews.tsx";
import styles from "../../skills/skills.module.css";

type InitialSelection = { episode: string | null; state: string | null; search: string | null };
type Review = { title: string; path: string; body: Record<string, unknown> };
type Detail = { search: TaskDiscoverySearch; result: TaskDiscoveryResult | null; selection: TaskDiscoverySelection | null };
const headers = (workspace: string) => ({ "X-Potbot-Workspace": workspace });
const split = (value: string) => value.split(/[\n,]/).map(item => item.trim()).filter(Boolean);

function WorkspaceTaskDiscovery({ initial, initialSelection, invalidateScope }: { initial: TaskDiscoveryPage; initialSelection: InitialSelection; invalidateScope: () => void }) {
  const { workspaceId, principalId } = initial;
  const [history, setHistory] = useState(initial), [historyBusy, setHistoryBusy] = useState(false), [historyNotice, setHistoryNotice] = useState("");
  const [episode, setEpisode] = useState(initialSelection.episode ?? ""), [state, setState] = useState(initialSelection.state ?? "");
  const [context, setContext] = useState<TaskDiscoveryContext | null>(null), [contextBusy, setContextBusy] = useState(false), [contextNotice, setContextNotice] = useState("");
  const [query, setQuery] = useState(""), [constraints, setConstraints] = useState<TaskDiscoveryConstraints>(structuredClone(EMPTY_TASK_CONSTRAINTS));
  const [inputTypes, setInputTypes] = useState(""), [outputTypes, setOutputTypes] = useState(""), [scopes, setScopes] = useState("");
  const [mode, setMode] = useState<"lexical" | "hybrid">("lexical"), [limit, setLimit] = useState(5), [spaceKey, setSpaceKey] = useState(""), [maxCandidates, setMaxCandidates] = useState(8), [runtimeBudget, setRuntimeBudget] = useState(30000);
  const [selectedId, setSelectedId] = useState<string | null>(initialSelection.search), [detail, setDetail] = useState<Detail | null>(null), [detailBusy, setDetailBusy] = useState(false), [detailNotice, setDetailNotice] = useState("");
  const [inspection, setInspection] = useState<{ candidate: RankedTaskCandidate; detail: Record<string, unknown> } | null>(null), [inspectionNotice, setInspectionNotice] = useState("");
  const [review, setReview] = useState<Review | null>(null), [pending, setPending] = useState<WorkshopAttempt | null>(null), [recovered, setRecovered] = useState(false), [busy, setBusy] = useState(false), [durable, setDurable] = useState<boolean | null>(null), [notice, setNotice] = useState("");
  const [live, setLive] = useState(true), [visibility, setVisibility] = useState(0), [pollNotice, setPollNotice] = useState("");
  const lists = useRef(new WorkshopRequests()), contexts = useRef(new WorkshopRequests()), details = useRef(new WorkshopRequests()), inspections = useRef(new WorkshopRequests()), mutations = useRef(new WorkshopRequests());
  const pageRef = useRef(initial), selectedRef = useRef(initialSelection.search), pendingRef = useRef<WorkshopAttempt | null>(null), busyRef = useRef(false), journal = useRef(""), pollCount = useRef(0);

  const abortReads = useCallback(() => { lists.current.reset(); contexts.current.reset(); details.current.reset(); inspections.current.reset(); }, []);
  const leaveScope = useCallback(() => { abortReads(); mutations.current.reset(); invalidateScope(); }, [abortReads, invalidateScope]);
  useEffect(() => {
    journal.current = taskDiscoveryJournalScope(workspaceId, principalId); const ticket = mutations.current.open();
    void recoverAttempt(journal.current, validateTaskDiscoveryAttempt).then(value => { if (ticket.current()) { pendingRef.current = value; setPending(value); setRecovered(true); } ticket.finish(); });
    const changed = () => setVisibility(value => value + 1); document.addEventListener("visibilitychange", changed);
    return () => { document.removeEventListener("visibilitychange", changed); abortReads(); mutations.current.reset(); };
  }, [workspaceId, principalId, abortReads]);

  const read = useCallback(async (path: string, ticket: RequestTicket): Promise<unknown> => {
    const response = await fetch(path, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: headers(workspaceId) });
    if (!ticket.current()) return null;
    if (taskDiscoveryScopeInvalidated(response.status, null, workspaceId, principalId)) { leaveScope(); return null; }
    if (!response.ok) throw new Error(response.status === 409 ? "This observation no longer matches the current task or owner. Refresh its context." : "This retained record is unavailable.");
    const raw = await response.json(); if (!ticket.current()) return null;
    if (taskDiscoveryScopeInvalidated(response.status, raw, workspaceId, principalId)) { leaveScope(); return null; }
    return raw;
  }, [workspaceId, principalId, leaveScope]);

  const loadHistory = useCallback((append = false) => {
    const previous = pageRef.current, cursor = append ? previous.nextCursor : null; if (append && !cursor) return;
    lists.current.reset(); const ticket = lists.current.open(); setHistoryBusy(true); setHistoryNotice("Reading retained search history…");
    void (async () => {
      try {
        const raw = await read(`${BASE}/searches?${new URLSearchParams({ limit: "25", ...(cursor ? { after: cursor } : {}) })}`, ticket); if (!ticket.current()) return;
        const page = projectTaskSearches(raw, workspaceId, principalId); if (!page || cursor && (page.nextCursor === cursor || page.items.some(item => item.id <= cursor))) throw new Error("The history cursor did not advance safely.");
        const items = append ? [...new Map([...previous.items, ...page.items].map(item => [item.id, item])).values()] : page.items;
        const next = { ...page, items: items.slice(-200) }; pageRef.current = next; setHistory(next);
        setHistoryNotice(items.length > 200 ? "Showing the last 200 loaded searches. Refresh to return to the beginning." : page.nextCursor ? "More retained searches are available." : "All searches in this bounded history are shown.");
      } catch (error) { if (ticket.current()) setHistoryNotice(error instanceof Error ? error.message : "History could not be refreshed. The cursor has not advanced."); }
      finally { if (ticket.current()) setHistoryBusy(false); ticket.finish(); }
    })();
  }, [read, workspaceId, principalId]);

  const loadDetail = useCallback(async (id: string) => {
    details.current.reset(); inspections.current.reset(); const ticket = details.current.open(); setDetailBusy(true); setDetail(null); setInspection(null); setInspectionNotice(""); setReview(null); setDetailNotice("Reading the exact search and retained evidence…");
    try {
      const raw = await read(`${BASE}/searches/${id}`, ticket); if (!ticket.current() || selectedRef.current !== id) return;
      const envelope = projectTaskEnvelope(raw, workspaceId, "search", id, principalId); if (!envelope || !("search" in envelope)) throw new Error("The search identity could not be verified.");
      const search = envelope.search; let result: TaskDiscoveryResult | null = null, selection: TaskDiscoverySelection | null = null;
      if (search.resultId) {
        const resultRaw = await read(`${BASE}/results/${search.resultId}`, ticket); if (!ticket.current() || selectedRef.current !== id) return;
        const resultEnvelope = projectTaskEnvelope(resultRaw, workspaceId, "result", search.resultId, principalId);
        if (!resultEnvelope || !("result" in resultEnvelope) || !taskResultMatches(search, resultEnvelope.result)) throw new Error("The result does not match this exact search. Refresh to read a consistent snapshot.");
        result = resultEnvelope.result;
      }
      if (search.selectionId) {
        const selectionRaw = await read(`${BASE}/selections/${search.selectionId}`, ticket); if (!ticket.current() || selectedRef.current !== id) return;
        const selectionEnvelope = projectTaskEnvelope(selectionRaw, workspaceId, "selection", search.selectionId, principalId);
        if (!result || !selectionEnvelope || !("selection" in selectionEnvelope) || !taskSelectionMatches(search, result, selectionEnvelope.selection)) throw new Error("The selection does not match this exact result and owner. Refresh to inspect it again.");
        selection = selectionEnvelope.selection;
      }
      setDetail({ search, result, selection }); setDetailNotice("");
    } catch (error) { if (ticket.current() && selectedRef.current === id) setDetailNotice(error instanceof Error ? error.message : "This search could not be refreshed."); }
    finally { if (ticket.current()) setDetailBusy(false); ticket.finish(); }
  }, [read, workspaceId, principalId]);

  const selectSearch = useCallback((id: string) => {
    selectedRef.current = id; setSelectedId(id); pollCount.current = 0; setPollNotice(""); setLive(true);
    const url = new URL(window.location.href); url.searchParams.set("workspace", workspaceId); url.searchParams.set("search", id); window.history.replaceState(null, "", url);
    void loadDetail(id);
  }, [workspaceId, loadDetail]);
  useEffect(() => { if (initialSelection.search) selectSearch(initialSelection.search); }, [initialSelection.search, selectSearch]);

  useEffect(() => {
    if (!detail || !live || !taskDiscoveryNeedsMonitoring(detail.search) || document.visibilityState === "hidden") return;
    if (pollCount.current >= 30) { setPollNotice("Automatic reads paused after 30 checks. Refresh explicitly to continue monitoring."); return; }
    const id = detail.search.id, timer = window.setTimeout(() => { pollCount.current++; void loadDetail(id); }, 5000);
    return () => window.clearTimeout(timer);
  }, [detail, live, visibility, loadDetail]);

  function changeTask(nextEpisode: string, nextState: string) {
    contexts.current.reset(); setEpisode(nextEpisode); setState(nextState); setContext(null); setContextBusy(false); setContextNotice(""); setSpaceKey(""); setReview(null);
    const url = new URL(window.location.href); url.searchParams.set("workspace", workspaceId); url.searchParams.delete("episode"); url.searchParams.delete("state");
    if (UUID.test(nextEpisode)) url.searchParams.set("episode", nextEpisode); if (UUID.test(nextState)) url.searchParams.set("state", nextState); window.history.replaceState(null, "", url);
  }
  function loadContext() {
    if (!UUID.test(episode) || !UUID.test(state)) { setContextNotice("Enter the exact task and repository state UUIDs."); return; }
    contexts.current.reset(); const ticket = contexts.current.open(); setContext(null); setContextBusy(true); setReview(null); setContextNotice("Reading current task, source and embedding registrations…");
    void (async () => {
      try {
        const raw = await read(`${BASE}/context?${new URLSearchParams({ episodeId: episode, stateId: state })}`, ticket); if (!ticket.current()) return;
        const envelope = projectTaskEnvelope(raw, workspaceId, "context", undefined, principalId);
        if (!envelope || !("context" in envelope) || !taskContextMatches(envelope.context, episode, state)) throw new Error("The returned task snapshot does not match these identities.");
        setContext(envelope.context); setSpaceKey(""); setContextNotice("Current task context reviewed. Choose constraints and review the exact search request.");
      } catch (error) { if (ticket.current()) setContextNotice(error instanceof Error ? error.message : "The task context is unavailable."); }
      finally { if (ticket.current()) setContextBusy(false); ticket.finish(); }
    })();
  }
  function reviewCreate() {
    if (!context || !taskContextMatches(context, episode, state)) return;
    const space = context.embeddingSpaces.find(item => `${item.spaceId}:${item.embeddingName}` === spaceKey);
    const body = { episodeId: episode, stateId: state, contextSha256: context.contextSha256, query, constraints: { ...constraints, inputTypes: split(inputTypes), outputTypes: split(outputTypes), permittedScopes: constraints.permittedScopes === null ? null : split(scopes) }, mode, limit,
      embedding: mode === "lexical" ? null : space ? { spaceId: space.spaceId, embeddingName: space.embeddingName, profileSha256: space.profileSha256, maxCandidates, runtimeBudgetMs: runtimeBudget } : null, confirm: true };
    const validated = validateTaskMutation(["searches"], body);
    if (!validated || !validateTaskDiscoveryAttempt(newTaskDiscoveryAttempt(`${BASE}/searches`, validated))) { setNotice("Review the bounded query, unique canonical type references, constraints and registered embedding profile. The exact request must fit within 8 KiB."); return; }
    setLive(false); setReview({ title: mode === "lexical" ? "Create a task-bound lexical shortlist" : "Retain a pending native hybrid request", path: `${BASE}/searches`, body: validated }); setNotice("");
  }
  function reviewSelect(candidate: RankedTaskCandidate) {
    if (!detail?.result || !taskCanSelect(detail.search, detail.result, candidate)) return;
    setLive(false); setReview({ title: `Retain selection: ${candidate.name}`, path: `${BASE}/searches/${detail.search.id}/select`, body: { revision: detail.search.revision, digest: detail.search.bindingSha256, candidateId: candidate.id, confirm: true } });
  }
  function inspect(candidate: RankedTaskCandidate) {
    if (!detail?.result) return; const searchId = detail.search.id;
    inspections.current.reset(); const ticket = inspections.current.open(); setInspection(null); setInspectionNotice("Reading this candidate’s current existing owner…");
    void (async () => {
      try {
        const raw = await read(`${BASE}/searches/${searchId}/candidates/${candidate.id}`, ticket); if (!ticket.current() || selectedRef.current !== searchId) return;
        const envelope = projectTaskEnvelope(raw, workspaceId, "candidate", candidate.id, principalId);
        if (!envelope || !("candidate" in envelope) || !sameTaskValue(candidate, envelope.candidate)) throw new Error("The candidate owner or retained evidence changed. Refresh the search before inspecting it.");
        setInspection({ candidate: envelope.candidate, detail: envelope.detail }); setInspectionNotice("");
      } catch (error) { if (ticket.current()) { setDetail(null); setReview(null); setInspectionNotice(error instanceof Error ? error.message : "The existing owner detail is unavailable."); setDetailNotice("The selected owner could not be verified as current. Refresh this search before making a decision."); } }
      finally { ticket.finish(); }
    })();
  }

  function send(attempt: WorkshopAttempt, retry = false) {
    if (!recovered || busyRef.current || !validateTaskDiscoveryAttempt(attempt) || pendingRef.current && (!retry || !sameTaskValue(pendingRef.current, attempt))) return;
    busyRef.current = true; pendingRef.current = attempt; setPending(attempt); setBusy(true); setReview(null); setNotice("Retaining and sending this exact command…");
    const ticket = mutations.current.open(), journalScope = journal.current;
    void (async () => {
      try {
        const persisted = await retainAttempt(journalScope, attempt); if (!ticket.current()) return; setDurable(persisted);
        const response = await fetch(attempt.path, { method: "POST", credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: { ...headers(workspaceId), "Content-Type": "application/json", "Idempotency-Key": attempt.key }, body: attempt.body });
        if (!ticket.current()) return;
        if (!response.ok) {
          const cleared = await clearRejectedAttempt(journalScope, attempt.key, response.status, retry); if (!ticket.current()) return;
          if (cleared) { pendingRef.current = null; setPending(null); }
          if (taskDiscoveryScopeInvalidated(response.status, null, workspaceId, principalId)) { leaveScope(); return; }
          throw new Error(cleared ? "The command was rejected. Refresh the current context or search before reviewing another request." : "The outcome is unresolved. Keep this exact command and explicitly retry it to recover its original receipt.");
        }
        const raw = await response.json(); if (!ticket.current()) return;
        if (taskDiscoveryScopeInvalidated(response.status, raw, workspaceId, principalId)) { leaveScope(); return; }
        const route = taskDiscoveryAttemptRoute(attempt.path)!, receipt = projectTaskMutation(raw, workspaceId, route, parseMcpJSON(attempt.body) as Record<string, unknown>, principalId);
        if (!receipt) throw new Error("The returned receipt could not be verified. Explicitly retry the retained command to recover its original outcome.");
        const id = "search" in receipt ? receipt.search.id : "selection" in receipt ? receipt.selection.searchId : null;
        if (!id) throw new Error("The command receipt has no matching search identity.");
        await clearAttempt(journalScope, attempt.key); if (!ticket.current()) return;
        pendingRef.current = null; setPending(null); setDurable(null); setContext(null); setContextNotice("Review current task context before creating another search.");
        setNotice("The original command receipt was verified. Reading the current search and evidence separately…"); loadHistory(); selectSearch(id);
      } catch (error) { if (ticket.current()) setNotice(error instanceof Error ? error.message : "The command outcome is unresolved. Explicitly retry the exact retained request."); }
      finally { if (ticket.current()) { busyRef.current = false; setBusy(false); } ticket.finish(); }
    })();
  }

  const disabled = busy || !!pending || !recovered;
  const updateConstraints = (value: Partial<TaskDiscoveryConstraints>) => { setConstraints(current => ({ ...current, ...value })); setReview(null); };
  return <>
    {notice && <p className={styles.status} role="status">{notice}</p>}
    {pending && <section className={styles.uncertain} aria-label="Unresolved exact command"><h3>Recover this exact command</h3><p>Its outcome may already exist. Retrying uses the same body and idempotency key; it does not create a new approval or refresh the task context.</p>
      {durable === false && <p>Browser recovery storage is unavailable. Keep this tab open until the receipt is resolved.</p>}<TaskJSON title="Exact retained request" value={pending} /><button disabled={busy || !recovered} onClick={() => send(pending, true)}>Retry exact command</button></section>}
    <section className={styles.panel} aria-label="Task discovery request"><div className={styles.panelHeading}><h2>1. Review the task and constraints</h2><p>Read an existing running task and exact repository state before creating a search.</p></div>
      <div className={styles.body}><fieldset disabled={disabled}><div className={styles.form}>
        <div className={styles.pair}><label className={styles.field}>Task episode UUID<input value={episode} maxLength={36} onChange={event => changeTask(event.target.value, state)} spellCheck={false} /></label><label className={styles.field}>Repository state UUID<input value={state} maxLength={36} onChange={event => changeTask(episode, event.target.value)} spellCheck={false} /></label></div>
        <div><button disabled={contextBusy || !UUID.test(episode) || !UUID.test(state)} onClick={loadContext}>Read current task context</button></div>
        {contextNotice && <p className={styles.status} role="status">{contextNotice}</p>}{context && <TaskContextView context={context} />}
        <label className={styles.field}>Task query (up to 500 characters)<input value={query} maxLength={500} onChange={event => { setQuery(event.target.value); setReview(null); }} placeholder="Find a tool to inspect this repository’s Python code" /></label>
        <details className={styles.details}><summary>Hard constraints</summary><div className={styles.form}>
          <label className={styles.field}>Capability classes (select any required classes)<select multiple size={5} value={constraints.classes} onChange={event => updateConstraints({ classes: Array.from(event.target.selectedOptions, option => option.value) })}>{CAPABILITIES.map(value => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}</select></label>
          <div className={styles.pair}><label className={styles.field}>Supplied input types, one per line<textarea value={inputTypes} maxLength={8192} onChange={event => { setInputTypes(event.target.value); setReview(null); }} placeholder="syn://type/…" /></label><label className={styles.field}>Required output types, one per line<textarea value={outputTypes} maxLength={8192} onChange={event => { setOutputTypes(event.target.value); setReview(null); }} placeholder="synny://type/…" /></label></div>
          <p className={styles.help}>Use registered Syn/Synny type references. Supplied inputs must fit accepted inputs; produced outputs must fit the required outputs. Unknown required evidence cannot prove a match.</p>
          <label className={styles.field}>Platform<select value={constraints.platform ?? ""} onChange={event => updateConstraints({ platform: event.target.value || null })}><option value="">Unspecified</option>{PLATFORMS.map(value => <option key={value}>{value}</option>)}</select></label>
          <label className={styles.check}><input type="checkbox" checked={constraints.permittedEffects !== null} onChange={event => updateConstraints({ permittedEffects: event.target.checked ? [] : null })} />Restrict permitted effects; selecting none explicitly permits no declared effects.</label>
          {constraints.permittedEffects !== null && <label className={styles.field}>Permitted effects<select multiple size={4} value={constraints.permittedEffects} onChange={event => updateConstraints({ permittedEffects: Array.from(event.target.selectedOptions, option => option.value) })}>{EFFECTS.map(value => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}</select></label>}
          <label className={styles.check}><input type="checkbox" checked={constraints.permittedScopes !== null} onChange={event => updateConstraints({ permittedScopes: event.target.checked ? [] : null })} />Restrict canonical scopes; an empty list explicitly permits none.</label>
          {constraints.permittedScopes !== null && <label className={styles.field}>Permitted scopes, one per line<textarea value={scopes} maxLength={8192} onChange={event => { setScopes(event.target.value); setReview(null); }} /></label>}
          <label className={styles.check}><input type="checkbox" checked={constraints.requireLocal} onChange={event => updateConstraints({ requireLocal: event.target.checked })} />Require local capability evidence</label>
          <label className={styles.check}><input type="checkbox" checked={constraints.requireTaskEvidence} onChange={event => updateConstraints({ requireTaskEvidence: event.target.checked })} />Require matching successful evidence for this exact task and state</label>
        </div></details>
        <div className={styles.pair}><label className={styles.field}>Retrieval mode<select value={mode} onChange={event => { setMode(event.target.value as "lexical" | "hybrid"); setReview(null); }}><option value="lexical">Lexical and typed matching</option><option value="hybrid">Optional native hybrid retrieval</option></select></label><label className={styles.field}>Shortlist limit<input type="number" min={1} max={10} value={limit} onChange={event => { setLimit(Number(event.target.value)); setReview(null); }} /></label></div>
        {mode === "hybrid" && <section className={styles.notice}><h3>Explicit native retrieval request</h3><p>This page retains a request. A native operator must explicitly run inference through the existing local embedding and task budget owners. Opening, refreshing or confirming this page never starts inference.</p>
          <label className={styles.field}>Registered embedding space<select value={spaceKey} onChange={event => { setSpaceKey(event.target.value); setReview(null); }}><option value="">Select a reviewed registered profile</option>{context?.embeddingSpaces.map(item => <option key={`${item.spaceId}:${item.embeddingName}`} value={`${item.spaceId}:${item.embeddingName}`}>{item.embeddingName} · {item.model} · {item.spaceId}</option>)}</select></label>
          <p className={styles.help}>A retained registration does not prove that model files are available locally. Privacy, task capability and budget gates remain with the existing native owners.</p>
          <div className={styles.pair}><label className={styles.field}>Maximum candidate documents<input type="number" min={1} max={16} value={maxCandidates} onChange={event => { setMaxCandidates(Number(event.target.value)); setReview(null); }} /></label><label className={styles.field}>Total native runtime budget (ms)<input type="number" min={1} max={120000} value={runtimeBudget} onChange={event => { setRuntimeBudget(Number(event.target.value)); setReview(null); }} /></label></div>
        </section>}
        <div><button className={styles.primary} disabled={!context || contextBusy} onClick={reviewCreate}>Review exact search request</button></div>
      </div></fieldset></div>
    </section>
    {review && <section className={styles.uncertain} aria-label="Confirm exact command"><h3>{review.title}</h3><p>Review the task or search identity and exact request below. Confirmation retains this explicit command. Selection grants no execution authority.</p><pre>{JSON.stringify(review.body, null, 2)}</pre><div className={styles.actions}><button className={styles.primary} disabled={disabled} onClick={() => send(newTaskDiscoveryAttempt(review.path, review.body))}>Confirm exact request</button><button disabled={busy} onClick={() => setReview(null)}>Back to review</button></div></section>}
    <div className={`${styles.layout} ${styles.section}`}>
      <section className={styles.panel} aria-label="Retained searches"><div className={styles.panelHeading}><h2>2. Inspect retained searches</h2><p>History reads do not start new searches or native inference.</p><div className={styles.actions}><button disabled={historyBusy} onClick={() => loadHistory()}>Refresh history</button></div></div>
        <ul className={styles.cards}>{history.items.map(item => <li key={item.id}><button className={selectedId === item.id ? styles.selected : undefined} onClick={() => selectSearch(item.id)}><strong>{item.query}</strong><span className={styles.badges}><span className={styles.badge}>{item.status}</span><span className={styles.badge}>{item.mode}</span></span><small>Task {item.taskBinding.episodeId}<br />Search {item.id}</small></button></li>)}</ul>
        {!history.items.length && <p className={styles.empty}>No retained task searches.</p>}<div className={styles.body}><p className={styles.help} role="status">{historyNotice}</p>{history.nextCursor && <button disabled={historyBusy} onClick={() => loadHistory(true)}>Load more searches</button>}</div>
      </section>
      <section className={styles.panel} aria-label="Selected search"><div className={styles.body}>
        <div className={styles.row}><h2>3. Review evidence and select</h2>{selectedId && <button disabled={detailBusy} onClick={() => { pollCount.current = 0; setPollNotice(""); void loadDetail(selectedId); }}>Refresh search</button>}</div>
        {!selectedId && <p className={styles.muted}>Select an exact search from history to inspect its shortlist and retained source evidence.</p>}{detailNotice && <p className={styles.status} role="status">{detailNotice}</p>}
        {detail && <><h3>{detail.search.query}</h3><div className={styles.badges}><span className={styles.badge}>{detail.search.status}</span><span className={styles.badge}>{detail.search.mode}</span><span className={styles.badge}>revision {detail.search.revision}</span></div>
          <dl className={styles.facts}><dt>Search</dt><dd><code>{detail.search.id}</code></dd><dt>Task episode</dt><dd><code>{detail.search.taskBinding.episodeId}</code></dd><dt>Repository state</dt><dd><code>{detail.search.taskBinding.repositoryStateId}</code></dd><dt>Context digest</dt><dd><code>{detail.search.contextSha256}</code></dd><dt>Current authority</dt><dd>{detail.search.approvalValid && detail.search.authorityCurrent ? "Current and approved for this discovery record" : "Historical, held or no longer approved"}</dd></dl>
          <button disabled={disabled} onClick={() => { changeTask(detail.search.taskBinding.episodeId, detail.search.taskBinding.repositoryStateId); setContextNotice("Task identities copied from retained history. Read current task context above before reviewing a fresh search."); }}>Use these task identities for a fresh context review</button>
          {(detail.search.reasonCode || detail.search.reasonCodes.length > 0) && <ul>{[...new Set([detail.search.reasonCode, ...detail.search.reasonCodes].filter(Boolean))].map(code => <li key={code}><code>{code}</code></li>)}</ul>}
          <TaskJSON title="Exact search, constraints and task binding" value={detail.search} />
          {taskDiscoveryNeedsMonitoring(detail.search) && <section className={styles.notice}><h3>{detail.search.status === "requested" ? "Waiting for an explicit native attempt" : "Native attempt claimed"}</h3><p>Use the documented <code>potbot task-discovery … refine</code> native command with this search’s current revision and binding digest. The host must satisfy the registered model, privacy and budget checks. Browser monitoring only reads retained progress.</p><label className={styles.check}><input type="checkbox" checked={live} onChange={event => setLive(event.target.checked)} />Monitor retained progress while this tab is visible (maximum 30 reads)</label>{pollNotice && <p>{pollNotice}</p>}</section>}
          {detail.search.actions.includes("cancel") && <button disabled={disabled || detailBusy} onClick={() => { setLive(false); setReview({ title: "Cancel this exact pending search", path: `${BASE}/searches/${detail.search.id}/cancel`, body: { revision: detail.search.revision, digest: detail.search.bindingSha256, confirm: true } }); }}>Review cancellation</button>}
          {detail.result && <TaskResultView workspace={workspaceId} search={detail.search} result={detail.result} selection={detail.selection} disabled={disabled || detailBusy} inspect={inspect} select={reviewSelect} />}
          {inspectionNotice && <p className={styles.status} role="status">{inspectionNotice}</p>}{inspection && <section className={styles.section}><h3>Current owner detail: {inspection.candidate.name}</h3><p className={styles.help}>Provider-authored content is displayed as text. This read performs no provider operation.</p><pre>{JSON.stringify(inspection.detail, null, 2)}</pre></section>}
        </>}
      </div></section>
    </div>
  </>;
}

export default function TaskDiscoveryWorkbench({ workspaceId, initialSelection }: { workspaceId: string; initialSelection: InitialSelection }) {
  const [initial, setInitial] = useState<TaskDiscoveryPage | null>(null), [notice, setNotice] = useState("Reading retained task searches…"), [version, setVersion] = useState(0);
  const invalidateScope = useCallback(() => { setInitial(null); setNotice("The authenticated workspace scope changed. Reading its current retained searches…"); setVersion(value => value + 1); }, []);
  useEffect(() => {
    const request = new AbortController(); setInitial(null);
    void fetch(`${BASE}/searches?limit=25`, { credentials: "same-origin", cache: "no-store", signal: request.signal, headers: headers(workspaceId) }).then(async response => {
      if (!response.ok) throw new Error("unavailable"); const page = projectTaskSearches(await response.json(), workspaceId); if (request.signal.aborted) return;
      if (!page) throw new Error("invalid"); setInitial(page); setNotice("");
    }).catch(() => { if (!request.signal.aborted) setNotice("Task discovery is unavailable in this workspace. Check operator authentication and refresh."); });
    return () => request.abort();
  }, [workspaceId, version]);
  return initial ? <WorkspaceTaskDiscovery key={`${workspaceId}:${initial.principalId}:${version}`} initial={initial} initialSelection={initialSelection} invalidateScope={invalidateScope} /> : <section className={styles.notice}><p role="status">{notice}</p><button onClick={() => setVersion(value => value + 1)}>Refresh task discovery</button></section>;
}
