/** Public retained task evidence; private cases and execution authority stay with native owners. */
import { UUID, SHA256, mcpObject, parseMcpJSON } from "./mcp-discovery.ts";
export { UUID, SHA256, mcpObject, parseMcpJSON };

export const MCP_QUALIFICATION_SCHEMA = "potbot-mcp-qualification/v1" as const;
export const MCP_QUALIFICATION_REPORT_SCHEMA = "potbot-mcp-qualification-report/v1" as const;
export const MAX_QUALIFICATION_REQUEST = 8192, MAX_QUALIFICATION_RESPONSE = 1_572_864;
export const QUALIFICATION_SUITE_STATES = ["enabled", "disabled", "revoked"] as const;
export const QUALIFICATION_SUITE_ACTIONS = ["enable", "disable", "revoke", "qualify"] as const;
export const QUALIFICATION_ATTEMPT_STATES = ["requested", "running", "recovering", "succeeded", "failed", "cancelled", "indeterminate"] as const;
export const QUALIFICATION_CASE_STATES = ["pending", "prepared", "passed", "failed", "not_run", "indeterminate"] as const;
const CHECK_REASONS = ["pointer_missing", "value_mismatch", "value_type_mismatch", "text_not_found", "length_mismatch"] as const;
export const QUALIFICATION_CASE_ID = /^[a-z][a-z0-9_-]{0,63}$/;
export type QualificationScope = { schema: typeof MCP_QUALIFICATION_SCHEMA; workspaceId: string; principalId: string };
export type QualificationPage<T> = QualificationScope & { items: T[]; nextCursor: string | null };
export type QualificationDecision = { revision: number; digest: string; confirm: true };
export type QualificationTaskBinding = { episodeId: string; contractRevisionId: string; contractSha256: string; repositoryStateId: string; repositorySha256: string; semanticReleaseId: string; semanticReleaseSha256: string };
export type QualificationCase = { id: string; title: string; toolName: string; caseSha256: string; argumentsSha256: string; oracleSha256: string; assertionCount: number };
export type QualificationCheck = { index: number; passed: boolean; reasonCode: typeof CHECK_REASONS[number] | null };
export type QualificationCaseResult = { id: string; title: string; caseSha256: string; status: typeof QUALIFICATION_CASE_STATES[number]; actionId: string | null; candidateId: string | null;
  reservationId: string | null; runId: string | null; resultArtifactId: string | null; resultSha256: string | null; receiptSha256: string | null; oracleSha256: string;
  checks: QualificationCheck[] | null; elapsedMs: number | null; reasonCode: string | null };
export type QualificationSuite = { id: string; bindingId: string; name: string; description: string; agentId: string; toolName: string; transport: "stdio" | "streamable_http";
  revision: number; state: typeof QUALIFICATION_SUITE_STATES[number]; bindingSha256: string; approvalValid: boolean; suiteSha256: string; suiteFileSha256: string;
  configurationSha256: string; installationSha256: string; consumerSha256: string; taskBinding: QualificationTaskBinding; cases: QualificationCase[];
  activeAttemptId: string | null; lastAttemptId: string | null; lastReportId: string | null; authorityCurrent: boolean; reasonCodes: string[];
  actions: typeof QUALIFICATION_SUITE_ACTIONS[number][]; createdAt: string; updatedAt: string };
export type QualificationAttempt = { id: string; suiteId: string; bindingId: string; agentId: string; revision: number; bindingSha256: string; status: typeof QUALIFICATION_ATTEMPT_STATES[number];
  requestedSuiteSha256: string; suiteSha256: string; configurationSha256: string; caseCount: number; completedCases: number; passedCases: number; failedCases: number;
  cases: QualificationCaseResult[]; reportId: string | null; reportSha256: string | null; reasonCode: string | null; actions: "cancel"[]; recoveryAvailable: boolean; createdAt: string; updatedAt: string };
