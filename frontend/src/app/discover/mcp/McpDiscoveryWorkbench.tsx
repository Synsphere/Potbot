"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { UUID, MCP_KINDS, projectMcpBindings, projectMcpBindingEnvelope, projectMcpEntries, projectMcpAttempts, projectMcpAttemptEnvelope, projectMcpReferenceEnvelope, projectMcpMutation,
  type McpBinding, type McpEntry, type McpAttempt, type McpReference, type McpPage, type McpDecision } from "../../../lib/mcp-discovery.ts";
import { MCP_DISCOVERY_ENDPOINT, MCP_ATTEMPT_LABELS, mcpDiscoveryScopeInvalidated, mcpDiscoveryJournalScope, mcpAttemptRoute, newMcpDiscoveryAttempt, validateMcpAttempt,
  mcpAttemptNeedsMonitoring, mcpReferenceCurrent } from "../../../lib/mcp-discovery-client.ts";
import { WorkshopRequests, recoverAttempt, retainAttempt, clearAttempt, clearRejectedAttempt, type WorkshopAttempt, type RequestTicket } from "../../../lib/skill-workshop-client.ts";
import { BindingReview, AttemptReview, EntryReview, readable } from "./McpDiscoveryViews.tsx";
import styles from "../../skills/skills.module.css";

const headers = (workspaceId: string) => ({ "X-Potbot-Workspace": workspaceId });
const VISIBLE_LIMIT = 200, MAX_POLLS = 30;

