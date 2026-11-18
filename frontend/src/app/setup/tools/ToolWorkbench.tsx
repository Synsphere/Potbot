"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { projectAcquisition, projectAcquisitionEvents, projectAcquisitionList, projectRecipeList, projectToolMutation,
  type AcquisitionEvents, type AcquisitionList, type RecipeList, type ToolOperation } from "../../../lib/tool-acquisition.ts";
import { TOOL_ENDPOINT, TOOL_PHASE_LABELS, newToolAttempt, toolAttemptRoute, toolJournalScope, toolNeedsMonitoring, validateToolAttempt } from "../../../lib/tool-acquisition-client.ts";
import { WorkshopRequests, clearAttempt, clearRejectedAttempt, recoverAttempt, retainAttempt, type WorkshopAttempt } from "../../../lib/skill-workshop-client.ts";
import { SourceRequest, ToolDetail, type ToolMutate } from "./AcquisitionViews.tsx";
import styles from "../../skills/skills.module.css";

const headers = (workspaceId: string) => ({ "X-Potbot-Workspace": workspaceId });
type Scope = { workspaceId: string; principalId: string };

function WorkspaceTools({ scope, initialOperations, initialRecipes, initialOperationId, invalidateScope }: { scope: Scope; initialOperations: AcquisitionList; initialRecipes: RecipeList; initialOperationId: string | null; invalidateScope: () => void }) {
  const [operations, setOperations] = useState(initialOperations), [recipes, setRecipes] = useState(initialRecipes), [listBusy, setListBusy] = useState(false);
  const [selected, setSelected] = useState(""), [selectionVersion, setSelectionVersion] = useState(0), [operation, setOperation] = useState<ToolOperation | null>(null);
  const [detailBusy, setDetailBusy] = useState(false), [detailNotice, setDetailNotice] = useState(""), [notice, setNotice] = useState("");
  const [history, setHistory] = useState<AcquisitionEvents | null>(null), [historyBusy, setHistoryBusy] = useState(false), [historyNotice, setHistoryNotice] = useState("");
  const [pending, setPending] = useState<WorkshopAttempt | null>(null), [durable, setDurable] = useState<boolean | null>(null), [busy, setBusy] = useState(false), [recovered, setRecovered] = useState(false);
  const [live, setLive] = useState(true), [pollNotice, setPollNotice] = useState(""), [visibility, setVisibility] = useState(0);
  const lists = useRef(new WorkshopRequests()), details = useRef(new WorkshopRequests()), events = useRef(new WorkshopRequests()), mutations = useRef(new WorkshopRequests());
  const selectedRef = useRef(""), selectionGeneration = useRef(0), eventCursor = useRef(0), historyRef = useRef<AcquisitionEvents | null>(null), droppedEvents = useRef(0), pollCount = useRef(0);
  const pendingRef = useRef<WorkshopAttempt | null>(null), busyRef = useRef(false), listBusyRef = useRef(false), journal = useRef("");
  useEffect(() => {
    journal.current = toolJournalScope(scope.workspaceId, scope.principalId); const ticket = mutations.current.open();
    void recoverAttempt(journal.current, validateToolAttempt).then(value => { if (!ticket.current()) return; pendingRef.current = value; setPending(value); setRecovered(true); ticket.finish(); });
    const changedVisibility = () => setVisibility(value => value + 1); document.addEventListener("visibilitychange", changedVisibility);
    return () => { document.removeEventListener("visibilitychange", changedVisibility); lists.current.reset(); details.current.reset(); events.current.reset(); mutations.current.reset(); };
  }, [scope.workspaceId, scope.principalId]);
  const leaveScope = useCallback(() => { lists.current.reset(); details.current.reset(); events.current.reset(); mutations.current.reset(); invalidateScope(); }, [invalidateScope]);
  const select = useCallback((id: string) => {
    details.current.reset(); events.current.reset(); selectedRef.current = id; selectionGeneration.current++; eventCursor.current = 0; historyRef.current = null; droppedEvents.current = 0; pollCount.current = 0;
    setSelected(id); setSelectionVersion(value => value + 1); setOperation(null); setHistory(null); setDetailNotice(""); setHistoryNotice(""); setPollNotice(""); setLive(true);
  }, []);
  useEffect(() => { if (initialOperationId) select(initialOperationId); }, [initialOperationId, select]);
  const refreshLists = useCallback(() => {
    lists.current.reset(); const ticket = lists.current.open(); listBusyRef.current = true; setListBusy(true); setNotice("Refreshing acquisition records and published recipes…");
    void (async () => {
      try {
        const responses = await Promise.all(["", "/recipes"].map(suffix => fetch(`${TOOL_ENDPOINT}${suffix}?limit=25`, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: headers(scope.workspaceId) })));
        if (!ticket.current()) return; if (responses.some(response => response.status === 401 || response.status === 403)) { leaveScope(); return; }
        if (responses.some(response => !response.ok)) throw new Error("unavailable");
        const [a, b] = await Promise.all(responses.map(response => response.json())); if (!ticket.current()) return;
        const nextOperations = projectAcquisitionList(a, scope.workspaceId), nextRecipes = projectRecipeList(b, scope.workspaceId);
        if ((nextOperations && nextOperations.principalId !== scope.principalId) || (nextRecipes && nextRecipes.principalId !== scope.principalId)) { leaveScope(); return; }
        if (!nextOperations || !nextRecipes) throw new Error("invalid");
        setOperations(nextOperations); setRecipes(nextRecipes); setNotice("");
      } catch { if (ticket.current()) setNotice("Acquisition records could not be refreshed. Check the workspace connection and reload before applying another change."); }
      finally { if (ticket.current()) { listBusyRef.current = false; setListBusy(false); } ticket.finish(); }
    })();
  }, [scope.workspaceId, scope.principalId, leaveScope]);

  function more(kind: "operations" | "recipes") {
    const cursor = kind === "operations" ? operations.nextCursor : recipes.nextCursor; if (!cursor || listBusyRef.current) return;
    const ticket = lists.current.open(); listBusyRef.current = true; setListBusy(true);
    void (async () => {
      try {
        const response = await fetch(`${TOOL_ENDPOINT}${kind === "recipes" ? "/recipes" : ""}?limit=25&after=${cursor}`, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: headers(scope.workspaceId) });
        if (!ticket.current()) return; if (!response.ok) throw new Error("unavailable"); const raw = await response.json(); if (!ticket.current()) return;
        if (kind === "recipes") {
          const result = projectRecipeList(raw, scope.workspaceId, scope.principalId); if (!result || result.nextCursor === cursor) throw new Error("invalid");
          setRecipes(prior => ({ ...result, items: [...new Map([...prior.items, ...result.items].map(item => [item.id, item])).values()] }));
        } else {
          const result = projectAcquisitionList(raw, scope.workspaceId, scope.principalId); if (!result || result.nextCursor === cursor) throw new Error("invalid");
          setOperations(prior => ({ ...result, items: [...new Map([...prior.items, ...result.items].map(item => [item.id, item])).values()] }));
        }
      } catch { if (ticket.current()) setNotice("The next bounded page could not be read. Reload the workspace records."); }
      finally { if (ticket.current()) { listBusyRef.current = false; setListBusy(false); } ticket.finish(); }
    })();
  }

  const readDetail = useCallback(async (id: string, showBusy: boolean) => {
    if (selectedRef.current !== id) return;
    details.current.reset(); const ticket = details.current.open(); if (showBusy) setDetailBusy(true); setDetailNotice("");
    try {
      const response = await fetch(`${TOOL_ENDPOINT}/${id}`, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: headers(scope.workspaceId) });
      if (!ticket.current() || selectedRef.current !== id) return; if (!response.ok) throw new Error("unavailable");
      const result = projectAcquisition(await response.json(), scope.workspaceId, id, scope.principalId); if (!ticket.current() || selectedRef.current !== id) return;
      if (!result) throw new Error("invalid"); setOperation(result.operation);
      setOperations(prior => ({ ...prior, items: prior.items.map(item => item.id === id ? result.operation : item) }));
    } catch { if (ticket.current() && selectedRef.current === id) { setDetailNotice("Current acquisition state is unavailable in this scope. Refresh before applying another action."); setLive(false); } }
    finally { if (ticket.current()) setDetailBusy(false); ticket.finish(); }
  }, [scope.workspaceId, scope.principalId]);

  const readEvents = useCallback((reset = false) => {
    const id = selectedRef.current; if (!id) return;
    events.current.reset(); const ticket = events.current.open();
    if (reset) { eventCursor.current = 0; historyRef.current = null; droppedEvents.current = 0; setHistory(null); }
    const after = eventCursor.current; setHistoryBusy(true); setHistoryNotice("Reading bounded control history…");
    void (async () => {
      try {
        const response = await fetch(`${TOOL_ENDPOINT}/${id}/events?after=${after}&limit=50`, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: headers(scope.workspaceId) });
        if (!ticket.current() || selectedRef.current !== id) return; if (!response.ok) throw new Error("unavailable");
        const result = projectAcquisitionEvents(await response.json(), scope.workspaceId, after, scope.principalId, id); if (!ticket.current() || selectedRef.current !== id) return;
        if (!result) throw new Error("invalid"); eventCursor.current = result.nextCursor;
        const all = [...(after && historyRef.current ? historyRef.current.items : []), ...result.items]; droppedEvents.current += Math.max(0, all.length - 200);
        historyRef.current = { ...result, items: all.slice(-200) }; setHistory(historyRef.current);
        setHistoryNotice(result.hasMore ? "More control events remain. Load the next page to continue." : "Control history is current through the returned cursor.");
      } catch { if (ticket.current() && selectedRef.current === id) setHistoryNotice("Control history could not be read. Its cursor has not advanced."); }
      finally { if (ticket.current()) setHistoryBusy(false); ticket.finish(); }
    })();
  }, [scope.workspaceId, scope.principalId]);

  useEffect(() => { if (!selected) return; void readDetail(selected, true); readEvents(true); return () => { details.current.reset(); events.current.reset(); }; }, [selected, selectionVersion, readDetail, readEvents]);
  useEffect(() => {
    if (!operation || !live || !toolNeedsMonitoring(operation)) return;
    if (pollCount.current >= 60) { setLive(false); setPollNotice("Automatic monitoring reached its 60-read limit. Resume monitoring or refresh for a new observation."); return; }
    if (document.visibilityState === "hidden") { setPollNotice("Live monitoring is paused while this tab is hidden."); return; }
    setPollNotice("Reading the selected operation every five seconds while host work is pending.");
    const timer = window.setTimeout(() => { pollCount.current++; void readDetail(operation.id, false); }, 5000);
    return () => window.clearTimeout(timer);
  }, [operation, live, visibility, readDetail]);

  function send(attempt: WorkshopAttempt, retry = false) {
    if (!recovered || busyRef.current || (pendingRef.current && (!retry || pendingRef.current.key !== attempt.key))) return;
    if (!validateToolAttempt(attempt)) { setNotice("The retained action is not a supported exact acquisition request."); return; }
    busyRef.current = true; pendingRef.current = attempt; setBusy(true); setPending(attempt); setNotice("Retaining the exact request before submission…");
    const ticket = mutations.current.open(), currentJournal = journal.current, selectedGeneration = selectionGeneration.current;
    void (async () => {
      try {
        const retained = await retainAttempt(currentJournal, attempt); if (!ticket.current()) return; setDurable(retained); setNotice("Recording the exact reviewed action…");
        const response = await fetch(attempt.path, { method: "POST", credentials: "same-origin", cache: "no-store", signal: ticket.signal,
          headers: { ...headers(scope.workspaceId), "Content-Type": "application/json", "Idempotency-Key": attempt.key }, body: attempt.body });
        if (!ticket.current()) return;
        if ([400, 401, 403, 404, 409, 413, 422].includes(response.status)) {
          const cleared = await clearRejectedAttempt(currentJournal, attempt.key, response.status, retry); if (!ticket.current()) return;
          if (!cleared) { setNotice("Recovery was rejected, but the earlier action may already have committed. The original body and key are retained; restore authorization or connectivity, then retry this exact request."); return; }
          pendingRef.current = null; setPending(null); setDurable(null);
          setNotice(response.status === 409 ? "The revision or reviewed evidence changed. Refresh and review the current source, build or release before submitting a new action." : "The acquisition action was rejected. Check the published recipe, exact source and operator authorization.");
          if (selectedRef.current && selectedGeneration === selectionGeneration.current) { void readDetail(selectedRef.current, true); readEvents(false); }
          return;
        }
        if (!response.ok) throw new Error("unconfirmed");
        const result = projectToolMutation(await response.json(), scope.workspaceId, toolAttemptRoute(attempt.path)!, JSON.parse(attempt.body), scope.principalId);
        if (!ticket.current()) return; if (!result) throw new Error("unconfirmed");
        await clearAttempt(currentJournal, attempt.key); if (!ticket.current()) return;
        pendingRef.current = null; setPending(null); setDurable(null); setNotice("The exact action was recorded. Reading its current state…"); refreshLists();
        if (selectedGeneration === selectionGeneration.current) select(result.operation.id);
      } catch { if (ticket.current()) setNotice("The action's outcome could not be confirmed. Recover its original receipt using the retained exact request below."); }
      finally { if (ticket.current()) { busyRef.current = false; setBusy(false); } ticket.finish(); }
    })();
  }
  const mutate: ToolMutate = (path, body) => send(newToolAttempt(path, body));
  const locked = !recovered || busy || pending !== null;
  return <>
    <div className={styles.actions}><button type="button" disabled={listBusy || busy} onClick={() => { refreshLists(); if (selectedRef.current) { void readDetail(selectedRef.current, true); readEvents(false); } }}>Refresh workspace</button>
      <span className={styles.help}>{operations.items.length} acquisition records · {recipes.items.length} published recipes shown</span></div>
    <p className={styles.status} role="status">{recovered ? notice : "Checking this tab for an unresolved acquisition request…"}</p>
    {pending && <section className={styles.uncertain} aria-label="Exact acquisition request recovery"><h3>{busy ? "Recording this action" : "Recover an unresolved action"}</h3>
      <p>{busy ? "The exact request is retained while the server records it." : "Retry uses the identical body and idempotency key. It recovers the original receipt if the action already committed; it does not repeat an uncertain host effect."}</p>
      <code>{pending.key}</code><p>Action: {pending.path === TOOL_ENDPOINT ? "request source inspection" : pending.path.slice(TOOL_ENDPOINT.length + 1)}</p>
      {durable === false && <p className={styles.warning}>Durable browser recovery is unavailable or another duplicated tab owns the stored slot. Recovery is limited to this open tab; keep it open until the outcome is confirmed.</p>}
      <button type="button" disabled={busy} onClick={() => send(pending, true)}>Retry exact request</button></section>}
    <div className={styles.layout}><aside className={styles.panel}><div className={styles.panelHeading}><h2>Operator recipes and acquisitions</h2><p>Exact sources, explicit reviews and retained evidence.</p></div>
      <SourceRequest recipes={recipes.items} disabled={locked || listBusy} mutate={mutate} nextCursor={recipes.nextCursor} loadMore={() => more("recipes")} loading={listBusy} />
      <div className={styles.panelHeading}><h2>My acquisition records</h2><p>Every step refers to the same durable tool operation.</p></div>
      <ul className={styles.cards}>{operations.items.map(item => <li key={item.id}><button type="button" className={selected === item.id ? styles.selected : undefined} onClick={() => select(item.id)}>
        <span className={styles.row}><strong>{item.slug}</strong><span className={styles.badge}>{TOOL_PHASE_LABELS[item.phase]}</span></span><small>{item.newCommit.slice(0, 12)} · revision {item.revision}</small>
        <small>{item.kind === null ? "Predecessor not verified" : item.kind === "install" ? "First installation" : "Upgrade"}</small></button></li>)}</ul>
      {!operations.items.length && <p className={styles.empty}>Request an exact source to start the acquisition review.</p>}
      {operations.nextCursor && <div className={styles.body}><button type="button" disabled={listBusy} onClick={() => more("operations")}>Load more acquisitions</button></div>}
    </aside><section className={styles.panel} aria-label="Selected acquisition" aria-busy={detailBusy}>
      {selected && <div className={styles.body}><label className={styles.check}><input type="checkbox" checked={live} onChange={event => { pollCount.current = 0; setLive(event.target.checked); setPollNotice(""); }} /><span>Monitor pending host work</span></label>
        <p className={styles.help}>{pollNotice || "Monitoring pauses when this operation requires a review or a host prerequisite."}</p>
        {detailNotice && <p className={styles.warning} role="alert">{detailNotice}</p>}
        {droppedEvents.current > 0 && <p className={styles.help}>Showing the most recent 200 loaded control events; {droppedEvents.current} earlier loaded events were trimmed. Read from the beginning to inspect them again.</p>}
      </div>}
      {operation ? <ToolDetail operation={operation} disabled={locked || Boolean(detailNotice)} mutate={mutate} history={history} historyBusy={historyBusy} historyNotice={historyNotice} loadEvents={readEvents}
        refresh={() => { void readDetail(operation.id, true); readEvents(false); }} workspaceId={scope.workspaceId} /> : <div className={styles.empty} role="status">
        <span className={styles.eyebrow}>SOURCE · BUILD · TEST · RELEASE</span><h2>{detailBusy ? "Reading the exact operation…" : "Review each executable change"}</h2>
        <p>{selected ? detailNotice : "Select a published recipe and exact commit, then inspect the retained source and build review. A passing build leads to a separate activation decision and independent host release checks."}</p>
      </div>}
    </section></div>
  </>;
}