export type QualificationEvidence = { schema: typeof MCP_QUALIFICATION_REPORT_SCHEMA; workspaceId: string; principalId: string; attemptId: string; agentId: string; suiteSha256: string;
  suiteFileSha256: string; consumerSha256: string; configurationSha256: string; installationSha256: string; definitionSha256: string; runtimeSha256: string; imageSha256: string;
  qualificationSha256: string; agentHomeRevision: number; storageSha256: string; referenceSha256: string; taskBinding: QualificationTaskBinding; observedAt: string; completedAt: string; casesSha256: string };
export type QualificationReport = { id: string; suiteId: string; attemptId: string; bindingId: string; sha256: string; approvalValid: boolean; complete: boolean; passed: boolean;
  authorityCurrent: boolean; reasonCodes: string[]; evidence: QualificationEvidence; cases: QualificationCaseResult[]; recordedAt: string };

const keys = (row: Record<string, unknown>, expected: readonly string[]) => Object.keys(row).sort().join("\0") === [...expected].sort().join("\0");
function text(value: unknown, maximum: number, empty = false, multiline = false): value is string { return typeof value === "string" && Array.from(value).length <= maximum && (empty || value.length > 0) && !(multiline ? /[\u0000-\u0008\u000b-\u001f\u007f]/ : /[\u0000-\u001f\u007f]/).test(value); }
function id(value: unknown): value is string { return typeof value === "string" && UUID.test(value); }
function sha(value: unknown): value is string { return typeof value === "string" && SHA256.test(value); }
function integer(value: unknown, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): value is number { return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum && value <= maximum; }
function timestamp(value: unknown): value is string { return typeof value === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) && Number.isFinite(Date.parse(value)); }
function code(value: unknown): value is string { return typeof value === "string" && /^[a-z][a-z0-9_]{0,95}$/.test(value); }
function codes(value: unknown): value is string[] { return Array.isArray(value) && value.length <= 32 && value.every(code) && new Set(value).size === value.length; }
function member<T extends string>(value: unknown, values: readonly T[]): value is T { return typeof value === "string" && values.includes(value as T); }
function actions<T extends string>(value: unknown, allowed: readonly T[]): value is T[] { return Array.isArray(value) && value.length <= allowed.length && value.every(item => member(item, allowed)) && new Set(value).size === value.length; }

export function projectQualificationTaskBinding(value: unknown): QualificationTaskBinding | null {
  const r = mcpObject(value);
  if (!r || !keys(r, ["episodeId", "contractRevisionId", "contractSha256", "repositoryStateId", "repositorySha256", "semanticReleaseId", "semanticReleaseSha256"]) ||
      !id(r.episodeId) || !id(r.contractRevisionId) || !sha(r.contractSha256) || !id(r.repositoryStateId) || !sha(r.repositorySha256) || !id(r.semanticReleaseId) || !sha(r.semanticReleaseSha256)) return null;
  return { episodeId: r.episodeId, contractRevisionId: r.contractRevisionId, contractSha256: r.contractSha256, repositoryStateId: r.repositoryStateId,
    repositorySha256: r.repositorySha256, semanticReleaseId: r.semanticReleaseId, semanticReleaseSha256: r.semanticReleaseSha256 };
}

export function projectQualificationCase(value: unknown): QualificationCase | null {
  const r = mcpObject(value);
  if (!r || !keys(r, ["id", "title", "toolName", "caseSha256", "argumentsSha256", "oracleSha256", "assertionCount"]) || typeof r.id !== "string" || !QUALIFICATION_CASE_ID.test(r.id) ||
      !text(r.title, 128) || !text(r.toolName, 128) || !sha(r.caseSha256) || !sha(r.argumentsSha256) || !sha(r.oracleSha256) || !integer(r.assertionCount, 1, 16)) return null;
  return { id: r.id, title: r.title, toolName: r.toolName, caseSha256: r.caseSha256, argumentsSha256: r.argumentsSha256, oracleSha256: r.oracleSha256, assertionCount: r.assertionCount };
}

export function projectQualificationCheck(value: unknown): QualificationCheck | null {
  const r = mcpObject(value);
  if (!r || !keys(r, ["index", "passed", "reasonCode"]) || !integer(r.index, 0, 15) || typeof r.passed !== "boolean" ||
      !(r.reasonCode === null || member(r.reasonCode, CHECK_REASONS)) || r.passed !== (r.reasonCode === null)) return null;
  return { index: r.index, passed: r.passed, reasonCode: r.reasonCode };
}