function ScopedWorkbench({ initial, initialBindingId, invalidateScope }: { initial: McpPage<McpBinding>; initialBindingId: string | null; invalidateScope: () => void }) {
  const scope = initial;
  const [bindings, setBindings] = useState(initial), [listCurrent, setListCurrent] = useState(true), [listBusy, setListBusy] = useState(false), [listNotice, setListNotice] = useState("");
  const [selected, setSelected] = useState<string | null>(initialBindingId), [binding, setBinding] = useState<McpBinding | null>(null), [bindingCurrent, setBindingCurrent] = useState(false), [detailNotice, setDetailNotice] = useState("");
  const [entries, setEntries] = useState<McpPage<McpEntry> | null>(null), [entryCurrent, setEntryCurrent] = useState(false), [entryBusy, setEntryBusy] = useState(false), [entryNotice, setEntryNotice] = useState(""), [entry, setEntry] = useState<McpEntry | null>(null);
  const [attempts, setAttempts] = useState<McpPage<McpAttempt> | null>(null), [attemptListBusy, setAttemptListBusy] = useState(false), [attemptListNotice, setAttemptListNotice] = useState(""), [attempt, setAttempt] = useState<McpAttempt | null>(null), [attemptCurrent, setAttemptCurrent] = useState(false);
  const [reference, setReference] = useState<McpReference | null>(null), [referenceBusy, setReferenceBusy] = useState(false), [referenceNotice, setReferenceNotice] = useState("");
  const [query, setQuery] = useState(""), [kind, setKind] = useState(""), [openId, setOpenId] = useState(""), [filterDirty, setFilterDirty] = useState(false);
  const [pending, setPending] = useState<WorkshopAttempt | null>(null), [recovered, setRecovered] = useState(false), [durable, setDurable] = useState<boolean | null>(null), [busy, setBusy] = useState(false), [notice, setNotice] = useState("");
  const [live, setLive] = useState(true), [pollNotice, setPollNotice] = useState(""), [visibility, setVisibility] = useState(0), [now, setNow] = useState(Date.now());
  const listRequests = useRef(new WorkshopRequests()), bindingRequests = useRef(new WorkshopRequests()), entryRequests = useRef(new WorkshopRequests()), attemptListRequests = useRef(new WorkshopRequests()), attemptRequests = useRef(new WorkshopRequests()), referenceRequests = useRef(new WorkshopRequests()), mutationRequests = useRef(new WorkshopRequests());
  const selectedRef = useRef<string | null>(initialBindingId), bindingRef = useRef<McpBinding | null>(null), bindingsRef = useRef(initial), entriesRef = useRef<McpPage<McpEntry> | null>(null), attemptsRef = useRef<McpPage<McpAttempt> | null>(null), attemptRef = useRef<McpAttempt | null>(null), entryRef = useRef<McpEntry | null>(null);
  const filters = useRef(new URLSearchParams("limit=25")), pendingRef = useRef<WorkshopAttempt | null>(null), journal = useRef(""), busyRef = useRef(false), pollCount = useRef(0), terminalRefresh = useRef<(identity: string) => void>(() => {});

  const abortReads = useCallback(() => { for (const ref of [listRequests, bindingRequests, entryRequests, attemptListRequests, attemptRequests, referenceRequests]) ref.current.reset(); }, []);
  const leaveScope = useCallback(() => { abortReads(); mutationRequests.current.reset(); invalidateScope(); }, [abortReads, invalidateScope]);
  useEffect(() => {
    journal.current = mcpDiscoveryJournalScope(scope.workspaceId, scope.principalId); const ticket = mutationRequests.current.open();
    void recoverAttempt(journal.current, validateMcpAttempt).then(value => { if (!ticket.current()) return; pendingRef.current = value; setPending(value); setRecovered(true); ticket.finish(); });
    const timer = setInterval(() => setNow(Date.now()), 1000), visible = () => setVisibility(value => value + 1); document.addEventListener("visibilitychange", visible);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange", visible); abortReads(); mutationRequests.current.reset(); };
  }, [scope.workspaceId, scope.principalId, abortReads]);

  const read = useCallback(async (path: string, ticket: RequestTicket): Promise<unknown> => {
    const response = await fetch(MCP_DISCOVERY_ENDPOINT + path, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: headers(scope.workspaceId) });
    if (!ticket.current()) return null;
    if (mcpDiscoveryScopeInvalidated(response.status, null, scope.workspaceId, scope.principalId)) { leaveScope(); return null; }
    if (!response.ok) throw new Error(response.status === 409 ? "The retained catalogue changed. Refresh the binding and restart this page." : "The retained record is unavailable. Refresh before making a decision.");
    const value = await response.json(); if (!ticket.current()) return null;
    if (mcpDiscoveryScopeInvalidated(response.status, value, scope.workspaceId, scope.principalId)) { leaveScope(); return null; }
    return value;
  }, [scope.workspaceId, scope.principalId, leaveScope]);

  const loadBindings = useCallback((append = false) => {
    const previous = bindingsRef.current, cursor = append ? previous.nextCursor : null; if (append && !cursor) return;
    listRequests.current.reset(); const ticket = listRequests.current.open(); setListBusy(true); if (!append) setListCurrent(false); setListNotice("Reading retained bindings…");
    void (async () => { try {
      const raw = await read("/bindings?limit=25" + (cursor ? "&after=" + cursor : ""), ticket); if (!ticket.current()) return;
      const result = projectMcpBindings(raw, scope.workspaceId, scope.principalId); if (!result || append && result.items.some(item => previous.items.some(old => old.id === item.id))) throw new Error("Invalid or overlapping binding page. Refresh the list.");
      const next = append ? { ...result, items: [...previous.items, ...result.items].slice(0, VISIBLE_LIMIT) } : result;
      bindingsRef.current = next; setBindings(next); setListCurrent(true); setListNotice(next.items.length ? "" : "No MCP binding has been admitted by the configured host.");
      const current = bindingRef.current, observed = result.items.find(item => item.id === selectedRef.current);
      if (current && observed && observed.revision >= current.revision && JSON.stringify(observed) !== JSON.stringify(current)) {
        bindingRef.current = observed; setBinding(observed); setBindingCurrent(false); entryRequests.current.reset(); referenceRequests.current.reset();
        setEntryCurrent(false); setEntryBusy(false); entryRef.current = null; setEntry(null); setReference(null); setReferenceBusy(false);
        setDetailNotice("The selected binding changed. Refresh it before reviewing a new decision or reference.");
      }
    } catch (error) { if (ticket.current()) { setListCurrent(false); setListNotice(error instanceof Error ? error.message : "Bindings are unavailable."); } }
    finally { if (ticket.current()) setListBusy(false); ticket.finish(); } })();
  }, [read, scope.workspaceId, scope.principalId]);

  const loadAttempt = useCallback((identity: string, monitor = false) => {
    attemptRequests.current.reset(); const ticket = attemptRequests.current.open(), owner = selectedRef.current; if (!monitor) setAttemptCurrent(false);
    void (async () => { try {
      const raw = await read("/attempts/" + identity, ticket); if (!ticket.current() || selectedRef.current !== owner) return;
      const result = projectMcpAttemptEnvelope(raw, scope.workspaceId, identity, scope.principalId); if (!result || result.attempt.bindingId !== owner) throw new Error("The attempt belongs to a different binding.");
      const previous = attemptRef.current; attemptRef.current = result.attempt; setAttempt(result.attempt); setAttemptCurrent(true);
      setAttempts(previous => { if (!previous) return previous; const next = { ...previous, items: previous.items.map(item => item.id === identity ? result.attempt : item) }; attemptsRef.current = next; return next; });
      if (!mcpAttemptNeedsMonitoring(result.attempt)) {
        const changed = previous?.id === identity && mcpAttemptNeedsMonitoring(previous); setLive(false);
        setPollNotice(changed ? "The attempt is terminal. Current binding and catalogue data are being reread." : "The retained attempt is terminal. Refresh the binding to inspect its current catalogue.");
        if (changed) terminalRefresh.current(result.attempt.bindingId);
      }
    } catch (error) { if (ticket.current()) { setAttemptCurrent(false); setLive(false); setPollNotice(error instanceof Error ? error.message : "Attempt status unavailable."); } }
    finally { ticket.finish(); } })();
  }, [read, scope.workspaceId, scope.principalId]);

  const loadAttemptList = useCallback((append = false) => {
    const owner = selectedRef.current; if (!owner) return; const previous = attemptsRef.current, cursor = append ? previous?.nextCursor : null; if (append && !cursor) return;
    attemptListRequests.current.reset(); const ticket = attemptListRequests.current.open(); setAttemptListBusy(true); setAttemptListNotice("Reading attempt history…");
    void (async () => { try {
      const raw = await read(`/attempts?bindingId=${owner}&limit=25${cursor ? "&after=" + cursor : ""}`, ticket); if (!ticket.current() || selectedRef.current !== owner) return;
      const result = projectMcpAttempts(raw, scope.workspaceId, scope.principalId, owner); if (!result || append && result.items.some(item => previous?.items.some(old => old.id === item.id))) throw new Error("Invalid or overlapping attempt page. Refresh history.");
      const next = append && previous ? { ...result, items: [...previous.items, ...result.items].slice(0, VISIBLE_LIMIT) } : result; attemptsRef.current = next; setAttempts(next); setAttemptListNotice(next.items.length ? "" : "No reconciliation attempts retained for this binding.");
    } catch (error) { if (ticket.current()) setAttemptListNotice(error instanceof Error ? error.message : "Attempt history unavailable."); }
    finally { if (ticket.current()) setAttemptListBusy(false); ticket.finish(); } })();
  }, [read, scope.workspaceId, scope.principalId]);

  const loadEntries = useCallback((append = false) => {
    const selectedBinding = bindingRef.current; if (!selectedBinding?.snapshotId || selectedRef.current !== selectedBinding.id) return;
    const previous = entriesRef.current, cursor = append ? previous?.nextCursor : null; if (append && !cursor) return;
    entryRequests.current.reset(); referenceRequests.current.reset(); const ticket = entryRequests.current.open(), owner = selectedBinding.id, snapshot = selectedBinding.snapshotId;
    setEntryBusy(true); setEntryCurrent(false); setEntryNotice("Reading the retained catalogue…"); setReference(null); setReferenceNotice(""); setReferenceBusy(false);
    const params = new URLSearchParams(filters.current); if (cursor) params.set("after", cursor);
    void (async () => { try {
      const raw = await read(`/bindings/${owner}/entries?${params}`, ticket); if (!ticket.current() || selectedRef.current !== owner || bindingRef.current?.snapshotId !== snapshot) return;
      const result = projectMcpEntries(raw, scope.workspaceId, owner, scope.principalId, snapshot); if (!result || result.items.some(item => item.snapshotSha256 !== selectedBinding.snapshotSha256 || params.has("kind") && item.kind !== params.get("kind")) || append && result.items.some(item => previous?.items.some(old => old.id === item.id))) throw new Error("The catalogue binding changed. Refresh the binding and restart this page.");
      const next = append && previous ? { ...result, items: [...previous.items, ...result.items].slice(0, VISIBLE_LIMIT) } : result;
      entriesRef.current = next; setEntries(next); setEntryCurrent(true); setEntryNotice(next.items.length ? "" : "No retained interfaces match these filters.");
      if (entryRef.current) { const item = next.items.find(item => item.id === entryRef.current?.id) ?? null; entryRef.current = item; setEntry(item); }
    } catch (error) { if (ticket.current()) setEntryNotice(error instanceof Error ? error.message : "Catalogue unavailable."); }
    finally { if (ticket.current()) setEntryBusy(false); ticket.finish(); } })();
  }, [read, scope.workspaceId, scope.principalId]);

  const loadBinding = useCallback((identity: string) => {
    bindingRequests.current.reset(); entryRequests.current.reset(); referenceRequests.current.reset(); const ticket = bindingRequests.current.open(); setBindingCurrent(false); setEntryCurrent(false); setEntryBusy(false); setReference(null); setReferenceBusy(false); setDetailNotice("Reading the exact binding…");
    void (async () => { try {
      const raw = await read("/bindings/" + identity, ticket); if (!ticket.current() || selectedRef.current !== identity) return;
      const result = projectMcpBindingEnvelope(raw, scope.workspaceId, identity, scope.principalId); if (!result) throw new Error("The binding response is invalid or changed scope.");
      if (bindingRef.current && result.binding.revision < bindingRef.current.revision) throw new Error("A newer binding revision is already retained. Refresh its current state.");
      const previous = bindingRef.current; bindingRef.current = result.binding; setBinding(result.binding); setBindingCurrent(true); setDetailNotice("");
      setBindings(previous => { const next = { ...previous, items: previous.items.map(item => item.id === identity ? result.binding : item) }; bindingsRef.current = next; return next; });
      if (previous?.snapshotId !== result.binding.snapshotId) { entriesRef.current = null; setEntries(null); entryRef.current = null; setEntry(null); }
      if (result.binding.snapshotId) loadEntries(); else { entriesRef.current = null; setEntries(null); setEntryNotice("The host has not retained a catalogue for this binding."); }
      const attemptId = result.binding.activeAttemptId ?? result.binding.lastAttemptId;
      if (attemptId && attemptRef.current === null) loadAttempt(attemptId);
    } catch (error) { if (ticket.current()) setDetailNotice(error instanceof Error ? error.message : "Binding unavailable."); }
    finally { ticket.finish(); } })();
  }, [read, scope.workspaceId, scope.principalId, loadEntries, loadAttempt]);
  useEffect(() => { terminalRefresh.current = identity => { if (identity === selectedRef.current) { loadBinding(identity); loadAttemptList(); } }; }, [loadBinding, loadAttemptList]);

  const select = useCallback((identity: string) => {
    for (const ref of [bindingRequests, entryRequests, attemptListRequests, attemptRequests, referenceRequests]) ref.current.reset();
    selectedRef.current = identity; setSelected(identity); setOpenId(identity); bindingRef.current = null; setBinding(null); setBindingCurrent(false); entriesRef.current = null; setEntries(null); entryRef.current = null; setEntry(null);
    attemptsRef.current = null; setAttempts(null); attemptRef.current = null; setAttempt(null); setAttemptCurrent(false); setReference(null); setReferenceNotice(""); setReferenceBusy(false); setEntryBusy(false); setLive(true); pollCount.current = 0; setPollNotice("");
    const url = new URL(window.location.href); url.searchParams.set("workspace", scope.workspaceId); url.searchParams.set("binding", identity); window.history.replaceState(null, "", url);
    loadBinding(identity); loadAttemptList();
  }, [scope.workspaceId, loadBinding, loadAttemptList]);
  useEffect(() => { if (initialBindingId) select(initialBindingId); }, [initialBindingId, select]);

  useEffect(() => {
    if (!live || !attempt || !mcpAttemptNeedsMonitoring(attempt) || busy || document.hidden) return;
    if (pollCount.current >= MAX_POLLS) { setLive(false); setPollNotice("Automatic status reads paused after 30 checks. Resume when the host is ready."); return; }
    const timer = setTimeout(() => { pollCount.current++; loadAttempt(attempt.id, true); }, 3000);
    return () => clearTimeout(timer);
  }, [attempt, live, busy, visibility, loadAttempt]);

  const inspectReference = useCallback(() => {
    const selectedEntry = entryRef.current, selectedBinding = bindingRef.current; if (!selectedEntry || !selectedBinding) return;
    referenceRequests.current.reset(); const ticket = referenceRequests.current.open(); setReferenceBusy(true); setReference(null); setReferenceNotice("Checking the exact current reference…");
    void (async () => { try {
      const raw = await read(`/bindings/${selectedBinding.id}/entries/${selectedEntry.id}/reference`, ticket); if (!ticket.current() || selectedRef.current !== selectedBinding.id || entryRef.current?.id !== selectedEntry.id) return;
      const result = projectMcpReferenceEnvelope(raw, scope.workspaceId, selectedBinding.id, selectedEntry.id, scope.principalId);
      if (!result || !bindingRef.current || !mcpReferenceCurrent(result.reference, selectedEntry, bindingRef.current)) throw new Error("The reference changed or expired. Refresh the binding before inspecting it again.");
      setReference(result.reference); setReferenceNotice("Exact current reference retained for inspection.");
    } catch (error) { if (ticket.current()) setReferenceNotice(error instanceof Error ? error.message : "Reference unavailable."); }
    finally { if (ticket.current()) setReferenceBusy(false); ticket.finish(); } })();
  }, [read, scope.workspaceId, scope.principalId]);

  const submit = useCallback(async (original: WorkshopAttempt, recovering: boolean) => {
    if (busyRef.current || !recovered) return; busyRef.current = true; setBusy(true); abortReads(); setBindingCurrent(false); setEntryCurrent(false); setAttemptCurrent(false); setReference(null);
    const ticket = mutationRequests.current.open(); let stored = original;
    try {
      if (!recovering) { const saved = await retainAttempt(journal.current, original); if (!ticket.current()) return; setDurable(saved); }
      else { stored = (await recoverAttempt(journal.current, validateMcpAttempt)) ?? original; if (stored.key !== original.key || stored.body !== original.body || stored.path !== original.path) throw new Error("The retained request differs from the selected recovery."); }
      if (!ticket.current()) return; pendingRef.current = stored; setPending(stored); setNotice("Submitting the exact reviewed request…");
      const response = await fetch(stored.path, { method: "POST", credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: { ...headers(scope.workspaceId), "Content-Type": "application/json", "Idempotency-Key": stored.key }, body: stored.body });
      if (!ticket.current()) return;
      if (mcpDiscoveryScopeInvalidated(response.status, null, scope.workspaceId, scope.principalId)) { leaveScope(); return; }
      if (!response.ok) {
        if (await clearRejectedAttempt(journal.current, stored.key, response.status, recovering)) { if (!ticket.current()) return; pendingRef.current = null; setPending(null); setNotice("The request was rejected. Refresh the current record and review a new decision."); }
        else setNotice("The outcome is uncertain. Keep this exact request and retry it explicitly to recover its receipt.");
        return;
      }
      const raw = await response.json(); if (!ticket.current()) return;
      if (mcpDiscoveryScopeInvalidated(response.status, raw, scope.workspaceId, scope.principalId)) { leaveScope(); return; }
      const path = mcpAttemptRoute(stored.path), result = path && projectMcpMutation(raw, scope.workspaceId, path, JSON.parse(stored.body), scope.principalId);
      if (!result) throw new Error("The response did not match this exact request. Its original recovery record is retained.");
      await clearAttempt(journal.current, stored.key); if (!ticket.current()) return;
      pendingRef.current = null; setPending(null); setDurable(null); setNotice("Exact request confirmed. Reading its current retained state…");
      if ("attempt" in result && result.attempt.bindingId === selectedRef.current) { attemptRef.current = result.attempt; setAttempt(result.attempt); setAttemptCurrent(true); pollCount.current = 0; setLive(mcpAttemptNeedsMonitoring(result.attempt)); setPollNotice(""); loadAttempt(result.attempt.id); }
    } catch (error) { if (ticket.current()) setNotice(error instanceof Error ? error.message : "The outcome is uncertain. Retain the exact request before retrying."); }
    finally {
      if (ticket.current()) { busyRef.current = false; setBusy(false); loadBindings(); const owner = selectedRef.current; if (owner) { loadBinding(owner); loadAttemptList(); } }
      ticket.finish();
    }
  }, [recovered, abortReads, scope.workspaceId, scope.principalId, leaveScope, loadBindings, loadBinding, loadAttemptList, loadAttempt]);

  const mutate = useCallback((path: string[], body: McpDecision) => {
    if (busyRef.current || pendingRef.current || !recovered) return;
    const attempt = newMcpDiscoveryAttempt(MCP_DISCOVERY_ENDPOINT + "/" + path.join("/"), body); void submit(attempt, false);
  }, [recovered, submit]);
  const locked = busy || pending !== null || !recovered;

  return <>
    {pending && <section className={styles.uncertain} aria-label="Unresolved MCP request"><h3>Recover the original request</h3><p>Keep the same path, body and key to learn whether this decision was committed. Loading this journal does not resubmit it.</p><code>{pending.path}</code><code>{pending.key}</code><details><summary>Inspect original request body</summary><pre>{pending.body}</pre></details>
      {durable === false && <p>Persistent browser storage is unavailable. This tab retains a temporary recovery copy; preserve the displayed request before leaving.</p>}<button disabled={busy || !recovered} onClick={() => void submit(pending, true)}>Retry the identical request</button></section>}
    {notice && <p className={styles.status} role="status">{notice}</p>}
    <div className={styles.layout}><aside className={styles.panel}><header className={styles.panelHeading}><h2>Admitted bindings</h2><p>Read retained installation identities and their latest observations.</p></header>
      <div className={styles.body}><button disabled={listBusy || busy} onClick={() => loadBindings()}>Refresh bindings</button><form className={styles.form} onSubmit={event => { event.preventDefault(); if (UUID.test(openId)) select(openId); }}><label className={styles.field}>Open exact binding ID<input value={openId} onChange={event => setOpenId(event.target.value)} placeholder="Binding UUID" /></label><button disabled={!UUID.test(openId) || busy}>Open retained binding</button></form>{listNotice && <p className={styles.status}>{listNotice}</p>}</div>
      <ul className={styles.cards}>{bindings.items.map(item => <li key={item.id}><button disabled={busy || !listCurrent} className={selected === item.id ? styles.selected : undefined} onClick={() => select(item.id)}><strong>{item.name}</strong><span className={styles.badges}><span className={styles.badge}>{readable(item.state)}</span><span className={styles.badge}>{item.stale ? "Stale" : "Retained snapshot"}</span></span><small>{item.toolName} · {readable(item.transport)}</small></button></li>)}</ul>
      <div className={styles.body}>{bindings.nextCursor && <button disabled={listBusy || busy || !listCurrent || bindings.items.length >= VISIBLE_LIMIT} onClick={() => loadBindings(true)}>Load more bindings</button>}{bindings.items.length >= VISIBLE_LIMIT && <p className={styles.help}>This view holds 200 records. Open an exact binding ID to inspect another retained record.</p>}</div>
    </aside><article className={styles.panel}><div className={styles.body}>{!selected ? <div className={styles.empty}><h2>Select an admitted MCP binding</h2><p>Initial admission is an explicit configured-host action tied to the existing installed descriptor. Registry listings alone do not create a live binding.</p></div> : <>
      <div className={styles.actions}><button disabled={busy} onClick={() => { loadBinding(selected); loadAttemptList(); }}>Refresh selected binding</button></div>{detailNotice && <p className={styles.status} role="status">{detailNotice}</p>}
      {binding && <><BindingReview binding={binding} locked={locked} current={bindingCurrent} now={now} mutate={mutate} />
        <section className={styles.section} aria-label="Retained MCP interfaces"><h3>Retained interfaces</h3><form className={styles.form} onSubmit={event => { event.preventDefault(); if (Array.from(query).length > 256 || /[\u0000-\u001f\u007f]/.test(query)) return;
          filters.current = new URLSearchParams({ limit: "25", ...(query ? { q: query } : {}), ...(kind ? { kind } : {}) }); setFilterDirty(false); entryRef.current = null; setEntry(null); loadEntries();
        }}><div className={styles.pair}><label className={styles.field}>Search retained names and descriptions<input value={query} onChange={event => { setQuery(event.target.value); setFilterDirty(true); entryRequests.current.reset(); referenceRequests.current.reset(); setEntryCurrent(false); setReference(null); }} /></label><label className={styles.field}>Interface kind<select value={kind} onChange={event => { setKind(event.target.value); setFilterDirty(true); entryRequests.current.reset(); referenceRequests.current.reset(); setEntryCurrent(false); setReference(null); }}><option value="">All interfaces</option>{MCP_KINDS.map(value => <option key={value} value={value}>{readable(value)}</option>)}</select></label></div><button disabled={busy || !bindingCurrent || !binding.snapshotId || Array.from(query).length > 256}>Search retained catalogue</button></form>
          {filterDirty && <p className={styles.help}>Apply the changed filters to restart pagination.</p>}{entryNotice && <p className={styles.status} role="status">{entryNotice}</p>}
          {entries && <div className={styles.tableScroll}><table className={styles.table}><thead><tr><th>Interface</th><th>Kind</th><th>Reference</th><th>Review</th></tr></thead><tbody>{entries.items.map(item => <tr key={item.id}><td><strong>{item.name}</strong><p>{item.description.slice(0, 200)}</p></td><td>{readable(item.kind)}</td><td>{item.referenceAvailable ? "Available under current binding" : "Unavailable"}</td><td><button disabled={!entryCurrent || filterDirty || busy} onClick={() => { referenceRequests.current.reset(); entryRef.current = item; setEntry(item); setReference(null); setReferenceNotice(""); setReferenceBusy(false); }}>Inspect</button></td></tr>)}</tbody></table></div>}
          {entries?.nextCursor && <div className={styles.actions}><button disabled={entryBusy || busy || !entryCurrent || filterDirty || entries.items.length >= VISIBLE_LIMIT} onClick={() => loadEntries(true)}>Load more interfaces</button></div>}
          {entries && entries.items.length >= VISIBLE_LIMIT && <p className={styles.help}>This view holds 200 definitions. Narrow the retained search to inspect additional interfaces.</p>}
          {entry && <EntryReview key={entry.id} entry={entry} binding={binding} reference={reference} now={now} current={bindingCurrent && entryCurrent && !filterDirty} busy={referenceBusy || busy} notice={referenceNotice} inspectReference={inspectReference} />}
        </section>
        <section className={styles.section} aria-label="Reconciliation history"><h3>Reconciliation history</h3><button disabled={attemptListBusy || busy} onClick={() => loadAttemptList()}>Refresh history</button>{attemptListNotice && <p className={styles.status}>{attemptListNotice}</p>}
          <ul className={styles.history}>{attempts?.items.map(item => <li key={item.id}><div className={styles.row}><strong>{MCP_ATTEMPT_LABELS[item.status]}</strong><button disabled={busy} onClick={() => { attemptRef.current = null; setAttempt(null); pollCount.current = 0; setLive(true); setPollNotice(""); loadAttempt(item.id); }}>Inspect attempt</button></div><time>{new Date(item.createdAt).toLocaleString()}</time><p><code>{item.id}</code></p></li>)}</ul>
          {attempts?.nextCursor && <button disabled={attemptListBusy || busy || attempts.items.length >= VISIBLE_LIMIT} onClick={() => loadAttemptList(true)}>Load more attempts</button>}
        </section>
        {attempt && <><AttemptReview attempt={attempt} locked={locked} current={attemptCurrent} mutate={mutate} /><div className={styles.actions}><button disabled={busy} onClick={() => loadAttempt(attempt.id)}>Refresh attempt</button>{mcpAttemptNeedsMonitoring(attempt) && <button onClick={() => { pollCount.current = 0; setLive(value => !value); setPollNotice(""); }}>{live ? "Pause automatic status reads" : "Resume bounded status reads"}</button>}</div>{pollNotice && <p className={styles.status} role="status">{pollNotice}</p>}</>}
      </>}
    </>}</div></article></div>
  </>;
}

