"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { UUID, projectDiscoverySources, projectDiscoveryPublishers, projectDiscoveryCandidates, projectDiscoveryChecks,
  projectDiscoveryCandidateEnvelope, projectDiscoveryCheckEnvelope, projectDiscoveryMutation,
  type DiscoveryScope, type DiscoverySource, type DiscoveryPublisher, type DiscoveryCandidate, type DiscoveryCheck, type DiscoveryPage } from "../../lib/discovery.ts";
import { DISCOVERY_ENDPOINT, EMPTY_SEARCH, CHECK_LABELS, discoverySearchQuery, discoveryJournalScope, discoveryAttemptRoute,
  newDiscoveryAttempt, validateDiscoveryAttempt, discoveryNeedsMonitoring, discoveryScopeInvalidated, type SearchDraft } from "../../lib/discovery-client.ts";
import { WorkshopRequests, clearAttempt, clearRejectedAttempt, recoverAttempt, retainAttempt, type RequestTicket, type WorkshopAttempt } from "../../lib/skill-workshop-client.ts";
import { SearchForm, SourceCard, PublisherCard, CandidateReview, CheckReview, readable, type DiscoveryMutate } from "./DiscoveryViews.tsx";
import styles from "../skills/skills.module.css";

const headers = (workspaceId: string) => ({ "X-Potbot-Workspace": workspaceId });
type ListKind = "sources" | "publishers" | "candidates" | "checks";
type RecordView = DiscoverySource | DiscoveryPublisher | DiscoveryCandidate | DiscoveryCheck;
type Initial = { sources: DiscoveryPage<DiscoverySource>; candidates: DiscoveryPage<DiscoveryCandidate>; checks: DiscoveryPage<DiscoveryCheck> };
type Selection = { kind: "candidate" | "check"; id: string };
const LIST_KINDS: ListKind[] = ["sources", "publishers", "candidates", "checks"];
const VISIBLE_RECORDS = 200;