export function projectQualificationCaseResult(value: unknown): QualificationCaseResult | null {
  const r = mcpObject(value), checks = r?.checks === null ? null : Array.isArray(r?.checks) ? r.checks.map(projectQualificationCheck) : undefined;
  if (!r || !keys(r, ["id", "title", "caseSha256", "status", "actionId", "candidateId", "reservationId", "runId", "resultArtifactId", "resultSha256", "receiptSha256", "oracleSha256", "checks", "elapsedMs", "reasonCode"]) ||
      typeof r.id !== "string" || !QUALIFICATION_CASE_ID.test(r.id) || !text(r.title, 128) || !sha(r.caseSha256) || !member(r.status, QUALIFICATION_CASE_STATES) ||
      ![r.actionId, r.candidateId, r.reservationId, r.runId, r.resultArtifactId].every(value => value === null || id(value)) || ![r.resultSha256, r.receiptSha256].every(value => value === null || sha(value)) ||
      !sha(r.oracleSha256) || !(r.elapsedMs === null || integer(r.elapsedMs)) || !(r.reasonCode === null || code(r.reasonCode)) || checks === undefined ||
      checks !== null && (checks.length < 1 || checks.length > 16 || checks.some((item, index) => !item || item.index !== index))) return null;
  if ((r.resultArtifactId === null) !== (r.resultSha256 === null)) return null;
  if (r.status === "prepared" && [r.actionId, r.candidateId, r.reservationId].some(value => value === null)) return null;
  if (r.status === "pending" && [r.actionId, r.candidateId, r.reservationId].some(value => value !== null)) return null;
  if (!["passed", "failed"].includes(r.status) && [r.runId, r.resultArtifactId, r.resultSha256, r.receiptSha256, r.elapsedMs].some(value => value !== null)) return null;
  if (["passed", "failed"].includes(r.status) && [r.actionId, r.candidateId, r.reservationId, r.runId, r.resultArtifactId, r.resultSha256, r.receiptSha256, r.elapsedMs].some(value => value === null)) return null;
  if (r.status === "passed" && (checks === null || checks.some(item => !item?.passed) || r.reasonCode !== null) || r.status === "failed" && (r.reasonCode === null || checks !== null && checks.every(item => item?.passed)) || checks !== null && !["passed", "failed"].includes(r.status)) return null;
  return { id: r.id, title: r.title, caseSha256: r.caseSha256, status: r.status, actionId: r.actionId as string | null, candidateId: r.candidateId as string | null,
    reservationId: r.reservationId as string | null, runId: r.runId as string | null, resultArtifactId: r.resultArtifactId as string | null,
    resultSha256: r.resultSha256 as string | null, receiptSha256: r.receiptSha256 as string | null, oracleSha256: r.oracleSha256,
    checks: checks as QualificationCheck[] | null, elapsedMs: r.elapsedMs, reasonCode: r.reasonCode };
}

function caseList<T extends { id: string }>(value: unknown, project: (raw: unknown) => T | null): T[] | null {
  if (!Array.isArray(value) || value.length < 1 || value.length > 8) return null;
  const items = value.map(project); return items.every(item => item !== null) && new Set(items.map(item => item?.id)).size === items.length ? items as T[] : null;
}

