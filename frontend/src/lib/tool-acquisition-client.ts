/** Shared recovery storage is reused under a separate tool acquisition scope. */
import { toolObject, validateToolMutation, type ToolOperation, type ToolPhase } from "./tool-acquisition.ts";
import { workshopJournalScope, type WorkshopAttempt } from "./skill-workshop-client.ts";
export const TOOL_ENDPOINT = "/api/ui/tools/acquisitions";
export function toolJournalScope(workspace: string, principal: string): string { return "tool:" + workshopJournalScope(workspace, principal); }
export function newToolAttempt(path: string, body: unknown): WorkshopAttempt { return { path, body: JSON.stringify(body), key: "tool:" + crypto.randomUUID() }; }
export function toolAttemptRoute(path: string): string[] | null {
  return path === TOOL_ENDPOINT ? [] : path.startsWith(TOOL_ENDPOINT + "/") ? path.slice(TOOL_ENDPOINT.length + 1).split("/") : null;
}
export function validateToolAttempt(value: unknown): WorkshopAttempt | null {
  const row = toolObject(value);
  if (!row || Object.keys(row).sort().join(",") !== "body,key,path" || typeof row.path !== "string" || typeof row.body !== "string" ||
      new TextEncoder().encode(row.body).length > 8192 || typeof row.key !== "string" || !/^[A-Za-z0-9_.:-]{1,128}$/.test(row.key)) return null;
  const route = toolAttemptRoute(row.path); if (!route) return null;
  try { return validateToolMutation(route, JSON.parse(row.body)) ? { path: row.path, body: row.body, key: row.key } : null; } catch { return null; }
}
export const TOOL_PHASE_LABELS: Record<ToolPhase, string> = {
  source_queued: "Source requested", sourcing: "Inspecting source", review: "Build review required", build_queued: "Build approved", building: "Building and testing",
  failed: "Failed", indeterminate: "Outcome unresolved", cancelled: "Cancelled", awaiting_approval: "Tested · activation review required", approved: "Activation approved",
  executing: "Activating", awaiting_repin: "Host pin review required", repinned: "Postflight required", testing: "Postflight running", active: "Released",
  rolled_back: "Previous version restored", withdrawn: "Installation removed", rejected: "Activation rejected",
};
export function toolNeedsMonitoring(operation: ToolOperation): boolean {
  return ["source_queued", "sourcing", "build_queued", "building", "approved", "executing", "testing"].includes(operation.phase) ||
    operation.hostAction?.status === "queued" || operation.hostAction?.status === "running";
}
export function toolAvailability(operation: ToolOperation): string {
  return operation.installedState === "released" ? "Released · normal execution policy applies" : operation.installedState === "absent" ? "Not installed" :
    operation.installedState === "held" ? "Held · execution unavailable" : "Installed state not yet verified";
}
