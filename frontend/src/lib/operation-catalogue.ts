/** Read-only public catalogue projection. Observation never grants execution authority. */
export const OPERATION_KINDS = ["tool", "cog", "workflow", "connector", "model", "skill", "gateway"] as const;
export const OPERATION_STATES = ["source_only", "installed_unqualified", "policy_disabled", "qualified", "degraded", "indeterminate"] as const;
export type OperationKind = typeof OPERATION_KINDS[number];
export type OperationState = typeof OPERATION_STATES[number];
export const CATALOGUE_SCHEMA = "potbot-operation-catalogue/v1" as const;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA = /^[a-f0-9]{64}$/;
const CODE = /^[a-z][a-z0-9_]{0,63}$/;
const SOURCE_FAMILIES = new Set(["runtime_context", "tool_history", "shared_tool_upgrade", "model_catalog", "model_deployment", "published_skill", "syn_snapshot"]);
const INVOCATION_SOURCES = new Set(["cog", "hosted", "reasoning"]);
const PROVENANCE = new Set(["measured", "provider_reported", "synthetic", "conservative_estimate"]);

export type OperationSource = { family: string; recordId: string; revision: number | null; sha256: string | null };
export type OperationRelease = {
  operationId: string; state: string; releaseState: string; generation: number | null;
  bindingSha256: string | null; evidenceSha256: string | null;
};
export type OperationInvocation = {
  id: string; source: string; state: string; recordedAt: string; reservationId: string | null;
};
export type OperationAccounting = {
  scope: "linked_recent_invocations"; complete: false;
  reservationIds: string[]; settledCount: number; indeterminateCount: number;
  totals: { unit: string; value: string; provenance: string }[];
};
export type Operation = {
  id: string; kind: OperationKind; reference: string; name: string; state: OperationState;
  reasons: string[]; contextObjectId: string | null; version: string | null;
  fingerprintSha256: string | null; updateAvailable: boolean | null;
  sources: OperationSource[]; release: OperationRelease | null;
  recentInvocations: OperationInvocation[]; accounting: OperationAccounting;
};
export type CatalogueScope = {
  schema: typeof CATALOGUE_SCHEMA; workspaceId: string; principalId: string; snapshotSha256: string;
};
export type OperationListing = CatalogueScope & { items: Operation[]; nextCursor: string | null };
export type OperationDetail = CatalogueScope & { item: Operation };
export type CatalogueQuery = { q: string; kind: OperationKind | ""; state: OperationState | ""; after: string | null; limit: number };

function doc(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
function text(value: unknown, max: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= max && /^[\x20-\x7e]+$/.test(value);
}
function code(value: unknown): value is string { return typeof value === "string" && CODE.test(value); }
function count(value: unknown, max = Number.MAX_SAFE_INTEGER): value is number {
  return Number.isSafeInteger(value) && Number(value) >= 0 && Number(value) <= max;
}
function nullableId(value: unknown, pattern: RegExp): value is string | null {
  return value === null || (typeof value === "string" && pattern.test(value));
}
function boundedArray(value: unknown, max: number): value is unknown[] { return Array.isArray(value) && value.length <= max; }
function cursor(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && /^[A-Za-z0-9_.=-]{1,4096}$/.test(value));
}