export function projectQualificationSuite(value: unknown): QualificationSuite | null {
  const r = mcpObject(value), task = projectQualificationTaskBinding(r?.taskBinding), cases = caseList(r?.cases, projectQualificationCase);
  if (!r || !id(r.id) || !id(r.bindingId) || !id(r.agentId) || !text(r.name, 96) || !text(r.description, 2048, true, true) || !text(r.toolName, 128) || !member(r.transport, ["stdio", "streamable_http"]) ||
      !integer(r.revision, 1) || !member(r.state, QUALIFICATION_SUITE_STATES) || !sha(r.bindingSha256) || typeof r.approvalValid !== "boolean" ||
      ![r.suiteSha256, r.suiteFileSha256, r.configurationSha256, r.installationSha256, r.consumerSha256].every(sha) || !task || !cases || cases.some(item => item.toolName !== r.toolName) ||
      ![r.activeAttemptId, r.lastAttemptId, r.lastReportId].every(value => value === null || id(value)) || typeof r.authorityCurrent !== "boolean" || !codes(r.reasonCodes) ||
      !actions(r.actions, QUALIFICATION_SUITE_ACTIONS) || !timestamp(r.createdAt) || !timestamp(r.updatedAt)) return null;
  if (r.authorityCurrent !== (r.reasonCodes.length === 0) || r.authorityCurrent && (!r.approvalValid || r.state !== "enabled") || Date.parse(r.updatedAt) < Date.parse(r.createdAt) ||
      r.state === "revoked" && r.actions.length || !r.approvalValid && r.actions.some(action => action !== "revoke") || r.actions.includes("enable") && r.state !== "disabled" ||
      r.actions.includes("disable") && r.state !== "enabled" || r.actions.includes("qualify") && (r.state !== "enabled" || !r.approvalValid || !r.authorityCurrent || r.activeAttemptId !== null)) return null;
  return { id: r.id, bindingId: r.bindingId, name: r.name, description: r.description, agentId: r.agentId, toolName: r.toolName, transport: r.transport, revision: r.revision,
    state: r.state, bindingSha256: r.bindingSha256, approvalValid: r.approvalValid, suiteSha256: r.suiteSha256 as string, suiteFileSha256: r.suiteFileSha256 as string,
    configurationSha256: r.configurationSha256 as string, installationSha256: r.installationSha256 as string, consumerSha256: r.consumerSha256 as string, taskBinding: task, cases,
    activeAttemptId: r.activeAttemptId as string | null, lastAttemptId: r.lastAttemptId as string | null, lastReportId: r.lastReportId as string | null,
    authorityCurrent: r.authorityCurrent, reasonCodes: [...r.reasonCodes], actions: [...r.actions], createdAt: r.createdAt, updatedAt: r.updatedAt };
}

export function projectQualificationAttempt(value: unknown): QualificationAttempt | null {
  const r = mcpObject(value), cases = caseList(r?.cases, projectQualificationCaseResult);
  if (!r || !id(r.id) || !id(r.suiteId) || !id(r.bindingId) || !id(r.agentId) || !integer(r.revision, 1) || !sha(r.bindingSha256) || !member(r.status, QUALIFICATION_ATTEMPT_STATES) ||
      ![r.requestedSuiteSha256, r.suiteSha256, r.configurationSha256].every(sha) || !integer(r.caseCount, 1, 8) || !cases || cases.length !== r.caseCount ||
      ![r.completedCases, r.passedCases, r.failedCases].every(value => integer(value, 0, r.caseCount as number)) || !actions(r.actions, ["cancel"]) ||
      !(r.reportId === null || id(r.reportId)) || !(r.reportSha256 === null || sha(r.reportSha256)) || !(r.reasonCode === null || code(r.reasonCode)) ||
      typeof r.recoveryAvailable !== "boolean" || !timestamp(r.createdAt) || !timestamp(r.updatedAt)) return null;
  if ((r.reportId === null) !== (r.reportSha256 === null) || r.passedCases !== cases.filter(item => item.status === "passed").length || r.failedCases !== cases.filter(item => item.status === "failed").length ||
      r.completedCases !== (r.passedCases as number) + (r.failedCases as number) || Date.parse(r.updatedAt) < Date.parse(r.createdAt) || r.actions.includes("cancel") && !["requested", "running", "recovering"].includes(r.status)) return null;
  if (r.status === "succeeded" && (r.reportId === null || r.passedCases !== r.caseCount || r.failedCases !== 0)) return null;
  return { id: r.id, suiteId: r.suiteId, bindingId: r.bindingId, agentId: r.agentId, revision: r.revision, bindingSha256: r.bindingSha256, status: r.status,
    requestedSuiteSha256: r.requestedSuiteSha256 as string, suiteSha256: r.suiteSha256 as string, configurationSha256: r.configurationSha256 as string,
    caseCount: r.caseCount, completedCases: r.completedCases as number, passedCases: r.passedCases as number, failedCases: r.failedCases as number, cases,
    reportId: r.reportId, reportSha256: r.reportSha256, reasonCode: r.reasonCode, actions: [...r.actions], recoveryAvailable: r.recoveryAvailable, createdAt: r.createdAt, updatedAt: r.updatedAt };
}

