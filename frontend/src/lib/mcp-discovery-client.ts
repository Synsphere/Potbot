/** Scope and exact-request helpers for retained MCP observations. */
import { UUID } from "./operation-workbench.ts";
import { workshopJournalScope, type WorkshopAttempt } from "./skill-workshop-client.ts";
import { MCP_DISCOVERY_SCHEMA, MAX_MCP_REQUEST, mcpObject, parseMcpJSON, validateMcpMutation, type McpBinding, type McpEntry, type McpAttempt, type McpReference } from "./mcp-discovery.ts";
export { MCP_DISCOVERY_SCHEMA };

export const MCP_DISCOVERY_ENDPOINT = "/api/ui/mcp-discovery";

export function mcpBindingSelection(search: string, workspaceId: string): string | null {
  if (!UUID.test(workspaceId)) return null;
  const query = new URLSearchParams(search), selected = query.get("binding");
  return query.getAll("workspace").length === 1 && query.get("workspace") === workspaceId && query.getAll("binding").length === 1 && selected !== null && UUID.test(selected) ? selected : null;
}

export function mcpDiscoveryScopeInvalidated(status: number, value: unknown, workspaceId: string, principalId: string): boolean {
  if (status === 401 || status === 403) return true;
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const row = value as Record<string, unknown>;
  return row.schema === MCP_DISCOVERY_SCHEMA && typeof row.workspaceId === "string" && UUID.test(row.workspaceId) && typeof row.principalId === "string" && UUID.test(row.principalId) && (row.workspaceId !== workspaceId || row.principalId !== principalId);
}

export function mcpDiscoveryJournalScope(workspace: string, principal: string): string { return "mcp-discovery:" + workshopJournalScope(workspace, principal); }
export function mcpAttemptRoute(path: string): string[] | null { return path.startsWith(MCP_DISCOVERY_ENDPOINT + "/") ? path.slice(MCP_DISCOVERY_ENDPOINT.length + 1).split("/") : null; }
export function newMcpDiscoveryAttempt(path: string, body: unknown): WorkshopAttempt { return { path, body: JSON.stringify(body), key: "mcp-discovery:" + crypto.randomUUID() }; }

export function validateMcpAttempt(value: unknown): WorkshopAttempt | null {
  const row = mcpObject(value);
  if (!row || Object.keys(row).sort().join(",") !== "body,key,path" || typeof row.path !== "string" || typeof row.body !== "string" || new TextEncoder().encode(row.body).length > MAX_MCP_REQUEST || typeof row.key !== "string" || !/^[A-Za-z0-9_.:-]{1,128}$/.test(row.key)) return null;
  const path = mcpAttemptRoute(row.path); if (!path) return null;
  try { return validateMcpMutation(path, parseMcpJSON(row.body)) ? { path: row.path, body: row.body, key: row.key } : null; } catch { return null; }
}

export function mcpBindingCurrent(binding: McpBinding, now = Date.now()): boolean {
  return binding.state === "enabled" && binding.approvalValid && !binding.stale && binding.snapshotId !== null && binding.snapshotSha256 !== null && binding.expiresAt !== null && Number.isFinite(now) && Date.parse(binding.expiresAt) > now;
}
export function mcpCanReference(entry: McpEntry, binding: McpBinding, now = Date.now()): boolean {
  return mcpBindingCurrent(binding, now) && entry.referenceAvailable && entry.bindingId === binding.id && entry.snapshotId === binding.snapshotId && entry.snapshotSha256 === binding.snapshotSha256;
}
export function mcpReferenceCurrent(reference: McpReference, entry: McpEntry, binding: McpBinding, now = Date.now()): boolean {
  return mcpCanReference(entry, binding, now) && reference.bindingId === binding.id && reference.bindingRevision === binding.revision && reference.bindingSha256 === binding.bindingSha256 &&
    reference.installationSha256 === binding.installationSha256 && reference.snapshotId === binding.snapshotId && reference.snapshotSha256 === binding.snapshotSha256 && reference.entryId === entry.id &&
    reference.kind === entry.kind && reference.identity === entry.identity && reference.definitionSha256 === entry.definitionSha256 && Date.parse(reference.expiresAt) > now && Date.parse(reference.expiresAt) === Date.parse(binding.expiresAt!);
}
export function mcpAttemptNeedsMonitoring(attempt: McpAttempt): boolean { return attempt.status === "requested" || attempt.status === "running"; }
export const MCP_ATTEMPT_LABELS: Record<McpAttempt["status"], string> = {
  requested: "Awaiting host reconciliation", running: "Reconciling interfaces", succeeded: "Catalogue reconciled", failed: "Reconciliation failed", cancelled: "Attempt cancelled", indeterminate: "Outcome requires host inspection",
};