export default function ToolWorkbench({ workspaceId, initialOperationId = null }: { workspaceId: string; initialOperationId?: string | null }) {
  const [initial, setInitial] = useState<{ operations: AcquisitionList; recipes: RecipeList } | null>(null), [notice, setNotice] = useState("Loading scoped acquisition records…"), [version, setVersion] = useState(0);
  const invalidate = useCallback(() => { setInitial(null); setVersion(value => value + 1); }, []);
  useEffect(() => {
    const controller = new AbortController(); setInitial(null); setNotice("Loading scoped acquisition records…");
    void (async () => {
      try {
        const responses = await Promise.all(["", "/recipes"].map(suffix => fetch(`${TOOL_ENDPOINT}${suffix}?limit=25`, { credentials: "same-origin", cache: "no-store", signal: controller.signal, headers: headers(workspaceId) })));
        if (controller.signal.aborted) return; if (responses.some(response => !response.ok)) throw new Error("unavailable");
        const [a, b] = await Promise.all(responses.map(response => response.json())); if (controller.signal.aborted) return;
        const operations = projectAcquisitionList(a, workspaceId), recipes = projectRecipeList(b, workspaceId, operations?.principalId);
        if (!operations || !recipes || operations.principalId !== recipes.principalId) throw new Error("invalid");
        setInitial({ operations, recipes }); setNotice("");
      } catch { if (!controller.signal.aborted) setNotice("The acquisition workbench is unavailable. Check the operator session, matching backend migration and workspace connection. Existing tested approvals remain accessible in their tab."); }
    })(); return () => controller.abort();
  }, [workspaceId, version]);
  return initial ? <WorkspaceTools key={`${workspaceId}:${initial.operations.principalId}:${version}`} scope={{ workspaceId, principalId: initial.operations.principalId }} initialOperations={initial.operations} initialRecipes={initial.recipes} initialOperationId={initialOperationId} invalidateScope={invalidate} /> :
    <section className={styles.notice}><p role="status">{notice}</p><button type="button" onClick={invalidate}>Reload acquisition workbench</button></section>;
}
