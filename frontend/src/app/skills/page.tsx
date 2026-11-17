"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  MAX_ARCHIVE_BYTES, UUID, projectPackage, projectPackageList, projectSkill, projectSkillList, projectSkillMutation, validateSkillMutation,
  type AcquiredSkill, type PackageEnvelope, type PackageList, type SkillList, type SkillPackage,
} from "../../lib/skill-acquisition.ts";
import {
  WorkshopRequests, base64Bytes, clearAttempt, clearRejectedAttempt, newAttempt, recoverAttempt, retainAttempt,
  validateWorkshopAttempt, workshopJournalScope, type WorkshopAttempt,
} from "../../lib/skill-workshop-client.ts";
import { InstalledView, PackageView, availability, headers, short, size, type Mutate, type WorkshopScope } from "./SkillViews.tsx";
import styles from "./skills.module.css";

type Workspace = { id: string; name: string };
type Selection = { kind: "package" | "skill"; id: string; generation: number };
const endpoint = "/api/ui/skills";

function ImportForm({ disabled, mutate }: { disabled: boolean; mutate: Mutate }) {
  const [kind, setKind] = useState<"zip" | "github">("zip"), [file, setFile] = useState<File | null>(null);
  const [repository, setRepository] = useState(""), [commit, setCommit] = useState(""), [path, setPath] = useState("");
  const [notice, setNotice] = useState(""), [preparing, setPreparing] = useState(false); const requests = useRef(new WorkshopRequests());
  useEffect(() => () => requests.current.reset(), []);
  useEffect(() => { if (disabled) { requests.current.reset(); setPreparing(false); } }, [disabled]);
  function invalidate() { requests.current.reset(); setPreparing(false); setNotice(""); }
  function submit(event: FormEvent) {
    event.preventDefault(); if (disabled || preparing) return;
    requests.current.reset(); const ticket = requests.current.open(); setPreparing(true); setNotice("Preparing the exact source…");
    void (async () => {
      try {
        let body: Record<string, unknown>;
        if (kind === "zip") {
          if (!file || file.size < 1 || file.size > MAX_ARCHIVE_BYTES) throw new Error("Choose a ZIP file of up to 4 MiB.");
          const bytes = new Uint8Array(await file.arrayBuffer()); if (!ticket.current()) return;
          if (bytes.length !== file.size || bytes.length > MAX_ARCHIVE_BYTES) throw new Error("The ZIP changed while it was being read.");
          body = { archiveBase64: base64Bytes(bytes), subdirectory: path };
        } else body = { repository, commit, path };
        if (!validateSkillMutation(["imports", kind], body)) throw new Error(kind === "github" ? "Use an exact https://github.com/owner/repository URL, a 40-character lowercase commit, and a relative package path." : "Use a relative package directory with no traversal or unsafe file names.");
        if (!ticket.current()) return;
        mutate(`${endpoint}/imports/${kind}`, body); setNotice("");
      } catch (error) { if (ticket.current()) setNotice(error instanceof Error ? error.message : "The source could not be prepared."); }
      finally { if (ticket.current()) setPreparing(false); ticket.finish(); }
    })();
  }
  return <section className={styles.body} aria-label="Import a skill package"><h3>Import a source</h3>
    <p className={styles.help}>Import privately, inspect the complete package, then approve an exact installation plan.</p>
    <div className={styles.tabs}><button type="button" aria-pressed={kind === "zip"} disabled={disabled} onClick={() => { invalidate(); setKind("zip"); setPath(""); }}>ZIP file</button>
      <button type="button" aria-pressed={kind === "github"} disabled={disabled} onClick={() => { invalidate(); setKind("github"); setPath(""); }}>Pinned GitHub source</button></div>
    <form onSubmit={submit} className={styles.form}><fieldset disabled={disabled || preparing} className={styles.form}>
      {kind === "zip" ? <label className={styles.field}>ZIP archive · maximum 4 MiB<input type="file" accept=".zip,application/zip" onChange={event => { invalidate(); setFile(event.target.files?.[0] ?? null); }} />
        {file && <span className={styles.help}>{file.name} · {size(file.size)}</span>}</label> : <>
        <label className={styles.field}>Public GitHub repository<input type="url" required placeholder="https://github.com/owner/repository" value={repository} onChange={event => { invalidate(); setRepository(event.target.value); }} autoComplete="off" spellCheck={false} /></label>
        <label className={styles.field}>Exact commit SHA<input required pattern="[a-f0-9]{40}" minLength={40} maxLength={40} placeholder="40 lowercase hexadecimal characters" value={commit} onChange={event => { invalidate(); setCommit(event.target.value); }} autoComplete="off" spellCheck={false} /></label>
      </>}
      <label className={styles.field}>Package directory · optional<input placeholder="skills/example" value={path} maxLength={512} onChange={event => { invalidate(); setPath(event.target.value); }} autoComplete="off" spellCheck={false} />
        <span className={styles.help}>Leave blank for SKILL.md at the archive or repository root. Only an unambiguous package is accepted.</span></label>
      <button type="submit" className={styles.primary} disabled={kind === "zip" && !file}>{preparing ? "Preparing source…" : "Import for private review"}</button>
    </fieldset><p className={styles.status} role="status">{notice}</p></form>
  </section>;
}