export function projectQualificationEvidence(value: unknown): QualificationEvidence | null {
  const r = mcpObject(value), task = projectQualificationTaskBinding(r?.taskBinding);
  const hashes = ["suiteSha256", "suiteFileSha256", "consumerSha256", "configurationSha256", "installationSha256", "definitionSha256", "runtimeSha256", "imageSha256", "qualificationSha256", "storageSha256", "referenceSha256", "casesSha256"] as const;
  if (!r || !keys(r, ["schema", "workspaceId", "principalId", "attemptId", "agentId", ...hashes, "agentHomeRevision", "taskBinding", "observedAt", "completedAt"]) || r.schema !== MCP_QUALIFICATION_REPORT_SCHEMA ||
      ![r.workspaceId, r.principalId, r.attemptId, r.agentId].every(id) || !hashes.every(name => sha(r[name])) || !integer(r.agentHomeRevision, 1) || !task ||
      !timestamp(r.observedAt) || !timestamp(r.completedAt) || Date.parse(r.completedAt) < Date.parse(r.observedAt)) return null;
  return { schema: MCP_QUALIFICATION_REPORT_SCHEMA, workspaceId: r.workspaceId as string, principalId: r.principalId as string, attemptId: r.attemptId as string, agentId: r.agentId as string,
    ...Object.fromEntries(hashes.map(name => [name, r[name]])) as Record<typeof hashes[number], string>, agentHomeRevision: r.agentHomeRevision, taskBinding: task, observedAt: r.observedAt, completedAt: r.completedAt };
}

export function projectQualificationReport(value: unknown): QualificationReport | null {
  const r = mcpObject(value), evidence = projectQualificationEvidence(r?.evidence), cases = caseList(r?.cases, projectQualificationCaseResult);
  if (!r || !id(r.id) || !id(r.suiteId) || !id(r.attemptId) || !id(r.bindingId) || !sha(r.sha256) || typeof r.approvalValid !== "boolean" || typeof r.complete !== "boolean" || typeof r.passed !== "boolean" ||
      typeof r.authorityCurrent !== "boolean" || !codes(r.reasonCodes) || !evidence || evidence.attemptId !== r.attemptId || !cases || !timestamp(r.recordedAt)) return null;
  if (r.complete !== cases.every(item => ["passed", "failed"].includes(item.status)) || r.passed !== (r.complete && cases.every(item => item.status === "passed")) ||
      r.authorityCurrent !== (r.reasonCodes.length === 0) || r.authorityCurrent && !r.approvalValid) return null;
  return { id: r.id, suiteId: r.suiteId, attemptId: r.attemptId, bindingId: r.bindingId, sha256: r.sha256, approvalValid: r.approvalValid, complete: r.complete,
    passed: r.passed, authorityCurrent: r.authorityCurrent, reasonCodes: [...r.reasonCodes], evidence, cases, recordedAt: r.recordedAt };
}

