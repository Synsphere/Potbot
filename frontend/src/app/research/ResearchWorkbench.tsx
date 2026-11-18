"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { UUID, parseMcpJSON, sameTaskValue, projectResearchContext, projectResearchCitationEnvelope, projectResearchComparisonEnvelope, projectResearchNonRunMutation, projectResearchClaims, validateResearchMutation, citationSpec, type NeedSpec, type ResearchContext, type ResearchActionReceipt, type ResearchCitation, type ResearchClaimSpec, type ResearchComparisonEnvelope } from "../../lib/research.ts";
import { projectResearchRuns, projectResearchRunMutation, projectResearchResult, projectResearchEvents, projectResearchSource, researchExecutionMatches, researchNeedsMonitoring, type ResearchRuns, type ResearchResult, type ResearchSource, type ResearchSourceEnvelope, type ResearchEvents } from "../../lib/research-execution.ts";
import { RESEARCH_ENDPOINT as BASE, RESEARCH_MAX_POLLS, researchRequestHeaders, researchScopeInvalidated, researchJournalScope, researchAttemptRoute, newResearchAttempt, validateResearchAttempt, researchCanQueue, researchActionMatchesContext, researchCitationRequest, researchCitationMatchesSource, researchCitationFitsContext, type ResearchSelection } from "../../lib/research-client.ts";
import { researchQuote, researchUTF8, researchTextSha256 } from "../../lib/research-text.ts";
import { WorkshopRequests, retainAttempt, recoverAttempt, clearAttempt, clearRejectedAttempt, type WorkshopAttempt, type RequestTicket } from "../../lib/skill-workshop-client.ts";
import { ResearchJSON, ResearchReasons, ResearchLink, ResearchContextView, ResearchExecutionView, ResearchResultView, ResearchCitationView, ResearchComparisonView } from "./ResearchViews.tsx";
import styles from "../skills/skills.module.css";

type Review = { title: string; explanation: string; path: string; body: Record<string, unknown> };
const headers = (workspace: string) => ({ "X-Potbot-Workspace": workspace });
const defaultNeed: NeedSpec = { schema_version: 1, question: "", pending_decision: "", alternatives: [], required_evidence: [], resolution_criteria: [], scope: "", known_evidence: [], priority: 50, budget: {}, query_profiles: [] };
const lines = (value: string) => value.split("\n").map(item => item.trim()).filter(Boolean);