function Workshop({ scope, initialSkills, initialPackages, invalidateScope }: { scope: WorkshopScope; initialSkills: SkillList; initialPackages: PackageList; invalidateScope: () => void }) {
  const [skills, setSkills] = useState(initialSkills), [packages, setPackages] = useState(initialPackages);
  const [selection, setSelection] = useState<Selection | null>(null), [selectedPackage, setSelectedPackage] = useState<PackageEnvelope | null>(null), [selectedSkill, setSelectedSkill] = useState<AcquiredSkill | null>(null);
  const [notice, setNotice] = useState(""), [detailNotice, setDetailNotice] = useState(""), [listsBusy, setListsBusy] = useState(false), [detailBusy, setDetailBusy] = useState(false);
  const [pending, setPending] = useState<WorkshopAttempt | null>(null), [durable, setDurable] = useState<boolean | null>(null), [busy, setBusy] = useState(false), [recovered, setRecovered] = useState(false), [revision, setRevision] = useState(0);
  const listRequests = useRef(new WorkshopRequests()), selectionRequests = useRef(new WorkshopRequests()), mutationRequests = useRef(new WorkshopRequests());
  const pendingRef = useRef<WorkshopAttempt | null>(null), busyRef = useRef(false), journal = useRef(""), selectionGeneration = useRef(0);
  const [tab, setTab] = useState<"installed" | "packages">("installed");
  useEffect(() => {
    journal.current = workshopJournalScope(scope.workspaceId, scope.principalId);
    const ticket = mutationRequests.current.open();
    void recoverAttempt(journal.current, validateWorkshopAttempt).then(attempt => {
      if (!ticket.current()) return; pendingRef.current = attempt; setPending(attempt); setRecovered(true); ticket.finish();
    });
    return () => { listRequests.current.reset(); selectionRequests.current.reset(); mutationRequests.current.reset(); };
  }, [scope.workspaceId, scope.principalId]);
  const leaveScope = useCallback(() => {
    listRequests.current.reset(); selectionRequests.current.reset(); mutationRequests.current.reset(); invalidateScope();
  }, [invalidateScope]);
  const select = useCallback((kind: Selection["kind"], id: string) => {
    selectionGeneration.current++;
    selectionRequests.current.reset(); setSelectedPackage(null); setSelectedSkill(null); setDetailNotice("");
    setSelection(prior => ({ kind, id, generation: (prior?.generation ?? 0) + 1 }));
  }, []);

  const refresh = useCallback(() => {
    listRequests.current.reset(); const ticket = listRequests.current.open(); setListsBusy(true); setNotice("Refreshing authorized packages and skills…");
    void (async () => {
      try {
        const responses = await Promise.all(["", "/packages"].map(suffix => fetch(`${endpoint}${suffix}?limit=25`, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: headers(scope.workspaceId) })));
        if (!ticket.current()) return;
        if (responses.some(response => response.status === 401 || response.status === 403)) { leaveScope(); return; }
        if (responses.some(response => !response.ok)) throw new Error("unavailable");
        const [rawSkills, rawPackages] = await Promise.all(responses.map(response => response.json()));
        if (!ticket.current()) return;
        const first = projectSkillList(rawSkills, scope.workspaceId), second = projectPackageList(rawPackages, scope.workspaceId);
        if (first?.principalId !== scope.principalId || second?.principalId !== scope.principalId) { leaveScope(); return; }
        if (!first || !second) throw new Error("invalid");
        setSkills(first); setPackages(second); setNotice(""); setRevision(value => value + 1);
      } catch { if (ticket.current()) setNotice("The workspace lists could not be refreshed. Existing observations may be stale; refresh before applying another change."); }
      finally { if (ticket.current()) setListsBusy(false); ticket.finish(); }
    })();
  }, [scope.workspaceId, scope.principalId, leaveScope]);

  function loadMore(kind: "installed" | "packages") {
    const cursor = kind === "installed" ? skills.nextCursor : packages.nextCursor; if (!cursor || listsBusy) return;
    const ticket = listRequests.current.open(); setListsBusy(true); setNotice("Loading another bounded page…");
    void (async () => {
      try {
        const response = await fetch(`${endpoint}${kind === "packages" ? "/packages" : ""}?limit=25&after=${encodeURIComponent(cursor)}`, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: headers(scope.workspaceId) });
        if (!ticket.current()) return; if (!response.ok) throw new Error("unavailable");
        const raw = await response.json(); if (!ticket.current()) return;
        if (kind === "installed") {
          const result = projectSkillList(raw, scope.workspaceId, scope.principalId); if (!result || result.nextCursor === cursor) throw new Error("invalid");
          setSkills(prior => ({ ...result, items: [...new Map([...prior.items, ...result.items].map(item => [item.id, item])).values()] }));
        } else {
          const result = projectPackageList(raw, scope.workspaceId, scope.principalId); if (!result || result.nextCursor === cursor) throw new Error("invalid");
          setPackages(prior => ({ ...result, items: [...new Map([...prior.items, ...result.items].map(item => [item.id, item])).values()] }));
        }
        setNotice("");
      } catch { if (ticket.current()) setNotice("The next page could not be read in this scope. Refresh the workspace lists."); }
      finally { if (ticket.current()) setListsBusy(false); ticket.finish(); }
    })();
  }

  useEffect(() => {
    if (!selection) return;
    selectionRequests.current.reset(); const ticket = selectionRequests.current.open(); setDetailBusy(true); setDetailNotice("");
    setSelectedPackage(null); setSelectedSkill(null);
    void (async () => {
      try {
        const response = await fetch(`${endpoint}/${selection.kind === "package" ? "packages/" : ""}${selection.id}`, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: headers(scope.workspaceId) });
        if (!ticket.current()) return; if (!response.ok) throw new Error("unavailable");
        const raw = await response.json(); if (!ticket.current()) return;
        const result = selection.kind === "package" ? projectPackage(raw, scope.workspaceId, selection.id, scope.principalId) : projectSkill(raw, scope.workspaceId, selection.id, scope.principalId);
        if (!result) throw new Error("invalid");
        if ("package" in result) setSelectedPackage(result); else setSelectedSkill(result.skill);
      } catch { if (ticket.current()) setDetailNotice("This entry is unavailable in the current workspace and principal scope. Refresh the list or select it again."); }
      finally { if (ticket.current()) setDetailBusy(false); ticket.finish(); }
    })();
    return () => selectionRequests.current.reset();
  }, [selection, scope.workspaceId, scope.principalId]);

  function send(attempt: WorkshopAttempt, retry = false) {
    if (busyRef.current || !recovered || (pendingRef.current && (!retry || pendingRef.current.key !== attempt.key))) return;
    if (!validateWorkshopAttempt(attempt)) { setNotice("The saved action does not match a supported exact skill request."); return; }
    busyRef.current = true; pendingRef.current = attempt; setBusy(true); setPending(attempt); setNotice("Retaining the exact request for recovery…");
    const ticket = mutationRequests.current.open(), journalScope = journal.current, selectedGeneration = selectionGeneration.current;
    void (async () => {
      try {
        const stored = await retainAttempt(journalScope, attempt); if (!ticket.current()) return; setDurable(stored); setNotice("Submitting the exact reviewed action…");
        const response = await fetch(attempt.path, { method: "POST", credentials: "same-origin", cache: "no-store", signal: ticket.signal,
          headers: { ...headers(scope.workspaceId), "Content-Type": "application/json", "Idempotency-Key": attempt.key }, body: attempt.body });
        if (!ticket.current()) return;
        if ([400, 401, 403, 404, 409, 413, 422].includes(response.status)) {
          const cleared = await clearRejectedAttempt(journalScope, attempt.key, response.status, retry); if (!ticket.current()) return;
          if (!cleared) { setNotice("The recovery attempt was rejected, but the earlier action may already have committed. The original key and body are retained. Restore operator authorization or the workspace connection, then retry this exact request."); return; }
          pendingRef.current = null; setPending(null); setDurable(null); setRevision(value => value + 1);
          setNotice(response.status === 409 ? "The source, review plan or control revision changed. Refresh and review an exact new plan." : response.status === 401 || response.status === 403 ?
            "This action was not authorized. Check your operator session and the server's setup capability." : "The skill action was rejected before completion. Review the exact source, package and current revision.");
          if (selection && selectionGeneration.current === selectedGeneration) select(selection.kind, selection.id);
          return;
        }
        if (!response.ok) throw new Error("unconfirmed");
        const raw = await response.json(); if (!ticket.current()) return;
        const result = await projectSkillMutation(raw, scope.workspaceId, attempt.path.slice(endpoint.length + 1).split("/"), JSON.parse(attempt.body), scope.principalId);
        if (!ticket.current()) return;
        if (!result || ("skill" in result && !result.skill.owned)) throw new Error("unconfirmed");
        await clearAttempt(journalScope, attempt.key); if (!ticket.current()) return;
        pendingRef.current = null; setPending(null); setDurable(null); setNotice("The exact action was recorded. Reading its current state…");
        refresh();
        if (selectionGeneration.current === selectedGeneration) {
          if ("package" in result) { setTab("packages"); select("package", result.package.id); } else { setTab("installed"); select("skill", result.skill.id); }
        }
      } catch { if (ticket.current()) setNotice("The action's outcome could not be confirmed. Use the retained request below to recover the original receipt."); }
      finally { if (ticket.current()) { busyRef.current = false; setBusy(false); } ticket.finish(); }
    })();
  }
  const mutate: Mutate = (path, body) => send(newAttempt(path, body));
  const locked = !recovered || busy || pending !== null;
  const entries: (AcquiredSkill | SkillPackage)[] = tab === "installed" ? skills.items : packages.items;
  return <>
    <div className={styles.actions}><button type="button" disabled={listsBusy || busy} onClick={() => { refresh(); if (selection) select(selection.kind, selection.id); }}>Refresh workspace</button>
      <span className={styles.help}>{skills.items.length} installed shown · {packages.items.length} private imports shown</span></div>
    <p className={styles.status} role="status">{recovered ? notice : "Checking for an unresolved request in this tab…"}</p>
    {pending && <section className={styles.uncertain} aria-label="Unresolved skill request"><h3>{busy ? "Recording this request" : "Recover an unresolved request"}</h3>
      <p>{busy ? "Keep this page open while the server records the action." : "A response did not establish the outcome. Retry sends the same source, body and idempotency key and recovers the original result if it already committed."}</p>
      <code>{pending.key}</code><p className={styles.help}>Action: {pending.path.replace(endpoint + "/", "")}</p>
      {durable === false && <p className={styles.warning}>Browser recovery storage is unavailable. The exact request is retained only in this open tab; keep it open until the action is confirmed.</p>}
      <button type="button" disabled={busy} onClick={() => send(pending, true)}>Retry exact request</button>
    </section>}
    <div className={styles.layout}><aside className={styles.panel}><div className={styles.panelHeading}><h2>Workspace skills</h2><p>Private imports and the installed data library.</p></div>
      <ImportForm disabled={locked} mutate={mutate} />
      <div className={styles.body}><div className={styles.tabs}><button type="button" aria-pressed={tab === "installed"} onClick={() => setTab("installed")}>Installed skills</button>
        <button type="button" aria-pressed={tab === "packages"} onClick={() => setTab("packages")}>My imports</button></div></div>
      <ul className={styles.cards}>{entries.map(item => <li key={item.id}><button type="button" className={selection?.id === item.id ? styles.selected : undefined} onClick={() => select(tab === "installed" ? "skill" : "package", item.id)}>
        <span className={styles.row}><strong>{item.name}</strong><span className={styles.badge}>{"owned" in item ? availability(item) : "Private import"}</span></span>
        <small>{"owned" in item ? `${item.owned ? "Owned by you" : "Shared"} · revision ${item.revision}` : `${item.files} files · ${size(item.bytes)}`}</small><code>{short(item.releaseSha256)}</code></button></li>)}</ul>
      {entries.length === 0 && <p className={styles.empty}>{tab === "installed" ? "No installed skills are visible in this workspace." : "Import a ZIP or pinned GitHub package to begin a review."}</p>}
      {(tab === "installed" ? skills.nextCursor : packages.nextCursor) && <div className={styles.body}><button type="button" disabled={listsBusy} onClick={() => loadMore(tab)}>Load more {tab === "installed" ? "skills" : "imports"}</button></div>}
    </aside><section className={styles.panel} aria-label="Selected skill or import" aria-busy={detailBusy}>
      {detailBusy ? <div className={styles.empty} role="status">Reading the selected entry…</div> : detailNotice ? <div className={styles.empty} role="status">{detailNotice}</div> : selectedPackage ?
        <PackageView key={`package:${selectedPackage.package.id}:${selection?.generation}`} scope={scope} value={selectedPackage} installed={skills.items} disabled={locked} mutate={mutate} revision={revision} /> : selectedSkill ?
        <InstalledView key={`skill:${selectedSkill.id}:${selectedSkill.revision}:${selection?.generation}`} scope={scope} skill={selectedSkill} disabled={locked} mutate={mutate} onRefresh={() => select("skill", selectedSkill.id)} onReviewPackage={() => { setTab("packages"); select("package", selectedSkill.packageId); }} /> :
        <div className={styles.empty}><span className={styles.eyebrow}>IMPORT · REVIEW · ACTIVATE</span><h2>A complete package, with an exact identity</h2><p>Select an installed skill or import a new source to inspect its instructions, resources and declared requirements. An approved installation starts disabled.</p></div>}
    </section></div>
  </>;
}

