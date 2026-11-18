"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { releaseStatus, upgradeListing, upgradeRecord, type UpgradeRecord } from "../../../lib/tool-upgrades.ts";
import { WorkshopRequests } from "../../../lib/skill-workshop-client.ts";
import styles from "../../skills/skills.module.css";

/** Existing S11 approvals remain the authority for previously published tests. */
export default function LegacyToolApprovals({ workspaceId }: { workspaceId: string }) {
  const [items, setItems] = useState<UpgradeRecord[]>([]), [busy, setBusy] = useState(false), [notice, setNotice] = useState("");
  const reads = useRef(new WorkshopRequests()), mutations = useRef(new WorkshopRequests()), busyRef = useRef(false);
  const refresh = useCallback(async () => {
    reads.current.reset(); const ticket = reads.current.open(); setNotice("Reading published S11 test evidence…");
    try {
      const response = await fetch("/api/ui/tools/upgrades", { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: { "X-Potbot-Workspace": workspaceId } });
      if (!ticket.current()) return; if (!response.ok) throw new Error("unavailable");
      const raw = await response.json(); if (!ticket.current()) return;
      const result = upgradeListing(raw, workspaceId); if (!result || raw.workspaceId !== workspaceId) throw new Error("invalid");
      setItems(result.items); setNotice("");
    } catch { if (ticket.current()) setNotice("Published approvals are unavailable in this workspace. Reload after checking the operator session and backend."); }
    finally { ticket.finish(); }
  }, [workspaceId]);
  useEffect(() => { void refresh(); return () => { reads.current.reset(); mutations.current.reset(); }; }, [refresh]);
  function decide(item: UpgradeRecord, action: "approve" | "reject") {
    if (busyRef.current || item.workspaceId !== workspaceId || item.state !== "awaiting_approval") return;
    if (!window.confirm(`${action === "approve" ? "Approve activation of" : "Reject"} ${item.slug} from ${item.oldCommit?.slice(0, 12) ?? "not installed"} to ${item.newCommit.slice(0, 12)}?\n\nReview ${item.passedTests} passed tests and evidence ${item.evidenceSha256}.`)) return;
    busyRef.current = true; setBusy(true); setNotice("Recording the exact legacy approval decision…"); const ticket = mutations.current.open();
    void (async () => {
      try {
        const response = await fetch(`/api/ui/tools/upgrades/${item.id}/${action}`, { method: "POST", credentials: "same-origin", cache: "no-store", signal: ticket.signal,
          headers: { "X-Potbot-Workspace": workspaceId, "Content-Type": "application/json" }, body: JSON.stringify({ revision: item.revision, evidenceSha256: item.evidenceSha256, confirm: true }) });
        if (!ticket.current()) return; if (!response.ok) throw new Error("unavailable");
        const result = upgradeRecord(await response.json(), workspaceId); if (!ticket.current()) return;
        if (!result || result.id !== item.id || result.evidenceSha256 !== item.evidenceSha256 || result.revision !== item.revision + 1 || result.state !== (action === "approve" ? "approved" : "rejected")) throw new Error("invalid");
        await refresh(); if (!ticket.current()) return;
        setNotice(action === "approve" ? "Activation approved. Independent host execution, runtime pin review and installed postflight still govern release." : "The tested candidate was rejected.");
      } catch { if (ticket.current()) setNotice("The decision outcome could not be confirmed. Reload the published record to inspect its current revision before making another decision; no automatic retry occurs."); }
      finally { if (ticket.current()) { busyRef.current = false; setBusy(false); } ticket.finish(); }
    })();
  }
  return <section aria-label="Existing published tool approvals"><p className={styles.notice}>Existing S11 published test records retain their original approval controls. New source acquisition, build reviews and host actions are available in the acquisition workbench.</p>
    <button type="button" disabled={busy} onClick={() => void refresh()}>Reload published approvals</button><p className={styles.status} role="status">{notice}</p>
    <div className={styles.legacyGrid}>{items.map(item => <article className={styles.panel} key={item.id}><div className={styles.body}>
      <div className={styles.row}><h2>{item.slug}</h2><span className={styles.badge}>{item.state.replaceAll("_", " ")}</span></div>
      <p>{item.oldCommit ? item.oldCommit.slice(0, 12) : "First installation"} → {item.newCommit.slice(0, 12)}</p>
      <ul>{item.tests.map(test => <li key={test.name}>{test.name} · {test.kind} · {test.elapsedMs} ms · Passed</li>)}</ul>
      <dl className={styles.facts}><dt>Evidence</dt><dd><code>{item.evidenceSha256}</code></dd><dt>Suite</dt><dd><code>{item.suiteSha256}</code></dd></dl>
      <p className={styles.notice}><strong>{releaseStatus(item).label}.</strong> {releaseStatus(item).detail}</p>
      {item.state === "awaiting_approval" && <div className={styles.actions}><button type="button" className={styles.primary} disabled={busy} onClick={() => decide(item, "approve")}>Approve tested activation</button><button type="button" disabled={busy} onClick={() => decide(item, "reject")}>Reject</button></div>}
      {item.state === "indeterminate" && <p className={styles.warning}>Host reconciliation is required; the existing side effect is not automatically repeated.</p>}
    </div></article>)}</div>{!items.length && !notice && <p className={styles.empty}>No tested tool approvals are published for this workspace.</p>}
  </section>;
}