function WorkspaceResearch({ initial, initialSelection, invalidateScope }: { initial: ResearchRuns; initialSelection: ResearchSelection; invalidateScope: () => void }) {
  const { scope } = initial, { workspaceId, principalId } = scope;
  const [history, setHistory] = useState(initial), [historyBusy, setHistoryBusy] = useState(false), [historyNotice, setHistoryNotice] = useState("");
  const [episode, setEpisode] = useState(initialSelection.episode ?? ""), [state, setState] = useState(initialSelection.state ?? ""), [needId, setNeedId] = useState(initialSelection.need ?? "");
  const [context, setContext] = useState<ResearchContext | null>(null), [contextBusy, setContextBusy] = useState(false), [contextNotice, setContextNotice] = useState("");
  const [need, setNeed] = useState<NeedSpec>(structuredClone(defaultNeed)), [budgetJSON, setBudgetJSON] = useState("{}"), [evidenceIds, setEvidenceIds] = useState(""), [profiles, setProfiles] = useState("");
  const [queryJSON, setQueryJSON] = useState(""), [action, setAction] = useState<ResearchActionReceipt | null>(null), [disclosure, setDisclosure] = useState(false);
  const [selectedId, setSelectedId] = useState(initialSelection.run), [detail, setDetail] = useState<ResearchResult | null>(null), [detailBusy, setDetailBusy] = useState(false), [detailNotice, setDetailNotice] = useState("");
  const [events, setEvents] = useState<ResearchEvents | null>(null), [eventBusy, setEventBusy] = useState(false), [eventNotice, setEventNotice] = useState("");
  const [source, setSource] = useState<ResearchSourceEnvelope | null>(null), [sourceBusy, setSourceBusy] = useState(false), [sourceNotice, setSourceNotice] = useState(""), [startByte, setStartByte] = useState(0), [endByte, setEndByte] = useState(0), [citation, setCitation] = useState<ResearchCitation | null>(null), [citationBusy, setCitationBusy] = useState(false);
  const [claims, setClaims] = useState<ResearchClaimSpec[]>([]), [claimKey, setClaimKey] = useState("claim-1"), [claimLabel, setClaimLabel] = useState("");
  const [comparisonId, setComparisonId] = useState(""), [comparison, setComparison] = useState<ResearchComparisonEnvelope | null>(null), [comparisonBusy, setComparisonBusy] = useState(false), [comparisonNotice, setComparisonNotice] = useState("");
  const [review, setReview] = useState<Review | null>(null), [pending, setPending] = useState<WorkshopAttempt | null>(null), [recovered, setRecovered] = useState(false), [busy, setBusy] = useState(false), [durable, setDurable] = useState<boolean | null>(null), [notice, setNotice] = useState(initialSelection.invalid ? "The URL contained ambiguous or foreign workspace selectors. Enter exact identities below." : "");
  const [live, setLive] = useState(false), [visibility, setVisibility] = useState(0), [pollNotice, setPollNotice] = useState("");
  const lists = useRef(new WorkshopRequests()), contexts = useRef(new WorkshopRequests()), details = useRef(new WorkshopRequests()), eventReads = useRef(new WorkshopRequests()), sources = useRef(new WorkshopRequests()), citations = useRef(new WorkshopRequests()), comparisons = useRef(new WorkshopRequests()), mutations = useRef(new WorkshopRequests());
  const pageRef = useRef(initial), selectedRef = useRef(initialSelection.run), contextRef = useRef<ResearchContext | null>(null), sourceRef = useRef<ResearchSourceEnvelope | null>(null), pendingRef = useRef<WorkshopAttempt | null>(null), busyRef = useRef(false), journal = useRef(""), pollCount = useRef(0);
  const abortReads = useCallback(() => { for (const owner of [lists, contexts, details, eventReads, sources, citations, comparisons]) owner.current.reset(); }, []);
  const leaveScope = useCallback(() => { abortReads(); mutations.current.reset(); invalidateScope(); }, [abortReads, invalidateScope]);
  useEffect(() => {
    journal.current = researchJournalScope(workspaceId, principalId); const ticket = mutations.current.open();
    void recoverAttempt(journal.current, validateResearchAttempt).then(value => { if (ticket.current()) { pendingRef.current = value; setPending(value); setRecovered(true); } ticket.finish(); });
    const changed = () => setVisibility(value => value + 1); document.addEventListener("visibilitychange", changed);
    return () => { document.removeEventListener("visibilitychange", changed); abortReads(); mutations.current.reset(); };
  }, [workspaceId, principalId, abortReads]);

  const read = useCallback(async (path: string, ticket: RequestTicket): Promise<unknown> => {
    const response = await fetch(path, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: researchRequestHeaders(scope) });
    if (!ticket.current()) return null;
    if (researchScopeInvalidated(response.status, null, scope)) { leaveScope(); return null; }
    if (!response.ok) throw new Error(response.status === 409 ? "This context or evidence no longer matches its owner. Read the current task or retained run again." : "This retained research record is unavailable.");
    const raw = await response.json(); if (!ticket.current()) return null;
    if (researchScopeInvalidated(response.status, raw, scope)) { leaveScope(); return null; }
    return raw;
  }, [workspaceId, scope, leaveScope]);

  const loadHistory = useCallback((append = false) => {
    const previous = pageRef.current, cursor = append ? previous.nextCursor : null; if (append && !cursor) return;
    lists.current.reset(); const ticket = lists.current.open(); setHistoryBusy(true); setHistoryNotice("Reading shared research history…");
    void (async () => {
      try {
        const raw = await read(`${BASE}/runs?${new URLSearchParams({ limit: "25", ...(cursor ? { after: cursor } : {}) })}`, ticket); if (!ticket.current()) return;
        const page = projectResearchRuns(raw, workspaceId, undefined, principalId); if (!page || cursor && page.nextCursor === cursor || append && page.items.some(item => previous.items.some(prior => prior.run.id === item.run.id))) throw new Error("The history cursor did not advance safely.");
        const items = append ? [...previous.items, ...page.items] : page.items, next = { ...page, items: items.slice(-200) }; pageRef.current = next; setHistory(next);
        setHistoryNotice(items.length > 200 ? "Showing the last 200 loaded runs. Refresh to return to the beginning." : page.nextCursor ? "More retained runs are available." : "This bounded history page has no further cursor.");
      } catch (error) { if (ticket.current()) setHistoryNotice(error instanceof Error ? error.message : "Research history is unavailable."); }
      finally { if (ticket.current()) setHistoryBusy(false); ticket.finish(); }
    })();
  }, [read, workspaceId, principalId]);

  const loadEvents = useCallback((id: string, after = 0, previous: ResearchEvents | null = null) => {
    eventReads.current.reset(); const ticket = eventReads.current.open(); setEventBusy(true); setEventNotice("Reading retained progress events…");
    void (async () => {
      try {
        const raw = await read(`${BASE}/runs/${id}/events?after=${after}&limit=100`, ticket); if (!ticket.current() || selectedRef.current !== id) return;
        const page = projectResearchEvents(raw, workspaceId, id, after, principalId); if (!page || previous && !researchExecutionMatches(previous.execution, page.execution)) throw new Error("The event page does not match this exact research run.");
        const items = previous ? [...previous.items, ...page.items] : page.items;
        setEvents({ ...page, items: items.slice(-500) }); setEventNotice(items.length > 500 ? "Showing the latest 500 loaded events." : page.items.length ? "Retained request and transformation events loaded." : "No newer events were returned.");
      } catch (error) { if (ticket.current()) setEventNotice(error instanceof Error ? error.message : "Progress events are unavailable."); }
      finally { if (ticket.current()) setEventBusy(false); ticket.finish(); }
    })();
  }, [read, workspaceId, principalId]);

  const loadRun = useCallback(async (id: string) => {
    details.current.reset(); const ticket = details.current.open(); setDetailBusy(true); setDetail(null); setReview(null); setDetailNotice("Reading the exact run and retained result evidence…");
    try {
      const raw = await read(`${BASE}/runs/${id}/result`, ticket); if (!ticket.current() || selectedRef.current !== id) return;
      const result = projectResearchResult(raw, workspaceId, id, principalId); if (!result) throw new Error("The returned result does not match this exact run and its evidence identities.");
      setDetail(result); setDetailNotice("");
    } catch (error) { if (ticket.current() && selectedRef.current === id) setDetailNotice(error instanceof Error ? error.message : "This run could not be refreshed."); }
    finally { if (ticket.current()) setDetailBusy(false); ticket.finish(); }
  }, [read, workspaceId, principalId]);

  const selectRun = useCallback((id: string) => {
    if (!UUID.test(id)) return; selectedRef.current = id; setSelectedId(id); setLive(false); setPollNotice(""); pollCount.current = 0; sources.current.reset(); citations.current.reset(); sourceRef.current = null; setSource(null); setSourceNotice(""); setSourceBusy(false); setCitation(null); setCitationBusy(false); setEvents(null);
    const url = new URL(window.location.href); url.searchParams.set("workspace", workspaceId); url.searchParams.set("run", id); window.history.replaceState(null, "", url);
    void loadRun(id); loadEvents(id);
  }, [workspaceId, loadRun, loadEvents]);
  useEffect(() => { if (initialSelection.run) selectRun(initialSelection.run); }, [initialSelection.run, selectRun]);
  useEffect(() => {
    if (!detail || !live || !researchNeedsMonitoring(detail.execution) || document.visibilityState === "hidden") return;
    if (pollCount.current >= RESEARCH_MAX_POLLS) { setPollNotice("Automatic reads paused after 30 checks. Refresh explicitly to continue monitoring."); return; }
    const id = detail.execution.run.id, timer = window.setTimeout(() => { pollCount.current++; void loadRun(id); loadEvents(id); }, 5000);
    return () => window.clearTimeout(timer);
  }, [detail, live, visibility, loadRun, loadEvents]);

  function changeContext(nextEpisode: string, nextState: string, nextNeed: string) {
    contexts.current.reset(); contextRef.current = null; setEpisode(nextEpisode); setState(nextState); setNeedId(nextNeed); setContext(null); setContextBusy(false); setContextNotice(""); setAction(null); setDisclosure(false); setReview(null); setQueryJSON(""); setClaims([]);
  }
  const loadContext = useCallback((exactEpisode: string, exactState: string, exactNeed: string) => {
    if (!UUID.test(exactEpisode) || !UUID.test(exactState) || exactNeed && !UUID.test(exactNeed)) { setContextNotice("Enter exact task, repository state and optional need UUIDs."); return; }
    contexts.current.reset(); const ticket = contexts.current.open(); contextRef.current = null; setContext(null); setContextBusy(true); setReview(null); setDisclosure(false); setContextNotice("Reading the task, need revision and configured source policy…");
    void (async () => {
      try {
        const params = new URLSearchParams({ episodeId: exactEpisode, repositoryStateId: exactState, ...(exactNeed ? { needId: exactNeed } : {}) });
        const raw = await read(`${BASE}/context?${params}`, ticket); if (!ticket.current()) return;
        const value = projectResearchContext(raw, workspaceId, exactEpisode, exactState, exactNeed || null, principalId); if (!value) throw new Error("The returned context does not match these exact task, state and need identities.");
        contextRef.current = value; setContext(value); setQueryJSON(value.queryTemplate ? JSON.stringify(value.queryTemplate, null, 2) : ""); setContextNotice("Current context loaded. Review the need and source policy before admitting an action.");
        const url = new URL(window.location.href); url.searchParams.set("workspace", workspaceId); url.searchParams.set("episode", exactEpisode); url.searchParams.set("state", exactState); if (exactNeed) url.searchParams.set("need", exactNeed); else url.searchParams.delete("need"); window.history.replaceState(null, "", url);
      } catch (error) { if (ticket.current()) setContextNotice(error instanceof Error ? error.message : "Research context is unavailable."); }
      finally { if (ticket.current()) setContextBusy(false); ticket.finish(); }
    })();
  }, [read, workspaceId, principalId]);

  function reviewMutation(title: string, explanation: string, path: string, value: unknown) {
    const route = researchAttemptRoute(path), body = route && validateResearchMutation(route, value);
    if (!body || !validateResearchAttempt(newResearchAttempt(path, body))) { setNotice("The exact request is invalid or exceeds its byte allowance. Review every required field and bounded evidence list."); return; }
    setLive(false); setReview({ title, explanation, path, body }); setNotice("");
  }
  function reviewNeed() {
    if (context?.task.status !== "running") return;
    try { reviewMutation("Create this information need", "This retains the question, pending decision and exact resolution criteria in the shared workspace. It sends no external request.", `${BASE}/needs`, { episodeId: episode, repositoryStateId: state, spec: { ...need, alternatives: need.alternatives.filter(value => value.trim()), required_evidence: need.required_evidence.filter(value => value.trim()), resolution_criteria: need.resolution_criteria.filter(value => value.trim()), budget: parseMcpJSON(budgetJSON), known_evidence: lines(evidenceIds), query_profiles: lines(profiles) }, confirm: true }); }
    catch { setNotice("Need budget must be unambiguous JSON with named nonnegative safe integer limits."); }
  }
  function reviewAction() {
    if (!context?.current || !context.need || !context.policy) return;
    try {
      const query = parseMcpJSON(queryJSON) as Record<string, unknown>;
      if (query.target_version !== context.policy.sha256 || query.max_results !== context.policy.content.max_results || query.max_source_bytes !== context.policy.content.max_source_bytes) throw new Error("Keep the reviewed policy fingerprint and its exact result and source-byte ceilings.");
      reviewMutation("Admit this exact research action", "Action admission retains an inert plan. The separate run confirmation authorizes disclosure and reserves the configured resources.", `${BASE}/actions`, { episodeId: episode, repositoryStateId: state, needId: context.need.id, contextSha256: context.contextSha256, query, confirm: true });
    } catch (error) { setNotice(error instanceof Error ? error.message : "The action must contain the exact bounded research query fields."); }
  }
  function reviewRun() {
    if (!context || !action || !disclosure || !researchCanQueue(action.action, context)) return;
    reviewMutation("Authorize disclosure and queue this run", "Confirmation allows the admitted search text and required identifiers to leave this workspace through the listed providers, and reserves the configured request, byte, runtime and monetary ceilings. The native research worker owns each send and its policy checks.", `${BASE}/runs`, { actionId: action.action.action_id, policySha256: context.policy!.sha256, disclosureConfirmed: true, confirm: true });
  }

  function inspectSource(selected: ResearchSource) {
    if (!detail?.batch || detail.verification !== "verified" || !detail.batch.documents.some(item => sameTaskValue(item, selected))) return;
    setLive(false); sources.current.reset(); citations.current.reset(); const ticket = sources.current.open(), expected = detail.execution; sourceRef.current = null; setSource(null); setSourceBusy(true); setCitation(null); setSourceNotice("Verifying the retained raw and extracted source artifacts…");
    void (async () => {
      try {
        const raw = await read(`${BASE}/runs/${expected.run.id}/sources/${selected.id}`, ticket); if (!ticket.current() || selectedRef.current !== expected.run.id) return;
        const value = projectResearchSource(raw, workspaceId, expected.run.id, selected.id, principalId);
        if (!value || !researchExecutionMatches(expected, value.execution) || !sameTaskValue(value.source, selected) || await researchTextSha256(value.extraction.text) !== value.extraction.textSha256) throw new Error("The source text or retained source chain does not match the selected result.");
        if (!ticket.current()) return; sourceRef.current = value; setSource(value); setStartByte(0); setEndByte(0); setSourceNotice("Exact extracted bytes loaded. Select text or enter UTF-8 byte offsets, then explicitly verify the quote.");
      } catch (error) { if (ticket.current()) setSourceNotice(error instanceof Error ? error.message : "This retained source is unavailable."); }
      finally { if (ticket.current()) setSourceBusy(false); ticket.finish(); }
    })();
  }
  function changeRange(start: number, end: number) { citations.current.reset(); setStartByte(start); setEndByte(end); setCitation(null); setCitationBusy(false); }
  function verifyQuote() {
    const selected = sourceRef.current; if (!selected) return;
    citations.current.reset(); const ticket = citations.current.open(); setCitation(null); setCitationBusy(true); setSourceNotice("Verifying this exact citation against retained evidence…");
    void (async () => {
      try {
        const spec = await researchCitationRequest(selected.execution.run.id, selected.source.id, selected.extraction, startByte, endByte); if (!ticket.current()) return;
        const response = await fetch(`${BASE}/citations`, { method: "POST", credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: { ...researchRequestHeaders(scope), "Content-Type": "application/json" }, body: JSON.stringify(spec) });
        if (!ticket.current()) return;
        if (researchScopeInvalidated(response.status, null, scope)) { leaveScope(); return; }
        if (!response.ok) throw new Error("Citation evidence is unavailable or the exact source changed. Reload the source before another verification.");
        const raw = await response.json(); if (!ticket.current()) return; if (researchScopeInvalidated(response.status, raw, scope)) { leaveScope(); return; }
        const receipt = projectResearchCitationEnvelope(raw, workspaceId, spec, principalId);
        if (!receipt || !await researchCitationMatchesSource(receipt.citation, spec, selected.extraction) || receipt.citation.receiptSha256 !== selected.execution.run.receiptSha256 || receipt.citation.requestedUrl !== selected.source.requestedUrl || receipt.citation.resolvedUrl !== selected.source.resolvedUrl || receipt.citation.retrievedAt !== selected.source.retrievedAt || !sameTaskValue(receipt.citation.researchScope, selected.execution.action?.scope)) throw new Error("The verified citation does not join the selected source, run and task scope.");
        if (!ticket.current() || sourceRef.current !== selected) return; setCitation(receipt.citation); setSourceNotice("The quote was verified against retained source bytes. No external request was made.");
      } catch (error) { if (ticket.current()) setSourceNotice(error instanceof Error ? error.message : "The citation could not be verified."); }
      finally { if (ticket.current()) setCitationBusy(false); ticket.finish(); }
    })();
  }
  function addCitation() {
    if (!citation || !context || !researchCitationFitsContext(citation, context)) { setNotice("Read the citation’s current task and need context before attaching it to a comparison."); return; }
    const prior = claims.find(item => item.key === claimKey);
    if (prior && prior.label !== claimLabel) { setNotice("Use the existing label for this claim key, or choose a distinct key."); return; }
    const next = prior ? claims.map(item => item.key === claimKey ? { ...item, citations: [...item.citations, citationSpec(citation)] } : item) : [...claims, { key: claimKey, label: claimLabel, citations: [citationSpec(citation)] }];
    const checked = projectResearchClaims(next); if (!checked) { setNotice("Use a distinct bounded claim key, a nonempty label and unique citation spans. A comparison supports at most 16 claims and 32 citations."); return; }
    setClaims(checked); setReview(null); setNotice("Citation added to the draft comparison. Add comparable quotes from other retained sources before reviewing it.");
  }
  function readComparison() {
    if (!UUID.test(comparisonId)) return; const id = comparisonId; comparisons.current.reset(); const ticket = comparisons.current.open(); setComparison(null); setComparisonBusy(true); setComparisonNotice("Reading and revalidating the historical comparison artifact…");
    void (async () => {
      try { const raw = await read(`${BASE}/comparisons/${id}`, ticket); if (!ticket.current()) return; const value = projectResearchComparisonEnvelope(raw, workspaceId, id, principalId); if (!value) throw new Error("The comparison identity or retained source evidence could not be verified."); setComparison(value); setComparisonNotice(""); }
      catch (error) { if (ticket.current()) setComparisonNotice(error instanceof Error ? error.message : "The historical comparison is unavailable."); }
      finally { if (ticket.current()) setComparisonBusy(false); ticket.finish(); }
    })();
  }

  function send(attempt: WorkshopAttempt, retry = false) {
    if (!recovered || busyRef.current || !validateResearchAttempt(attempt) || pendingRef.current && (!retry || !sameTaskValue(pendingRef.current, attempt))) return;
    busyRef.current = true; pendingRef.current = attempt; setPending(attempt); setBusy(true); setReview(null); setNotice("Retaining and sending the exact reviewed command…");
    const ticket = mutations.current.open(), journalScope = journal.current;
    void (async () => {
      try {
        const persisted = await retainAttempt(journalScope, attempt); if (!ticket.current()) return; setDurable(persisted);
        const response = await fetch(attempt.path, { method: "POST", credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: { ...researchRequestHeaders(scope), "Content-Type": "application/json", "Idempotency-Key": attempt.key }, body: attempt.body });
        if (!ticket.current()) return;
        if (!response.ok) {
          const cleared = await clearRejectedAttempt(journalScope, attempt.key, response.status, retry); if (!ticket.current()) return;
          if (cleared) { pendingRef.current = null; setPending(null); }
          if (researchScopeInvalidated(response.status, null, scope)) { leaveScope(); return; }
          throw new Error(cleared ? "The command was rejected. Read current context before reviewing a new request." : "The outcome remains unresolved. Explicitly retry the exact retained body and key to recover its original receipt.");
        }
        const raw = await response.json(); if (!ticket.current()) return; if (researchScopeInvalidated(response.status, raw, scope)) { leaveScope(); return; }
        const route = researchAttemptRoute(attempt.path)!, body = parseMcpJSON(attempt.body) as Record<string, unknown>;
        const receipt = route[0] === "runs" ? projectResearchRunMutation(raw, workspaceId, route, body, principalId) : projectResearchNonRunMutation(raw, workspaceId, route, body, principalId);
        if (!receipt) throw new Error("The returned receipt does not match this exact command. Explicitly retry the retained request to recover its original outcome.");
        await clearAttempt(journalScope, attempt.key); if (!ticket.current()) return;
        pendingRef.current = null; setPending(null); setDurable(null); setNotice("The original command receipt was verified.");
        if ("need" in receipt) { setEpisode(body.episodeId as string); setState(body.repositoryStateId as string); setNeedId(receipt.need.id); setAction(null); setClaims([]); loadContext(body.episodeId as string, body.repositoryStateId as string, receipt.need.id); }
        else if ("action" in receipt) { setAction(receipt); setDisclosure(false); loadContext(receipt.action.scope.task_id, receipt.action.scope.repository_state_id, receipt.action.scope.need_id); }
        else if ("execution" in receipt) { setDisclosure(false); loadHistory(); selectRun(receipt.execution.run.id); }
        else { setComparison(receipt); setComparisonId(receipt.evidenceId); setComparisonNotice("The comparison is retained. The information need remains unchanged."); }
      } catch (error) { if (ticket.current()) setNotice(error instanceof Error ? error.message : "The command outcome is unresolved. Preserve and retry its exact body and key."); }
      finally { if (ticket.current()) { busyRef.current = false; setBusy(false); } ticket.finish(); }
    })();
  }

  const disabled = busy || !!pending || !recovered, quote = source ? researchQuote(source.extraction.text, startByte, endByte) : null;
  const patchNeed = (patch: Partial<NeedSpec>) => { setNeed(current => ({ ...current, ...patch })); setReview(null); };
  return <>
    {notice && <p className={styles.status} role="status">{notice}</p>}
    {pending && <section className={styles.uncertain} aria-label="Unresolved research command"><h3>Recover this exact command</h3><p>The original outcome may already exist. Retrying keeps the same request and key, including its original task, policy and disclosure decision.</p>{durable === false && <p>Browser recovery storage is unavailable. Keep this tab open until the receipt is resolved.</p>}<ResearchJSON title="Exact retained command" value={pending} /><button disabled={busy || !recovered} onClick={() => send(pending, true)}>Retry exact command</button></section>}
    <section className={styles.panel} aria-label="Research task and information need"><div className={styles.panelHeading}><h2>1. Review the task and information need</h2><p>Read an exact task and repository state. Questions, queries and retained evidence are shared within this workspace.</p></div><div className={styles.body}><fieldset disabled={disabled}><div className={styles.form}>
      <div className={styles.pair}><label className={styles.field}>Task episode UUID<input value={episode} maxLength={36} spellCheck={false} onChange={event => changeContext(event.target.value, state, needId)} /></label><label className={styles.field}>Repository state UUID<input value={state} maxLength={36} spellCheck={false} onChange={event => changeContext(episode, event.target.value, needId)} /></label></div>
      <label className={styles.field}>Information need UUID (optional to list or create a need)<input value={needId} maxLength={36} spellCheck={false} onChange={event => changeContext(episode, state, event.target.value)} /></label>
      <div><button disabled={contextBusy || !UUID.test(episode) || !UUID.test(state) || !!needId && !UUID.test(needId)} onClick={() => loadContext(episode, state, needId)}>Read current context</button></div>
      {contextNotice && <p className={styles.status} role="status">{contextNotice}</p>}
      {context && <><ResearchContextView context={context} /><label className={styles.field}>Select a retained need (up to 25 listed)<select value={context.need?.id ?? ""} onChange={event => { setNeedId(event.target.value); setAction(null); setClaims([]); loadContext(episode, state, event.target.value); }}><option value="">No selected need</option>{context.needs.map(item => <option key={item.id} value={item.id}>{item.status} · {item.question}</option>)}</select></label></>}
      <details className={styles.details}><summary>Create a new information need</summary><div className={styles.form}>
        <label className={styles.field}>Question<textarea value={need.question} maxLength={8000} onChange={event => patchNeed({ question: event.target.value })} /></label><label className={styles.field}>Pending decision<textarea value={need.pending_decision} maxLength={8000} onChange={event => patchNeed({ pending_decision: event.target.value })} /></label>
        <div className={styles.pair}><label className={styles.field}>Required evidence, one criterion per line<textarea value={need.required_evidence.join("\n")} onChange={event => patchNeed({ required_evidence: event.target.value.split("\n") })} maxLength={65536} /></label><label className={styles.field}>Resolution criteria, one per line<textarea value={need.resolution_criteria.join("\n")} onChange={event => patchNeed({ resolution_criteria: event.target.value.split("\n") })} maxLength={65536} /></label></div>
        <label className={styles.field}>Research scope<textarea value={need.scope} maxLength={8000} onChange={event => patchNeed({ scope: event.target.value })} /></label><label className={styles.field}>Alternatives, one per line<textarea value={need.alternatives.join("\n")} onChange={event => patchNeed({ alternatives: event.target.value ? event.target.value.split("\n") : [] })} maxLength={65536} /></label>
        <div className={styles.pair}><label className={styles.field}>Priority (0–100)<input type="number" min={0} max={100} value={need.priority} onChange={event => patchNeed({ priority: Number(event.target.value) })} /></label><label className={styles.field}>Optional need budget (JSON)<textarea value={budgetJSON} maxLength={16384} spellCheck={false} onChange={event => { setBudgetJSON(event.target.value); setReview(null); }} /></label></div>
        <details className={styles.details}><summary>Existing evidence and retrieval profile references</summary><div className={styles.pair}><label className={styles.field}>Known evidence UUIDs, one per line<textarea value={evidenceIds} maxLength={8192} onChange={event => { setEvidenceIds(event.target.value); setReview(null); }} /></label><label className={styles.field}>Registered query profiles, one per line<textarea value={profiles} maxLength={8192} onChange={event => { setProfiles(event.target.value); setReview(null); }} /></label></div></details>
        <div><button disabled={context?.task.status !== "running" || contextBusy} onClick={reviewNeed}>Review new information need</button></div>
      </div></details>
    </div></fieldset></div></section>

    <section className={`${styles.panel} ${styles.section}`} aria-label="Research action and disclosure"><div className={styles.panelHeading}><h2>2. Review the action, then authorize disclosure</h2><p>The first confirmation admits an inert action. The second queues native research under its exact policy and budget.</p></div><div className={styles.body}><fieldset disabled={disabled}>
      <label className={styles.field}>Exact query plan<textarea value={queryJSON} maxLength={131072} rows={18} spellCheck={false} onChange={event => { setQueryJSON(event.target.value); setReview(null); setDisclosure(false); setAction(null); }} placeholder="Read a current information need and configured policy to obtain its query template." /></label><p className={styles.help}>Review literal search text, required identifiers, expected observation, decision effect, evidence requirement, stopping condition, freshness and fallback description. The policy fixes supported providers, destination hosts and request ceilings.</p>
      <button disabled={!context?.current || !context.need || !context.policy || !queryJSON || contextBusy} onClick={reviewAction}>Review action admission</button>
      {action && <section className={styles.notice}><h3>Admitted action: {action.reused ? "retained existing identity" : "new identity"}</h3><code>{action.action.action_id}</code><p>This original retained query is the exact plan that queue confirmation will authorize:</p><pre>{JSON.stringify(action.action.query, null, 2)}</pre><ResearchJSON title="Exact admitted action and task binding" value={action.action} />{context && !researchActionMatchesContext(action.action, context) && <p className={styles.warning}>This action does not match the currently reviewed task, need revision and policy.</p>}
        <label className={styles.check}><input type="checkbox" checked={disclosure} onChange={event => { setDisclosure(event.target.checked); setReview(null); }} />I reviewed the exact admitted query and configured provider endpoints, approve disclosure of this query outside the workspace, and accept the displayed resource ceilings.</label><button className={styles.primary} disabled={!context || !disclosure || !researchCanQueue(action.action, context) || contextBusy} onClick={reviewRun}>Review disclosure and queue request</button>
      </section>}
    </fieldset></div></section>

    {review && <section className={styles.uncertain} aria-label="Confirm exact research command"><h3>{review.title}</h3><p>{review.explanation}</p><pre>{JSON.stringify(review.body, null, 2)}</pre><div className={styles.actions}><button className={styles.primary} disabled={disabled} onClick={() => send(newResearchAttempt(review.path, review.body))}>Confirm exact request</button><button disabled={busy} onClick={() => setReview(null)}>Back to review</button></div></section>}

    <div className={`${styles.layout} ${styles.section}`}><section className={styles.panel} aria-label="Shared research history"><div className={styles.panelHeading}><h2>3. Inspect retained runs</h2><p>History reads never authorize a provider request.</p><div className={styles.actions}><button disabled={historyBusy} onClick={() => loadHistory()}>Refresh history</button></div></div><ul className={styles.cards}>{history.items.map(item => <li key={item.run.id}><button className={selectedId === item.run.id ? styles.selected : undefined} onClick={() => selectRun(item.run.id)}><strong>{item.action?.query.search_text ?? "Research record with unavailable action"}</strong><span className={styles.badges}><span className={styles.badge}>{item.run.state}</span><span className={styles.badge}>{item.run.usageOrigin ?? "unsettled"}</span></span><small>Run {item.run.id}<br />Task {item.run.episodeId}</small></button></li>)}</ul>{!history.items.length && <p className={styles.empty}>No retained external research runs.</p>}<div className={styles.body}><p className={styles.help} role="status">{historyNotice}</p>{history.nextCursor && <button disabled={historyBusy} onClick={() => loadHistory(true)}>Load more runs</button>}<label className={styles.field}>Resume exact run UUID<input placeholder="Paste a retained run UUID" maxLength={36} spellCheck={false} onChange={event => { if (UUID.test(event.target.value)) selectRun(event.target.value); }} /></label></div></section>
      <section className={styles.panel} aria-label="Selected research run"><div className={styles.body}><div className={styles.row}><h2>Run and source evidence</h2>{selectedId && <button disabled={detailBusy} onClick={() => { pollCount.current = 0; setPollNotice(""); void loadRun(selectedId); }}>Refresh run</button>}</div>{!selectedId && <p className={styles.muted}>Select or resume a retained run to inspect its owned progress, results and sources.</p>}{detailNotice && <p className={styles.status} role="status">{detailNotice}</p>}
        {detail && <><ResearchExecutionView execution={detail.execution} />{detail.execution.action && <button disabled={disabled} onClick={() => { const scope = detail.execution.action!.scope; changeContext(scope.task_id, scope.repository_state_id, scope.need_id); setContextNotice("Exact task and need identities copied. Read current context before another decision."); }}>Use these task and need identities</button>}
          {researchNeedsMonitoring(detail.execution) && <section className={styles.notice}><p>The native research worker owns execution. A configured native operator can process an admitted run with <code>potbot research … worker-once --worker-id WORKER_NAME</code>. This page only reads retained progress until you explicitly request a change.</p><label className={styles.check}><input type="checkbox" checked={live} onChange={event => setLive(event.target.checked)} />Monitor progress while this tab is visible (up to 30 reads)</label>{pollNotice && <p>{pollNotice}</p>}<button disabled={disabled || detailBusy || detail.execution.run.cancelRequested} onClick={() => reviewMutation("Cancel this research run", "Cancellation blocks later sends. It cannot retract a request already sent; the run retains any known or uncertain usage.", `${BASE}/runs/${detail.execution.run.id}/cancel`, { confirm: true })}>Review cancellation</button></section>}
          <ResearchResultView result={detail} inspect={inspectSource} disabled={sourceBusy || detailBusy} />
        </>}
        {selectedId && <details className={styles.details}><summary>Retained progress and causal events</summary><p className={styles.help} role="status">{eventNotice}</p>{events && <ResearchJSON title={`${events.items.length} retained events`} value={events.items} />}<button disabled={eventBusy} onClick={() => { setLive(false); loadEvents(selectedId, events?.nextCursor ?? events?.items.at(-1)?.sequence ?? 0, events); }}>Read newer events</button></details>}
      </div></section></div>

    <section className={`${styles.panel} ${styles.section}`} aria-label="Source citation selection"><div className={styles.panelHeading}><h2>4. Select and verify exact source quotes</h2><p>Source content is displayed as inert text. Byte offsets are UTF-8, start inclusive and end exclusive.</p></div><div className={styles.body}>{sourceNotice && <p className={styles.status} role="status">{sourceNotice}</p>}{!source && <p className={styles.help}>Inspect a fetched document above to load its complete bounded extracted source.</p>}{source && <>
      <h3>{source.extraction.metadata.title ?? "Retained extracted source"}</h3><ResearchLink url={source.source.resolvedUrl} /><dl className={styles.facts}><dt>Extraction status</dt><dd>{source.extraction.status} · {source.extraction.complete ? "complete" : "incomplete"}</dd><dt>Retrieved</dt><dd>{source.source.retrievedAt}</dd><dt>Published</dt><dd>{source.extraction.metadata.publishedAt ?? "Unknown"}{source.extraction.metadata.publicationProvenance ? " · source reported" : ""}</dd><dt>License</dt><dd>{source.extraction.license}</dd><dt>Source digest</dt><dd><code>{source.source.rawSha256}</code></dd><dt>Text digest</dt><dd><code>{source.extraction.textSha256}</code></dd></dl><ResearchReasons codes={source.extraction.reasonCodes} />
      <label className={styles.field}>Complete extracted text — select the exact passage<textarea readOnly rows={16} value={source.extraction.text} onSelect={event => { const element = event.currentTarget; if (element.selectionStart === element.selectionEnd) return; const prefix = researchUTF8(element.value.slice(0, element.selectionStart)), selected = researchUTF8(element.value.slice(element.selectionStart, element.selectionEnd)); if (prefix && selected) changeRange(prefix.length, prefix.length + selected.length); }} /></label>
      <div className={styles.pair}><label className={styles.field}>Start byte (inclusive)<input type="number" min={0} value={startByte} onChange={event => changeRange(Number(event.target.value), endByte)} /></label><label className={styles.field}>End byte (exclusive)<input type="number" min={1} value={endByte} onChange={event => changeRange(startByte, Number(event.target.value))} /></label></div>{quote && <><h4>Selected literal quote ({quote.bytes.length.toLocaleString()} UTF-8 bytes)</h4><pre>{quote.text}</pre></>}<div className={styles.actions}><button disabled={citationBusy || !source.extraction.complete || !quote || quote.bytes.length > 16384} onClick={verifyQuote}>Verify exact citation</button></div><ResearchJSON title="Source metadata, extractor and UTF-8 locations" value={{ source: source.source, extraction: { ...source.extraction, text: "Full text is displayed above." } }} />
      {citation && <><ResearchCitationView citation={citation} /><fieldset disabled={disabled}><div className={styles.pair}><label className={styles.field}>Claim key<input value={claimKey} maxLength={80} onChange={event => { setClaimKey(event.target.value); setReview(null); }} /></label><label className={styles.field}>Claim label<input value={claimLabel} maxLength={512} onChange={event => { setClaimLabel(event.target.value); setReview(null); }} /></label></div><p className={styles.help}>Attach only comparable literal excerpts to the same claim key. The label is your description; the comparison does not infer whether a source supports its meaning.</p><button onClick={addCitation}>Add verified citation to draft comparison</button></fieldset></>}
    </>}</div></section>

    <section className={`${styles.panel} ${styles.section}`} aria-label="Cited source comparison"><div className={styles.panelHeading}><h2>5. Compare cited values and retain evidence</h2><p>Different exact quotes remain unresolved. Freshness requires a source-reported publication time; retrieval time alone does not establish freshness.</p></div><div className={styles.body}>
      {claims.length ? <>{claims.map(claim => <section className={styles.section} key={claim.key}><h3>{claim.label}</h3><code>{claim.key}</code><p>{claim.citations.length} selected citation spans.</p><ResearchJSON title="Exact citation specifications" value={claim.citations} /><button disabled={disabled} onClick={() => { setClaims(current => current.filter(item => item.key !== claim.key)); setReview(null); }}>Remove claim</button></section>)}<button className={styles.primary} disabled={disabled || !context?.current || !context.need || contextBusy} onClick={() => { if (context?.need) reviewMutation("Retain this exact cited-source comparison", "The report revalidates retained source chains and compares exact quotes at a recorded evaluation time. It neither resolves the information need nor marks the task complete.", `${BASE}/comparisons`, { episodeId: context.task.episodeId, needId: context.need.id, contextSha256: context.contextSha256, claims, confirm: true }); }}>Review comparison publication</button></> : <p className={styles.help}>Verify source quotes and attach them to claim keys above to build a comparison.</p>}
      <div className={styles.section}><label className={styles.field}>Read a retained comparison evidence UUID<input value={comparisonId} maxLength={36} spellCheck={false} onChange={event => { comparisons.current.reset(); setComparisonId(event.target.value); setComparison(null); setComparisonBusy(false); }} /></label><div className={styles.actions}><button disabled={comparisonBusy || !UUID.test(comparisonId)} onClick={readComparison}>Read historical comparison</button></div></div>{comparisonNotice && <p className={styles.status} role="status">{comparisonNotice}</p>}{comparison && <ResearchComparisonView comparison={comparison} />}
    </div></section>
  </>;
}