function WorkspaceWorkshop({ workspaceId }: { workspaceId: string }) {
  const [initial, setInitial] = useState<{ skills: SkillList; packages: PackageList } | null>(null), [notice, setNotice] = useState("Loading scoped skill records…"), [refresh, setRefresh] = useState(0);
  const invalidate = useCallback(() => { setInitial(null); setRefresh(value => value + 1); }, []);
  useEffect(() => {
    const controller = new AbortController(); setInitial(null); setNotice("Loading scoped skill records…");
    void (async () => {
      try {
        const responses = await Promise.all(["", "/packages"].map(suffix => fetch(`${endpoint}${suffix}?limit=25`, { credentials: "same-origin", cache: "no-store", signal: controller.signal, headers: headers(workspaceId) })));
        if (controller.signal.aborted) return; if (responses.some(response => !response.ok)) throw new Error("unavailable");
        const [first, second] = await Promise.all(responses.map(response => response.json())); if (controller.signal.aborted) return;
        const skills = projectSkillList(first, workspaceId), packages = projectPackageList(second, workspaceId, skills?.principalId);
        if (!skills || !packages || skills.principalId !== packages.principalId) throw new Error("invalid");
        setInitial({ skills, packages }); setNotice("");
      } catch { if (!controller.signal.aborted) setNotice("The skill workshop is unavailable. Check the operator session and workspace backend, then reload."); }
    })(); return () => controller.abort();
  }, [workspaceId, refresh]);
  return initial ? <Workshop key={`${workspaceId}:${initial.skills.principalId}:${refresh}`} scope={{ workspaceId, principalId: initial.skills.principalId }} initialSkills={initial.skills} initialPackages={initial.packages} invalidateScope={invalidate} /> :
    <div className={styles.notice}><p role="status">{notice}</p><button type="button" onClick={invalidate}>Reload workshop</button></div>;
}

