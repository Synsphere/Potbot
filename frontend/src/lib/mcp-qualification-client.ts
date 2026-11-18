/** Scoped inspection, exact request recovery and evidence linkage for MCP task suites. */
import { workshopJournalScope, type WorkshopAttempt } from "./skill-workshop-client.ts";
import { UUID, MCP_QUALIFICATION_SCHEMA, MAX_QUALIFICATION_REQUEST, mcpObject, parseMcpJSON, validateQualificationMutation,
  type QualificationSuite, type QualificationAttempt, type QualificationReport, type QualificationCaseResult } from "./mcp-qualification.ts";
export { MCP_QUALIFICATION_SCHEMA };
export const MCP_QUALIFICATION_ENDPOINT = "/api/ui/mcp-qualification";
export const QUALIFICATION_MAX_POLLS = 30;

function selection(search: string, workspaceId: string, key: "suite" | "binding"): string | null {
  if (!UUID.test(workspaceId)) return null;
  const query = new URLSearchParams(search), value = query.get(key);
  return query.getAll("workspace").length === 1 && query.get("workspace") === workspaceId && query.getAll(key).length === 1 && value !== null && UUID.test(value) ? value : null;
}
export function mcpSuiteSelection(search: string, workspaceId: string): string | null { return selection(search, workspaceId, "suite"); }
export function mcpQualificationBindingSelection(search: string, workspaceId: string): string | null { return selection(search, workspaceId, "binding"); }
export function qualificationScopeInvalidated(status: number, value: unknown, workspaceId: string, principalId: string): boolean {
  if (status === 401 || status === 403) return true;
  const row = mcpObject(value); return !!row && row.schema === MCP_QUALIFICATION_SCHEMA && typeof row.workspaceId === "string" && UUID.test(row.workspaceId) && typeof row.principalId === "string" && UUID.test(row.principalId) && (row.workspaceId !== workspaceId || row.principalId !== principalId);
}
export function qualificationJournalScope(workspace: string, principal: string): string { return "mcp-qualification:" + workshopJournalScope(workspace, principal); }
export function qualificationCommandRoute(path: string): string[] | null { return path.startsWith(MCP_QUALIFICATION_ENDPOINT + "/") ? path.slice(MCP_QUALIFICATION_ENDPOINT.length + 1).split("/") : null; }
export function newQualificationCommand(path: string, body: unknown): WorkshopAttempt { return { path, body: JSON.stringify(body), key: "mcp-qualification:" + crypto.randomUUID() }; }
export function validateQualificationCommand(value: unknown): WorkshopAttempt | null {
  const row = mcpObject(value);
  if (!row || Object.keys(row).sort().join(",") !== "body,key,path" || typeof row.path !== "string" || typeof row.body !== "string" || new TextEncoder().encode(row.body).length > MAX_QUALIFICATION_REQUEST || typeof row.key !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(row.key)) return null;
  const path = qualificationCommandRoute(row.path); if (!path) return null;
  try { return validateQualificationMutation(path, parseMcpJSON(row.body)) ? { path: row.path, body: row.body, key: row.key } : null; } catch { return null; }
}
function casesMatch(cases: QualificationCaseResult[], suite: QualificationSuite): boolean {
  return cases.length === suite.cases.length && cases.every((value, index) => {
    const admitted = suite.cases[index]; return value.id === admitted.id && value.title === admitted.title && value.caseSha256 === admitted.caseSha256 && value.oracleSha256 === admitted.oracleSha256 && (value.checks === null || value.checks.length === admitted.assertionCount);
  });
}
export function qualificationAttemptMatchesSuite(attempt: QualificationAttempt, suite: QualificationSuite): boolean {
  return attempt.suiteId === suite.id && attempt.bindingId === suite.bindingId && attempt.agentId === suite.agentId && attempt.suiteSha256 === suite.suiteSha256 && attempt.configurationSha256 === suite.configurationSha256 && casesMatch(attempt.cases, suite);
}
export function qualificationReportMatchesSuite(report: QualificationReport, suite: QualificationSuite): boolean {
  const evidence = report.evidence;
  return report.suiteId === suite.id && report.bindingId === suite.bindingId && evidence.agentId === suite.agentId && evidence.suiteSha256 === suite.suiteSha256 && evidence.suiteFileSha256 === suite.suiteFileSha256 &&
    evidence.configurationSha256 === suite.configurationSha256 && evidence.installationSha256 === suite.installationSha256 && evidence.consumerSha256 === suite.consumerSha256 &&
    Object.entries(suite.taskBinding).every(([key, value]) => evidence.taskBinding[key as keyof typeof suite.taskBinding] === value) && casesMatch(report.cases, suite);
}
export function qualificationReportMatchesAttempt(report: QualificationReport, attempt: QualificationAttempt, suite: QualificationSuite): boolean {
  return qualificationAttemptMatchesSuite(attempt, suite) && qualificationReportMatchesSuite(report, suite) && report.attemptId === attempt.id && report.id === attempt.reportId && report.sha256 === attempt.reportSha256 &&
    report.cases.every((value, index) => Object.entries(value).every(([key, field]) => JSON.stringify(field) === JSON.stringify(attempt.cases[index][key as keyof QualificationCaseResult])));
}
export function qualificationAttemptNeedsMonitoring(attempt: QualificationAttempt): boolean { return ["requested", "running", "recovering"].includes(attempt.status); }
export function qualificationReportCurrent(report: QualificationReport, suite: QualificationSuite): boolean {
  return qualificationReportMatchesSuite(report, suite) && suite.state === "enabled" && suite.approvalValid && suite.authorityCurrent && suite.activeAttemptId === null &&
    suite.lastReportId === report.id && suite.lastAttemptId === report.attemptId && report.approvalValid && report.authorityCurrent;
}
export const QUALIFICATION_ATTEMPT_LABELS: Record<QualificationAttempt["status"], string> = {
  requested: "Awaiting host qualification", running: "Checking admitted task cases", recovering: "Reconciling retained receipts", succeeded: "Task cases passed", failed: "Qualification failed", cancelled: "Attempt cancelled", indeterminate: "Outcome requires host inspection",
};