export function projectOperation(value: unknown): Operation | null {
  const row = doc(value);
  if (!row || typeof row.id !== "string" || !SHA.test(row.id) ||
      !OPERATION_KINDS.includes(row.kind as OperationKind) || !OPERATION_STATES.includes(row.state as OperationState) ||
      !text(row.reference, 512) || !text(row.name, 256) ||
      !boundedArray(row.reasons, 32) || !row.reasons.every(code) || new Set(row.reasons).size !== row.reasons.length ||
      !nullableId(row.contextObjectId, UUID) || !nullableId(row.fingerprintSha256, SHA) ||
      !(row.version === null || text(row.version, 256)) ||
      !(row.updateAvailable === null || typeof row.updateAvailable === "boolean") ||
      !boundedArray(row.sources, 32) || !boundedArray(row.recentInvocations, 5)) return null;
  const sources: OperationSource[] = [];
  for (const value of row.sources) {
    const source = doc(value);
    if (!source || typeof source.family !== "string" || !SOURCE_FAMILIES.has(source.family) ||
        typeof source.recordId !== "string" || !(UUID.test(source.recordId) || SHA.test(source.recordId)) ||
        !(source.revision === null || count(source.revision)) || !nullableId(source.sha256, SHA)) return null;
    sources.push({ family: source.family, recordId: source.recordId, revision: source.revision as number | null, sha256: source.sha256 });
  }
  let release: OperationRelease | null = null;
  if (row.release !== null) {
    const value = doc(row.release);
    if (!value || typeof value.operationId !== "string" || !UUID.test(value.operationId) ||
        typeof value.state !== "string" || !["awaiting_approval", "approved", "rejected", "executing", "awaiting_repin", "active", "indeterminate", "rolled_back"].includes(value.state) ||
        typeof value.releaseState !== "string" || !["none", "repinned", "testing", "failed", "indeterminate", "released"].includes(value.releaseState) ||
        !(value.generation === null || (count(value.generation) && value.generation >= 1)) ||
        !nullableId(value.bindingSha256, SHA) || !nullableId(value.evidenceSha256, SHA) ||
        (value.releaseState !== "none" && value.generation === null) ||
        (value.releaseState === "released" && (value.bindingSha256 === null || value.evidenceSha256 === null))) return null;
    release = { operationId: value.operationId, state: value.state as string, releaseState: value.releaseState as string,
      generation: value.generation as number | null, bindingSha256: value.bindingSha256, evidenceSha256: value.evidenceSha256 };
  }
  const recentInvocations: OperationInvocation[] = [];
  for (const value of row.recentInvocations) {
    const invocation = doc(value);
    if (!invocation || typeof invocation.id !== "string" || !UUID.test(invocation.id) ||
        typeof invocation.source !== "string" || !INVOCATION_SOURCES.has(invocation.source) || !code(invocation.state) ||
        !text(invocation.recordedAt, 64) || !/^\d{4}-\d\d-\d\dT/.test(invocation.recordedAt) ||
        !Number.isFinite(Date.parse(invocation.recordedAt)) || !nullableId(invocation.reservationId, UUID)) return null;
    recentInvocations.push({ id: invocation.id, source: invocation.source, state: invocation.state,
      recordedAt: invocation.recordedAt, reservationId: invocation.reservationId });
  }
  if (new Set(recentInvocations.map(item => `${item.source}:${item.id}`)).size !== recentInvocations.length) return null;
  const rawAccounting = doc(row.accounting);
  if (!rawAccounting || rawAccounting.scope !== "linked_recent_invocations" || rawAccounting.complete !== false ||
      !boundedArray(rawAccounting.reservationIds, 5) ||
      !rawAccounting.reservationIds.every(id => typeof id === "string" && UUID.test(id)) ||
      new Set(rawAccounting.reservationIds).size !== rawAccounting.reservationIds.length ||
      !count(rawAccounting.settledCount, 5) || !count(rawAccounting.indeterminateCount, 5) ||
      !boundedArray(rawAccounting.totals, 32)) return null;
  const totals: OperationAccounting["totals"] = [];
  for (const value of rawAccounting.totals) {
    const amount = doc(value);
    if (!amount || typeof amount.unit !== "string" || !/^[A-Za-z][A-Za-z0-9_.-]{0,95}$/.test(amount.unit) ||
        amount.unit === "concurrency" || typeof amount.value !== "string" || !/^(0|[1-9]\d{0,31})$/.test(amount.value) ||
        typeof amount.provenance !== "string" || !PROVENANCE.has(amount.provenance)) return null;
    totals.push({ unit: amount.unit, value: amount.value, provenance: amount.provenance });
  }
  if (new Set(totals.map(amount => `${amount.unit}:${amount.provenance}`)).size !== totals.length) return null;
  return { id: row.id, kind: row.kind as OperationKind, reference: row.reference, name: row.name,
    state: row.state as OperationState, reasons: row.reasons as string[], contextObjectId: row.contextObjectId,
    version: row.version as string | null, fingerprintSha256: row.fingerprintSha256,
    updateAvailable: row.updateAvailable, sources, release, recentInvocations,
    accounting: { scope: "linked_recent_invocations", complete: false,
      reservationIds: rawAccounting.reservationIds as string[], settledCount: rawAccounting.settledCount,
      indeterminateCount: rawAccounting.indeterminateCount, totals } };
}

function scope(value: Record<string, unknown>, workspaceId: string): CatalogueScope | null {
  if (!UUID.test(workspaceId) || value.schema !== CATALOGUE_SCHEMA || value.workspaceId !== workspaceId ||
      typeof value.principalId !== "string" || !UUID.test(value.principalId) ||
      typeof value.snapshotSha256 !== "string" || !SHA.test(value.snapshotSha256)) return null;
  return { schema: CATALOGUE_SCHEMA, workspaceId, principalId: value.principalId, snapshotSha256: value.snapshotSha256 };
}

export function projectOperationListing(value: unknown, workspaceId: string): OperationListing | null {
  const row = doc(value);
  if (!row) return null;
  const selected = scope(row, workspaceId);
  if (!selected || !boundedArray(row.items, 100) || !cursor(row.nextCursor)) return null;
  const items: Operation[] = [];
  for (const raw of row.items) { const item = projectOperation(raw); if (!item) return null; items.push(item); }
  if (new Set(items.map(item => item.id)).size !== items.length || (row.nextCursor !== null && items.length === 0)) return null;
  return { ...selected, items, nextCursor: row.nextCursor };
}

export function projectOperationDetail(value: unknown, workspaceId: string, operationId: string): OperationDetail | null {
  const row = doc(value);
  if (!row || !SHA.test(operationId)) return null;
  const selected = scope(row, workspaceId), item = projectOperation(row.item);
  return selected && item?.id === operationId ? { ...selected, item } : null;
}