export default function ResearchWorkbench({ workspaceId, initialSelection }: { workspaceId: string; initialSelection: ResearchSelection }) {
  const [initial, setInitial] = useState<ResearchRuns | null>(null), [notice, setNotice] = useState("Reading retained workspace research…"), [version, setVersion] = useState(0);
  const invalidateScope = useCallback(() => { setInitial(null); setNotice("The authenticated workspace scope changed. Reading current workspace history…"); setVersion(value => value + 1); }, []);
  useEffect(() => {
    const request = new AbortController(); setInitial(null);
    void fetch(`${BASE}/runs?limit=25`, { credentials: "same-origin", cache: "no-store", signal: request.signal, headers: headers(workspaceId) }).then(async response => {
      if (!response.ok) throw new Error("unavailable"); const page = projectResearchRuns(await response.json(), workspaceId); if (request.signal.aborted) return; if (!page) throw new Error("invalid"); setInitial(page); setNotice("");
    }).catch(() => { if (!request.signal.aborted) setNotice("Research is unavailable in this workspace. Check operator authentication and refresh."); });
    return () => request.abort();
  }, [workspaceId, version]);
  return initial ? <WorkspaceResearch key={`${workspaceId}:${initial.scope.principalId}:${version}`} initial={initial} initialSelection={initialSelection} invalidateScope={invalidateScope} /> : <section className={styles.notice}><p role="status">{notice}</p><button onClick={() => setVersion(value => value + 1)}>Refresh research</button></section>;
}
