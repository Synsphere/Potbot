/** Exact explicit discovery recovery; loading this module never submits work. */
import { UUID, DISCOVERY_SCHEMA, discoveryObject, discoverySearchParams, validateDiscoveryMutation, MAX_DISCOVERY_REQUEST, type DiscoveryCheck } from "./discovery.ts";
import { workshopJournalScope, type WorkshopAttempt } from "./skill-workshop-client.ts";
export const DISCOVERY_ENDPOINT = "/api/ui/discovery";
/** Hide the old private scope before inspecting either a read or mutation receipt. */
export function discoveryScopeInvalidated(status: number, value: unknown, workspaceId: string, principalId: string): boolean {
  if (status === 401 || status === 403) return true;
  const row = discoveryObject(value);
  return row?.schema === DISCOVERY_SCHEMA && row.workspaceId === workspaceId && typeof row.principalId === "string" && UUID.test(row.principalId) && row.principalId !== principalId;
}
export type SearchDraft = { q: string; sourceId: string; kind: string; platform: string; local: boolean; restrictEffects: boolean; effects: string[];
  restrictScopes: boolean; scopes: string; classes: string; inputTypes: string; outputTypes: string };
export const EMPTY_SEARCH: SearchDraft = { q: "", sourceId: "", kind: "", platform: "", local: false, restrictEffects: false, effects: ["read_only"], restrictScopes: false, scopes: "", classes: "", inputTypes: "", outputTypes: "" };
export function discoverySearchQuery(draft: SearchDraft): URLSearchParams | null {
  const query = new URLSearchParams();
  for (const key of ["q", "sourceId", "kind", "platform", "classes", "inputTypes", "outputTypes"] as const) if (draft[key]) query.set(key, draft[key]);
  if (draft.local) query.set("requireLocal", "true");
  if (draft.restrictEffects) query.set("permittedEffects", draft.effects.join(","));
  if (draft.restrictScopes) query.set("permittedScopes", draft.scopes);
  query.set("limit", "25"); const filtered = discoverySearchParams(query); if (!filtered) return null;
  filtered.set("limit", "25"); return filtered;
}
export function discoveryJournalScope(workspace: string, principal: string): string { return "discovery:" + workshopJournalScope(workspace, principal); }
export function discoveryAttemptRoute(path: string): string[] | null { return path.startsWith(DISCOVERY_ENDPOINT + "/") ? path.slice(DISCOVERY_ENDPOINT.length + 1).split("/") : null; }
export function newDiscoveryAttempt(path: string, body: unknown): WorkshopAttempt { return { path, body: JSON.stringify(body), key: "discovery:" + crypto.randomUUID() }; }
export function validateDiscoveryAttempt(value: unknown): WorkshopAttempt | null {
  const row = discoveryObject(value);
  if (!row || Object.keys(row).sort().join(",") !== "body,key,path" || typeof row.path !== "string" || typeof row.body !== "string" ||
      new TextEncoder().encode(row.body).length > MAX_DISCOVERY_REQUEST || typeof row.key !== "string" || !/^[A-Za-z0-9_.:-]{1,128}$/.test(row.key)) return null;
  const route = discoveryAttemptRoute(row.path); if (!route) return null;
  try { return validateDiscoveryMutation(route, JSON.parse(row.body)) ? { path: row.path, body: row.body, key: row.key } : null; } catch { return null; }
}
export function discoveryNeedsMonitoring(check: DiscoveryCheck): boolean { return check.status === "awaiting_measurement" || check.status === "measuring"; }
export function discoveryMeasurementCurrent(check: DiscoveryCheck, now = Date.now()): boolean {
  return check.measurementCurrent && check.measurement !== null && Number.isFinite(now) && Date.parse(check.measurement.expiresAt) > now;
}
export function discoveryCanHandoff(check: DiscoveryCheck, now = Date.now()): boolean {
  return check.status === "measured" && check.actions.includes("handoff") && discoveryMeasurementCurrent(check, now);
}
export const CHECK_LABELS: Record<DiscoveryCheck["status"], string> = {
  awaiting_measurement: "Awaiting host measurement", measuring: "Measuring prerequisites", measured: "Prerequisites measured", failed: "Review failed", handed_off: "Acquisition review opened", cancelled: "Cancelled",
};