/** Query values are filters only; workspace identity comes from the authorized header. */
export function catalogueQuery(params: URLSearchParams): CatalogueQuery | null {
  const keys = ["q", "kind", "state", "after", "limit"];
  if ([...params.keys()].some(key => !keys.includes(key) || params.getAll(key).length !== 1)) return null;
  const q = params.get("q") ?? "", kind = params.get("kind") ?? "", state = params.get("state") ?? "";
  const after = params.get("after"), limit = params.get("limit") ?? "25";
  if (q.length > 160 || /[\u0000-\u001f\u007f]/.test(q) ||
      (kind !== "" && !OPERATION_KINDS.includes(kind as OperationKind)) ||
      (state !== "" && !OPERATION_STATES.includes(state as OperationState)) ||
      !cursor(after) || !/^[1-9]\d{0,2}$/.test(limit) || Number(limit) > 100) return null;
  return { q, kind: kind as CatalogueQuery["kind"], state: state as CatalogueQuery["state"], after, limit: Number(limit) };
}

export function catalogueSearch(query: CatalogueQuery): string {
  const values: [string, string][] = [["limit", String(query.limit)]];
  if (query.q) values.push(["q", query.q]);
  if (query.kind) values.push(["kind", query.kind]);
  if (query.state) values.push(["state", query.state]);
  if (query.after) values.push(["after", query.after]);
  // Encode dots in values as well: the shared upstream path guard rejects '..'
  // anywhere in a URL, while a search term must never be interpreted as a path.
  return values.map(([key, value]) => `${key}=${encodeURIComponent(value).replaceAll(".", "%2E")}`).join("&");
}

export const READINESS_LABELS: Record<OperationState, string> = {
  source_only: "Source only", installed_unqualified: "Installed · unqualified", policy_disabled: "Policy disabled",
  qualified: "Qualification reported", degraded: "Degraded", indeterminate: "Outcome unresolved",
};
export function readableCode(value: string): string { return value.replaceAll("_", " ").replaceAll(".", " · "); }

const REASON_LABELS: Record<string, string> = {
  source_metadata_only: "Only source metadata has been recorded.",
  runtime_observation_not_live: "The runtime observation has not been verified against the current host.",
  qualification_not_established: "Qualification has not been established for this operation.",
  execution_policy_not_evaluated: "Execution permissions must still be checked for each request.",
  skill_is_untrusted_data: "Skill content is untrusted data and cannot grant permissions.",
  approval_required: "The proposed upgrade is waiting for operator approval.",
  upgrade_approved_not_activated: "The upgrade is approved but has not been activated.",
  activation_in_progress: "Activation is in progress; the changed tool remains held.",
  runtime_repin_required: "The installed executable pins need independent review.",
  postflight_required: "The installed tool needs its approved postflight checks.",
  postflight_running: "Postflight is running; the changed tool remains held.",
  postflight_failed: "Postflight failed; the changed tool remains held.",
  release_indeterminate: "The release outcome is unresolved and requires reconciliation.",
  release_record_not_current_admission: "A release was recorded; current host admission has not been established here.",
  upgrade_rejected: "The operator rejected this upgrade.",
  model_deployment_record_only: "A deployment record exists; this view does not verify a running model.",
  model_health_unknown: "Current model health is unknown.",
  model_health_degraded: "The recorded model health is degraded.",
  model_health_unavailable: "The recorded model health is unavailable.",
  model_qualification_record_only: "A model qualification record exists; it does not establish current availability.",
  invocation_indeterminate: "A recent invocation has an unresolved outcome.",
  unsupported_reference: "This reference does not have a supported operation projection.",
  accounting_scope_limited: "Accounting covers only the recent invocations linked below.",
};
export function operationReason(reason: string): string { return REASON_LABELS[reason] ?? readableCode(reason); }

const REASON_ORDER = [
  "release_indeterminate", "postflight_failed", "model_health_unavailable", "model_health_degraded",
  "runtime_repin_required", "postflight_required", "postflight_running", "activation_in_progress",
  "invocation_indeterminate", "approval_required", "upgrade_approved_not_activated", "upgrade_rejected",
  "unsupported_reference", "runtime_observation_not_live", "model_deployment_record_only",
  "source_metadata_only", "qualification_not_established", "model_health_unknown",
  "model_qualification_record_only", "release_record_not_current_admission", "execution_policy_not_evaluated",
  "skill_is_untrusted_data",
];
export function orderedOperationReasons(reasons: readonly string[]): string[] {
  // The backend sorts codes for stable hashing. Put actionable holds first in
  // the UI, retaining every original code and placing accounting scope last.
  const rank = (reason: string) => {
    if (reason === "accounting_scope_limited") return REASON_ORDER.length + 1;
    const index = REASON_ORDER.indexOf(reason);
    return index < 0 ? REASON_ORDER.length : index;
  };
  return [...reasons].sort((left, right) => rank(left) - rank(right));
}

export function exactAmount(value: string): string {
  // Decimal strings preserve ledger integers above JavaScript's safe-number range.
  return value.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}