function WorkspaceDiscovery({ initial, initialCheckId, invalidateScope }: { initial: Initial; initialCheckId: string | null; invalidateScope: () => void }) {
  const scope: DiscoveryScope = initial.sources;
  const emptyPublishers: DiscoveryPage<DiscoveryPublisher> = { schema: scope.schema, workspaceId: scope.workspaceId, principalId: scope.principalId, items: [], nextCursor: null };
  const [sources, setSources] = useState(initial.sources), [candidates, setCandidates] = useState(initial.candidates), [checks, setChecks] = useState(initial.checks);
  const [publishers, setPublishers] = useState(emptyPublishers), [publisherFilter, setPublisherFilter] = useState("");
  const [tab, setTab] = useState<"candidates" | "checks" | "admissions">("candidates"), [draft, setDraft] = useState<SearchDraft>(EMPTY_SEARCH), [queryDirty, setQueryDirty] = useState(false);
  const [listBusy, setListBusy] = useState<Record<ListKind, boolean>>({ sources: false, publishers: false, candidates: false, checks: false });
  const [listNotice, setListNotice] = useState<Record<ListKind, string>>({ sources: "", publishers: "", candidates: "", checks: "" });
  const [listCurrent, setListCurrent] = useState<Record<ListKind, boolean>>({ sources: true, publishers: false, candidates: true, checks: true });
  const [selected, setSelected] = useState<Selection | null>(null), [selectionVersion, setSelectionVersion] = useState(0);
  const [candidate, setCandidate] = useState<DiscoveryCandidate | null>(null), [check, setCheck] = useState<DiscoveryCheck | null>(null);
  const [detailBusy, setDetailBusy] = useState(false), [detailNotice, setDetailNotice] = useState("");
  const [pending, setPending] = useState<WorkshopAttempt | null>(null), [durable, setDurable] = useState<boolean | null>(null), [busy, setBusy] = useState(false), [recovered, setRecovered] = useState(false), [notice, setNotice] = useState("");
  const [live, setLive] = useState(true), [pollNotice, setPollNotice] = useState(""), [visibility, setVisibility] = useState(0), [reviewId, setReviewId] = useState("");
  const lists = useRef(Object.fromEntries(LIST_KINDS.map(kind => [kind, new WorkshopRequests()])) as Record<ListKind, WorkshopRequests>);
  const details = useRef(new WorkshopRequests()), mutations = useRef(new WorkshopRequests());
  const pages = useRef<Record<ListKind, DiscoveryPage<RecordView>>>({ ...initial, publishers: emptyPublishers });
  const selectedRef = useRef<Selection | null>(null), selectionGeneration = useRef(0), query = useRef<string | null>("limit=25"), publisherFilterRef = useRef("");
  const pendingRef = useRef<WorkshopAttempt | null>(null), busyRef = useRef(false), journal = useRef(""), pollCount = useRef(0);

  const abortReads = useCallback(() => { for (const requests of Object.values(lists.current)) requests.reset(); details.current.reset(); }, []);
  const leaveScope = useCallback(() => { abortReads(); mutations.current.reset(); invalidateScope(); }, [abortReads, invalidateScope]);
  useEffect(() => {
    journal.current = discoveryJournalScope(scope.workspaceId, scope.principalId); const ticket = mutations.current.open();
    void recoverAttempt(journal.current, validateDiscoveryAttempt).then(value => { if (!ticket.current()) return; pendingRef.current = value; setPending(value); setRecovered(true); ticket.finish(); });
    const visibilityChanged = () => setVisibility(value => value + 1); document.addEventListener("visibilitychange", visibilityChanged);
    return () => { document.removeEventListener("visibilitychange", visibilityChanged); abortReads(); mutations.current.reset(); };
  }, [scope.workspaceId, scope.principalId, abortReads]);

  const read = useCallback(async (path: string, ticket: RequestTicket): Promise<unknown> => {
    const response = await fetch(path, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: headers(scope.workspaceId) });
    if (!ticket.current()) return null;
    if (discoveryScopeInvalidated(response.status, null, scope.workspaceId, scope.principalId)) { leaveScope(); return null; }
    if (!response.ok) throw new Error("unavailable");
    const raw = await response.json(); if (!ticket.current()) return null;
    if (discoveryScopeInvalidated(response.status, raw, scope.workspaceId, scope.principalId)) { leaveScope(); return null; }
    return raw;
  }, [scope.workspaceId, scope.principalId, leaveScope]);

  const select = useCallback((selection: Selection | null) => {
    details.current.reset(); selectedRef.current = selection; selectionGeneration.current++; pollCount.current = 0;
    setSelected(selection); setSelectionVersion(value => value + 1); setCandidate(null); setCheck(null); setDetailBusy(false); setDetailNotice(""); setPollNotice(""); setLive(true);
    const url = new URL(window.location.href); url.searchParams.set("workspace", scope.workspaceId); url.searchParams.delete("check");
    if (selection?.kind === "check") { url.searchParams.set("check", selection.id); setReviewId(selection.id); }
    window.history.replaceState(null, "", url);
  }, [scope.workspaceId]);
  useEffect(() => { if (initialCheckId) { setTab("checks"); select({ kind: "check", id: initialCheckId }); } }, [initialCheckId, select]);

  const loadList = useCallback((kind: ListKind, append = false) => {
    if (kind === "candidates" && query.current === null) return;
    const previous = pages.current[kind], cursor = append ? previous.nextCursor : null; if (append && !cursor) return;
    const params = new URLSearchParams(kind === "candidates" ? query.current! : "limit=25");
    if (kind === "publishers" && publisherFilterRef.current) params.set("sourceId", publisherFilterRef.current);
    if (cursor) params.set("after", cursor);
    lists.current[kind].reset(); const ticket = lists.current[kind].open();
    setListBusy(value => ({ ...value, [kind]: true })); setListNotice(value => ({ ...value, [kind]: "Reading retained records…" }));
    if (!append) setListCurrent(value => ({ ...value, [kind]: false }));
    void (async () => {
      try {
        const raw = await read(`${DISCOVERY_ENDPOINT}/${kind}?${params}`, ticket); if (!ticket.current()) return;
        const result: DiscoveryPage<RecordView> | null = kind === "sources" ? projectDiscoverySources(raw, scope.workspaceId, scope.principalId) : kind === "publishers" ? projectDiscoveryPublishers(raw, scope.workspaceId, scope.principalId, publisherFilterRef.current || undefined) :
          kind === "candidates" ? projectDiscoveryCandidates(raw, scope.workspaceId, scope.principalId) : projectDiscoveryChecks(raw, scope.workspaceId, scope.principalId);
        if (!result || (cursor !== null && result.nextCursor === cursor)) throw new Error("invalid");
        const items = append ? [...new Map([...previous.items, ...result.items].map(item => [item.id, item])).values()] : result.items;
        const next = { ...result, items: items.slice(-VISIBLE_RECORDS) }; pages.current[kind] = next;
        if (kind === "sources") setSources(next as DiscoveryPage<DiscoverySource>); else if (kind === "publishers") setPublishers(next as DiscoveryPage<DiscoveryPublisher>);
        else if (kind === "candidates") setCandidates(next as DiscoveryPage<DiscoveryCandidate>); else setChecks(next as DiscoveryPage<DiscoveryCheck>);
        setListCurrent(value => ({ ...value, [kind]: true }));
        setListNotice(value => ({ ...value, [kind]: items.length > VISIBLE_RECORDS ? "Showing the most recent 200 loaded records. Refresh to return to the beginning; the next cursor still continues without skipping records." : result.nextCursor ? "More retained records are available." : "All records in this bounded result are shown." }));
      } catch { if (ticket.current()) setListNotice(value => ({ ...value, [kind]: "These records could not be refreshed. Retry before making a decision; the cursor has not advanced." })); }
      finally { if (ticket.current()) setListBusy(value => ({ ...value, [kind]: false })); ticket.finish(); }
    })();
  }, [read, scope.workspaceId, scope.principalId]);

  const readDetail = useCallback(async (selection: Selection, showBusy: boolean) => {
    if (selectedRef.current?.id !== selection.id || selectedRef.current.kind !== selection.kind) return;
    details.current.reset(); const ticket = details.current.open(); if (showBusy) setDetailBusy(true); setDetailNotice("");
    try {
      const raw = await read(`${DISCOVERY_ENDPOINT}/${selection.kind === "candidate" ? "candidates" : "checks"}/${selection.id}`, ticket); if (!ticket.current()) return;
      if (selection.kind === "candidate") {
        const result = projectDiscoveryCandidateEnvelope(raw, scope.workspaceId, selection.id, scope.principalId); if (!result) throw new Error("invalid"); setCandidate(result.candidate);
      } else {
        const result = projectDiscoveryCheckEnvelope(raw, scope.workspaceId, selection.id, scope.principalId); if (!result) throw new Error("invalid"); setCheck(result.check);
        setChecks(value => { const next = { ...value, items: value.items.map(item => item.id === selection.id ? result.check : item) }; pages.current.checks = next; return next; });
      }
    } catch { if (ticket.current()) { setDetailNotice("The current record is unavailable in this scope. Refresh it before making another decision."); setLive(false); } }
    finally { if (ticket.current()) setDetailBusy(false); ticket.finish(); }
  }, [read, scope.workspaceId, scope.principalId]);
  useEffect(() => { if (selected) void readDetail(selected, true); return () => details.current.reset(); }, [selected, selectionVersion, readDetail]);
  useEffect(() => {
    if (!check || !live || !discoveryNeedsMonitoring(check)) return;
    if (pollCount.current >= 60) { setLive(false); setPollNotice("Automatic monitoring reached its 60-read limit. Resume monitoring or refresh for another observation."); return; }
    if (document.visibilityState === "hidden") { setPollNotice("Monitoring is paused while this tab is hidden."); return; }
    setPollNotice("Reading this review every five seconds while a host measurement is pending.");
    const timer = window.setTimeout(() => { pollCount.current++; void readDetail({ kind: "check", id: check.id }, false); }, 5000);
    return () => window.clearTimeout(timer);
  }, [check, live, visibility, readDetail]);

  function changeTab(next: typeof tab) { if (next !== tab) select(null); setTab(next); if (next === "admissions") loadList("publishers"); }
  function changeSearch(value: SearchDraft) {
    lists.current.candidates.reset(); query.current = null; setQueryDirty(true); setDraft(value); setListBusy(state => ({ ...state, candidates: false }));
    const next = { ...initial.candidates, items: [], nextCursor: null }; pages.current.candidates = next; setCandidates(next); setListCurrent(state => ({ ...state, candidates: false }));
    setListNotice(state => ({ ...state, candidates: "Filters changed. Search to read a result bound to these exact constraints." })); select(null);
  }
  function search() {
    const params = discoverySearchQuery(draft);
    if (!params) { setListNotice(state => ({ ...state, candidates: "Check the search limits and exact capability, platform, effect, scope and Type values." })); return; }
    select(null); query.current = params.toString(); setQueryDirty(false); loadList("candidates");
  }
  function inspectPublishers(sourceId: string) {
    select(null); setTab("admissions"); publisherFilterRef.current = sourceId; setPublisherFilter(sourceId);
    setPublishers(emptyPublishers); pages.current.publishers = emptyPublishers; loadList("publishers");
  }
  function refresh() { for (const kind of LIST_KINDS) if (kind !== "publishers" || tab === "admissions") loadList(kind); if (selectedRef.current) void readDetail(selectedRef.current, true); }

  function send(attempt: WorkshopAttempt, retry = false) {
    if (!recovered || busyRef.current || (pendingRef.current && (!retry || pendingRef.current.key !== attempt.key))) return;
    if (!validateDiscoveryAttempt(attempt)) { setNotice("This retained request is not a supported exact discovery action."); return; }
    busyRef.current = true; pendingRef.current = attempt; setBusy(true); setPending(attempt); setNotice("Retaining the exact request before submission…");
    const ticket = mutations.current.open(), currentJournal = journal.current, generation = selectionGeneration.current;
    void (async () => {
      try {
        const retained = await retainAttempt(currentJournal, attempt); if (!ticket.current()) return; setDurable(retained); setNotice("Recording the reviewed decision…");
        const response = await fetch(attempt.path, { method: "POST", credentials: "same-origin", cache: "no-store", signal: ticket.signal,
          headers: { ...headers(scope.workspaceId), "Content-Type": "application/json", "Idempotency-Key": attempt.key }, body: attempt.body });
        if (!ticket.current()) return;
        // Authorization can change while any tab is open. Remove private cards
        // immediately and retain the original journal for its original principal.
        if (discoveryScopeInvalidated(response.status, null, scope.workspaceId, scope.principalId)) { leaveScope(); return; }
        if ([400, 401, 403, 404, 409, 413, 422].includes(response.status)) {
          const cleared = await clearRejectedAttempt(currentJournal, attempt.key, response.status, retry); if (!ticket.current()) return;
          if (!cleared) { setNotice("Recovery was rejected, but the earlier action may already have committed. Keep this original request and key; restore authorization or connectivity, then retry it explicitly."); return; }
          pendingRef.current = null; setPending(null); setDurable(null);
          setNotice(response.status === 409 ? "The reviewed source, admission, snapshot or measurement changed. Refresh and review the current record before creating another action." : "The discovery action was rejected. Check the exact selection and operator authorization.");
          if (selectedRef.current && generation === selectionGeneration.current) void readDetail(selectedRef.current, true);
          return;
        }
        if (!response.ok) throw new Error("unconfirmed");
        const raw = await response.json(); if (!ticket.current()) return;
        if (discoveryScopeInvalidated(response.status, raw, scope.workspaceId, scope.principalId)) { leaveScope(); return; }
        const result = projectDiscoveryMutation(raw, scope.workspaceId, discoveryAttemptRoute(attempt.path)!, JSON.parse(attempt.body), scope.principalId);
        if (!ticket.current()) return; if (!result) throw new Error("unconfirmed");
        await clearAttempt(currentJournal, attempt.key); if (!ticket.current()) return;
        pendingRef.current = null; setPending(null); setDurable(null); setNotice("The exact action was recorded. Reading the latest retained state…");
        loadList("sources"); loadList("checks"); loadList("candidates"); loadList("publishers");
        if (generation === selectionGeneration.current) {
          if ("check" in result) { setTab("checks"); select({ kind: "check", id: result.check.id }); }
          else if (selectedRef.current) void readDetail(selectedRef.current, true);
        }
      } catch { if (ticket.current()) setNotice("The outcome could not be confirmed. The original exact request remains available below; no automatic retry will occur."); }
      finally { if (ticket.current()) { busyRef.current = false; setBusy(false); } ticket.finish(); }
    })();
  }
  const mutate: DiscoveryMutate = (path, body) => send(newDiscoveryAttempt(path, body));
  const locked = !recovered || busy || pending !== null;
  const activeList = tab === "checks" ? "checks" : "candidates";
  return <>
    <div className={styles.tabs}><button type="button" aria-pressed={tab === "candidates"} onClick={() => changeTab("candidates")}>Discover capabilities</button><button type="button" aria-pressed={tab === "checks"} onClick={() => changeTab("checks")}>My exact reviews</button><button type="button" aria-pressed={tab === "admissions"} onClick={() => changeTab("admissions")}>Sources and publishers</button></div>
    <div className={styles.actions}><button type="button" disabled={busy} onClick={refresh}>Refresh retained workspace state</button><span className={styles.help}>Refresh reads saved records. The host operator owns remote source refresh and explicit measurements.</span></div>
    <p className={styles.status} role="status">{recovered ? notice : "Checking this tab for an unresolved exact discovery action…"}</p>
    {pending && <section className={styles.uncertain} aria-label="Exact discovery request recovery"><h3>{busy ? "Recording this decision" : "Recover an unresolved decision"}</h3><p>Retry uses the original body and idempotency key. If it already committed, the original receipt is returned before this view reads the current state.</p><code>{pending.key}</code><p>Action: {pending.path.slice(DISCOVERY_ENDPOINT.length + 1)}</p>
      {durable === false && <p className={styles.warning}>Durable browser recovery is unavailable or another duplicated tab owns the stored slot. Keep this tab open until its outcome is confirmed.</p>}
      <button type="button" disabled={busy} onClick={() => send(pending, true)}>Retry exact request</button></section>}
    {tab === "admissions" ? <>
      <section className={styles.notice}><h2>Admit sources and publisher namespaces</h2><p>Review the recorded source configuration and publisher claims before enabling acquisition review. Source creation and bounded registry refresh use the operator CLI. Revocation fences future discovery handoffs; manage already acquired packages through their existing lifecycle controls.</p></section>
      <h2>Retained sources</h2><p className={styles.status} role="status">{listNotice.sources}</p><div className={styles.legacyGrid}>{sources.items.map(source => <SourceCard key={source.id} source={source} disabled={locked || listBusy.sources || !listCurrent.sources} mutate={mutate} publishers={inspectPublishers} />)}</div>
      {!sources.items.length && <p className={styles.empty}>No source is configured in this workspace. An operator can publish a curated source or add a supported registry adapter with the discovery CLI.</p>}
      {sources.nextCursor && <div className={styles.actions}><button type="button" disabled={listBusy.sources} onClick={() => loadList("sources", true)}>Load more sources</button></div>}
      <section className={styles.section}><h2>Publisher decisions</h2><label className={styles.field}>Source<select value={publisherFilter} onChange={event => inspectPublishers(event.target.value)}><option value="">All retained sources</option>{sources.items.map(source => <option key={source.id} value={source.id}>{source.name}</option>)}</select></label><p className={styles.status} role="status">{listNotice.publishers}</p>
        <div className={styles.legacyGrid}>{publishers.items.map(publisher => <PublisherCard key={publisher.id} publisher={publisher} sourceName={sources.items.find(source => source.id === publisher.sourceId)?.name ?? publisher.sourceId} disabled={locked || listBusy.publishers || !listCurrent.publishers} mutate={mutate} />)}</div>
        {!publishers.items.length && <p className={styles.empty}>No publisher claims are retained for this selection. A successful source refresh records the namespaces that can be reviewed.</p>}
        {publishers.nextCursor && <div className={styles.actions}><button type="button" disabled={listBusy.publishers} onClick={() => loadList("publishers", true)}>Load more publishers</button></div>}
      </section>
    </> : <div className={styles.layout}><aside className={styles.panel}>
      <div className={styles.panelHeading}><h2>{tab === "checks" ? "Retained exact reviews" : "Find a capability"}</h2><p>{tab === "checks" ? "Resume an owned review, measurement or acquisition handoff." : "Search saved registry observations and apply explicit constraints."}</p></div>
      {tab === "candidates" ? <div className={styles.body}><SearchForm value={draft} sources={sources.items} disabled={false} change={changeSearch} submit={search} /></div> : <form className={styles.body} onSubmit={event => { event.preventDefault(); if (UUID.test(reviewId)) select({ kind: "check", id: reviewId }); }}>
        <label className={styles.field}>Open exact review UUID<input value={reviewId} maxLength={36} autoComplete="off" spellCheck={false} onChange={event => { setReviewId(event.target.value); select(null); }} /></label><div className={styles.actions}><button type="submit" disabled={!UUID.test(reviewId)}>Open review</button></div></form>}
      <p className={styles.body} role="status">{listNotice[activeList]}</p>
      <ul className={styles.cards}>{tab === "candidates" ? candidates.items.map(item => <li key={item.id}><button type="button" className={selected?.kind === "candidate" && selected.id === item.id ? styles.selected : undefined} onClick={() => select({ kind: "candidate", id: item.id })}>
        <span className={styles.row}><strong>{item.observation.name}</strong><span className={styles.badge}>{readable(item.observation.kind)}</span></span><small>{item.observation.version ?? "Unresolved version"} · {readable(item.observation.adapter)}</small><small>{item.stale ? "Snapshot stale" : item.canReview ? "Exact acquisition review available" : item.reasonCodes.map(readable).join(" · ") || "Inspection only"}</small></button></li>) : checks.items.map(item => <li key={item.id}><button type="button" className={selected?.kind === "check" && selected.id === item.id ? styles.selected : undefined} onClick={() => select({ kind: "check", id: item.id })}>
        <strong>{item.candidate.observation.name}</strong><span className={styles.badge}>{CHECK_LABELS[item.status]}</span><small>{item.candidate.observation.version ?? "Resolved review"} · revision {item.revision}</small><small>{item.id}</small></button></li>)}</ul>
      {!(tab === "candidates" ? candidates.items.length : checks.items.length) && <div className={styles.empty}>{queryDirty && tab === "candidates" ? "Search with the current constraints to load matching observations." : tab === "checks" ? "No reviews have been retained for this principal." : "No retained observations match. Review source availability, refresh on the host, or adjust the search constraints."}</div>}
      {(tab === "checks" ? checks.nextCursor : candidates.nextCursor) && <div className={styles.body}><button type="button" disabled={listBusy[activeList]} onClick={() => loadList(activeList, true)}>Load more {tab === "checks" ? "reviews" : "observations"}</button></div>}
    </aside><section className={styles.panel} aria-label="Selected discovery record" aria-busy={detailBusy}>
      {selected?.kind === "check" && <div className={styles.body}><label className={styles.check}><input type="checkbox" checked={live} onChange={event => { pollCount.current = 0; setLive(event.target.checked); setPollNotice(""); }} /><span>Monitor pending host measurement</span></label><p className={styles.help}>{pollNotice || "Monitoring stops after completion, while hidden, or after 60 reads. It never submits a measurement."}</p></div>}
      {detailNotice && <div className={styles.body}><p className={styles.warning} role="alert">{detailNotice}</p><button type="button" onClick={() => selected && void readDetail(selected, true)}>Refresh selected record</button></div>}
      {candidate ? <CandidateReview key={`${candidate.id}:${candidate.snapshotId}:${candidate.candidateSha256}`} candidate={candidate} disabled={locked || detailBusy || Boolean(detailNotice)} mutate={mutate} admission={inspectPublishers} /> : check ? <CheckReview key={check.id} check={check} disabled={locked || detailBusy || Boolean(detailNotice)} mutate={mutate} workspaceId={scope.workspaceId} refresh={() => selected && void readDetail(selected, true)} /> : <div className={styles.empty}><span className={styles.eyebrow}>DISCOVER · REVIEW · MEASURE · ACQUIRE</span><h2>{detailBusy ? "Reading the exact retained record…" : "Review before acquisition"}</h2><p>Select an observation to inspect its source, version, capabilities and requirements. Review the selected agent&apos;s measured prerequisites before continuing to the existing acquisition workflow.</p></div>}
    </section></div>}
  </>;
}