export default function McpDiscoveryWorkbench({ workspaceId, initialBindingId }: { workspaceId: string; initialBindingId: string | null }) {
  const [initial, setInitial] = useState<McpPage<McpBinding> | null>(null), [notice, setNotice] = useState("Loading retained MCP bindings…"), [reload, setReload] = useState(0), [paused, setPaused] = useState(false);
  const invalidateScope = useCallback(() => { setInitial(null); setPaused(true); setNotice("The authenticated scope changed or access was denied. Reopen the workspace before inspecting private records."); }, []);
  useEffect(() => {
    if (paused) return; const request = new AbortController(); setInitial(null); setNotice("Loading retained MCP bindings…");
    void fetch(MCP_DISCOVERY_ENDPOINT + "/bindings?limit=25", { credentials: "same-origin", cache: "no-store", headers: headers(workspaceId), signal: request.signal }).then(async response => {
      if (!response.ok) throw new Error("unavailable"); const raw = await response.json(); if (request.signal.aborted) return; const result = projectMcpBindings(raw, workspaceId);
      if (!result) throw new Error("invalid"); setInitial(result); setNotice("");
    }).catch(() => { if (!request.signal.aborted) setNotice("Retained MCP discovery is unavailable. Check this workspace’s operator access and backend configuration."); });
    return () => request.abort();
  }, [workspaceId, reload, paused]);
  return initial ? <ScopedWorkbench key={`${initial.workspaceId}:${initial.principalId}`} initial={initial} initialBindingId={initialBindingId} invalidateScope={invalidateScope} /> : <section className={styles.panel}><div className={styles.body}><p className={styles.status} role="status">{notice}</p><button onClick={() => { setPaused(false); setReload(value => value + 1); }}>Reload authorized bindings</button></div></section>;
}