function scope(value: unknown, workspaceId: string, principalId?: string): QualificationScope | null {
  const r = mcpObject(value); return r && r.schema === MCP_QUALIFICATION_SCHEMA && r.workspaceId === workspaceId && id(r.workspaceId) && id(r.principalId) && (principalId === undefined || r.principalId === principalId) ?
    { schema: MCP_QUALIFICATION_SCHEMA, workspaceId: r.workspaceId, principalId: r.principalId } : null;
}
function page<T extends { id: string }>(value: unknown, workspaceId: string, principalId: string | undefined, project: (raw: unknown) => T | null): QualificationPage<T> | null {
  const r = mcpObject(value), s = scope(value, workspaceId, principalId);
  if (!r || !s || !Array.isArray(r.items) || r.items.length > 50 || !(r.nextCursor === null || id(r.nextCursor))) return null;
  const items = r.items.map(project); if (items.some(item => !item) || new Set(items.map(item => item?.id)).size !== items.length || items.some((item, index) => index > 0 && item!.id <= items[index - 1]!.id) || r.nextCursor !== null && items.at(-1)?.id !== r.nextCursor) return null;
  return { ...s, items: items as T[], nextCursor: r.nextCursor };
}
export function projectQualificationSuites(value: unknown, workspaceId: string, principalId?: string, bindingId?: string): QualificationPage<QualificationSuite> | null {
  const p = page(value, workspaceId, principalId, projectQualificationSuite); return p && (bindingId === undefined || p.items.every(item => item.bindingId === bindingId)) ? p : null;
}
export function projectQualificationAttempts(value: unknown, workspaceId: string, principalId?: string, suiteId?: string): QualificationPage<QualificationAttempt> | null {
  const p = page(value, workspaceId, principalId, projectQualificationAttempt); return p && (suiteId === undefined || p.items.every(item => item.suiteId === suiteId)) ? p : null;
}
export function projectQualificationSuiteEnvelope(value: unknown, workspaceId: string, suiteId?: string, principalId?: string): (QualificationScope & { suite: QualificationSuite }) | null {
  const r = mcpObject(value), s = scope(value, workspaceId, principalId), item = projectQualificationSuite(r?.suite); return s && item && (suiteId === undefined || item.id === suiteId) ? { ...s, suite: item } : null;
}
export function projectQualificationAttemptEnvelope(value: unknown, workspaceId: string, attemptId?: string, principalId?: string): (QualificationScope & { attempt: QualificationAttempt }) | null {
  const r = mcpObject(value), s = scope(value, workspaceId, principalId), item = projectQualificationAttempt(r?.attempt); return s && item && (attemptId === undefined || item.id === attemptId) ? { ...s, attempt: item } : null;
}
export function projectQualificationReportEnvelope(value: unknown, workspaceId: string, reportId?: string, principalId?: string): (QualificationScope & { report: QualificationReport }) | null {
  const r = mcpObject(value), s = scope(value, workspaceId, principalId), item = projectQualificationReport(r?.report);
  return s && item && (reportId === undefined || item.id === reportId) && item.evidence.workspaceId === s.workspaceId && item.evidence.principalId === s.principalId ? { ...s, report: item } : null;
}

export function validateQualificationMutation(path: string[], value: unknown): QualificationDecision | null {
  const r = mcpObject(value);
  if (path.length !== 3 || !id(path[1]) || !(path[0] === "suites" && member(path[2], QUALIFICATION_SUITE_ACTIONS) || path[0] === "attempts" && path[2] === "cancel") ||
      !r || !keys(r, ["revision", "digest", "confirm"]) || !integer(r.revision, 1, Number.MAX_SAFE_INTEGER - 1) || !sha(r.digest) || r.confirm !== true) return null;
  return { revision: r.revision, digest: r.digest, confirm: true };
}
export function projectQualificationMutation(value: unknown, workspaceId: string, path: string[], body: unknown, principalId?: string): (QualificationScope & ({ suite: QualificationSuite } | { attempt: QualificationAttempt })) | null {
  const request = validateQualificationMutation(path, body); if (!request) return null;
  if (path[0] === "suites" && path[2] !== "qualify") {
    const result = projectQualificationSuiteEnvelope(value, workspaceId, path[1], principalId), state = { enable: "enabled", disable: "disabled", revoke: "revoked" }[path[2]];
    return result && result.suite.revision === request.revision + 1 && result.suite.state === state ? result : null;
  }
  const result = projectQualificationAttemptEnvelope(value, workspaceId, path[0] === "attempts" ? path[1] : undefined, principalId); if (!result) return null;
  if (path[2] === "qualify") return result.attempt.suiteId === path[1] && result.attempt.requestedSuiteSha256 === request.digest && result.attempt.revision === 1 && result.attempt.status === "requested" ? result : null;
  return result.attempt.revision === request.revision + 1 && result.attempt.status === "cancelled" ? result : null;
}