export default function DiscoveryWorkbench({ workspaceId, initialCheckId = null }: { workspaceId: string; initialCheckId?: string | null }) {
  const [initial, setInitial] = useState<Initial | null>(null), [notice, setNotice] = useState("Reading retained discovery records…"), [version, setVersion] = useState(0);
  const invalidateScope = useCallback(() => { setInitial(null); setVersion(value => value + 1); }, []);
  useEffect(() => {
    const controller = new AbortController(); setInitial(null); setNotice("Reading retained discovery records…");
    void (async () => {
      try {
        const responses = await Promise.all(["sources", "candidates", "checks"].map(kind => fetch(`${DISCOVERY_ENDPOINT}/${kind}?limit=25`, { credentials: "same-origin", cache: "no-store", signal: controller.signal, headers: headers(workspaceId) })));
        if (controller.signal.aborted) return; if (responses.some(response => !response.ok)) throw new Error("unavailable");
        const [a, b, c] = await Promise.all(responses.map(response => response.json())); if (controller.signal.aborted) return;
        const sources = projectDiscoverySources(a, workspaceId), candidates = projectDiscoveryCandidates(b, workspaceId, sources?.principalId), checks = projectDiscoveryChecks(c, workspaceId, sources?.principalId);
        if (!sources || !candidates || !checks || sources.principalId !== candidates.principalId || sources.principalId !== checks.principalId) throw new Error("invalid");
        setInitial({ sources, candidates, checks }); setNotice("");
      } catch { if (!controller.signal.aborted) setNotice("Discovery records are unavailable. Check the operator session, matching backend migration and workspace connection, then reload."); }
    })(); return () => controller.abort();
  }, [workspaceId, version]);
  return initial ? <WorkspaceDiscovery key={`${workspaceId}:${initial.sources.principalId}:${version}`} initial={initial} initialCheckId={initialCheckId} invalidateScope={invalidateScope} /> :
    <section className={styles.notice}><p role="status">{notice}</p><button type="button" onClick={invalidateScope}>Reload discovery workbench</button></section>;
}