export default function SkillWorkshopPage() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]), [workspaceId, setWorkspaceId] = useState(""), [notice, setNotice] = useState("Loading authorized workspaces…");
  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/ui/bootstrap", { credentials: "same-origin", cache: "no-store", signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error("unavailable"); const raw = await response.json(); if (controller.signal.aborted) return;
      if (!Array.isArray(raw.workspaces) || raw.workspaces.length > 12) throw new Error("invalid");
      const known: Workspace[] = raw.workspaces.map((value: unknown) => {
        if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid"); const row = value as Record<string, unknown>;
        if (typeof row.id !== "string" || !UUID.test(row.id) || typeof row.name !== "string" || !row.name.trim() || row.name.length > 80) throw new Error("invalid");
        return { id: row.id, name: row.name };
      });
      if (new Set(known.map(item => item.id)).size !== known.length) throw new Error("invalid");
      setWorkspaces(known); const requested = new URLSearchParams(window.location.search).get("workspace"), selected = requested ? known.find(item => item.id === requested) : known[0];
      if (selected) { setWorkspaceId(selected.id); setNotice(""); } else setNotice(known.length ? "Select an authorized workspace to open the skill workshop." : "No authorized workspace is configured.");
    }).catch(() => { if (!controller.signal.aborted) setNotice("Sign in to Potbot to load the authorized skill workshop."); });
    return () => controller.abort();
  }, []);
  const query = workspaceId ? `?workspace=${encodeURIComponent(workspaceId)}` : "";
  return <main className={styles.shell}><header className={styles.header}><a className={styles.back} href={`/${query}`}>← Workspace</a><div className={styles.eyebrow}>POTBOT / SKILL WORKSHOP</div><h1>Bring a skill into your workspace</h1>
    <p>Import an exact source, review its complete package and approve a retained release. Control which instructions and resources authorized agents can load, with an auditable update and rollback history.</p>
    <nav className={styles.links} aria-label="Related workspace tools"><a href={`/tools${query}`}>Operations catalogue ↗</a><a href={`/setup/tools${query}`}>Executable tool upgrades ↗</a><a href="/setup/operator">Operator health ↗</a></nav></header>
    <div className={styles.toolbar}><label>Workspace<select value={workspaceId} onChange={event => { setWorkspaceId(event.target.value); setNotice(""); }}><option value="" disabled>Select workspace</option>{workspaces.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <span className={styles.help}>Every import, approval and resource read stays within this workspace and authenticated principal.</span></div>
    {notice && <p className={styles.status} role="status">{notice}</p>}{workspaceId && <WorkspaceWorkshop key={workspaceId} workspaceId={workspaceId} />}
    <footer className={styles.footer}><span>Skill packages provide instructions and data. Activation grants data availability.</span><a href={`/setup/tools${query}`}>Manage executable dependencies ↗</a></footer>
  </main>;
}
