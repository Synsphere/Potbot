/** Strict public projections for retained MCP observations; no invocation authority. */
import { UUID, SHA256 } from "./operation-workbench.ts";
export { UUID, SHA256 };
export const MCP_DISCOVERY_SCHEMA = "potbot-mcp-discovery/v1" as const;
export const MCP_REFERENCE_SCHEMA = "potbot-mcp-reference/v1" as const;
export const MAX_MCP_REQUEST = 8192, MAX_MCP_RESPONSE = 1_572_864;
export const MCP_KINDS = ["tool", "resource", "resource_template", "prompt"] as const;
export const MCP_BINDING_STATES = ["enabled", "disabled", "revoked"] as const;
export const MCP_ATTEMPT_STATES = ["requested", "running", "succeeded", "failed", "cancelled", "indeterminate"] as const;
export const MCP_BINDING_ACTIONS = ["enable", "disable", "revoke", "reconcile"] as const;
const PROTOCOLS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26"];
const COLLECTIONS = ["tools", "resources", "resourceTemplates", "prompts"] as const;
const NOTIFICATIONS = ["tools", "resources", "prompts"] as const;
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };
export type McpKind = typeof MCP_KINDS[number];
export type McpScope = { schema: typeof MCP_DISCOVERY_SCHEMA; workspaceId: string; principalId: string };
export type McpPage<T> = McpScope & { items: T[]; nextCursor: string | null };
export type McpCatalogInfo = { protocolVersion: string; serverInfo: JsonObject | null; capabilities: JsonObject;
  collections: Record<typeof COLLECTIONS[number], "observed" | "not_advertised">;
  notificationSupport: Record<typeof NOTIFICATIONS[number], boolean>; cacheTtlMs: number | null };
export type McpBinding = { id: string; name: string; agentId: string; revision: number; state: typeof MCP_BINDING_STATES[number]; bindingSha256: string;
  approvalValid: boolean; installationSha256: string; configurationSha256: string; definitionSha256: string; toolName: string; toolFingerprint: string;
  transport: "stdio" | "streamable_http"; ttlSeconds: number; watchMs: number; snapshotId: string | null; snapshotSha256: string | null;
  fetchedAt: string | null; expiresAt: string | null; stale: boolean; lastErrorCode: string | null; activeAttemptId: string | null; lastAttemptId: string | null;
  counts: Record<typeof COLLECTIONS[number], number>; catalogInfo: McpCatalogInfo | null; actions: typeof MCP_BINDING_ACTIONS[number][]; createdAt: string; updatedAt: string };
export type McpEntry = { id: string; bindingId: string; snapshotId: string; snapshotSha256: string; kind: McpKind; identity: string; name: string; description: string;
  definition: JsonObject; definitionSha256: string; configuredTool: boolean; referenceAvailable: boolean; reasonCodes: string[]; executable: false };
export type McpChanges = { added: number; removed: number; changed: number; unchanged: number };
export type McpAttempt = { id: string; bindingId: string; agentId: string; revision: number; bindingSha256: string; status: typeof MCP_ATTEMPT_STATES[number];
  requestedBindingSha256: string; installationSha256: string; configurationSha256: string; snapshotId: string | null; snapshotSha256: string | null;
  changes: McpChanges | null; reasonCode: string | null; actions: "cancel"[]; createdAt: string; updatedAt: string };
export type McpReference = { schema: typeof MCP_REFERENCE_SCHEMA; workspaceId: string; principalId: string; bindingId: string; bindingRevision: number;
  bindingSha256: string; installationSha256: string; snapshotId: string; snapshotSha256: string; entryId: string; kind: McpKind; identity: string; definitionSha256: string; expiresAt: string };
export type McpDecision = { revision: number; digest: string; confirm: true };

/** Reject ambiguous JSON keys before any scoped projection or journal replay. */
export function parseMcpJSON(source: string): unknown {
  const value = JSON.parse(source, (_key, node: unknown) => { if (typeof node === "number" && !Number.isFinite(node)) throw new Error("Nonfinite MCP JSON"); return node; });
  const stack: (Set<string> | null)[] = [];
  for (let index = 0; index < source.length; index++) {
    const char = source[index];
    if (char === "{" || char === "[") { stack.push(char === "{" ? new Set() : null); if (stack.length > 64) throw new Error("MCP JSON nesting exceeds bound"); }
    else if (char === "}" || char === "]") stack.pop();
    else if (char === '"') {
      const start = index++;
      while (source[index] !== '"') { if (source[index] === "\\") index++; index++; }
      let next = index + 1; while (/\s/.test(source[next] ?? "") && next < source.length) next++;
      if (source[next] === ":") {
        const keys = stack.at(-1), key: string = JSON.parse(source.slice(start, index + 1));
        if (!keys || keys.has(key)) throw new Error("Duplicate MCP JSON key"); keys.add(key);
      }
    }
  }
  return value;
}

export function mcpObject(value: unknown): Record<string, unknown> | null { return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null; }
function keys(row: Record<string, unknown>, expected: readonly string[]): boolean { return Object.keys(row).sort().join("\0") === [...expected].sort().join("\0"); }
function text(value: unknown, maximum: number, empty = false, multiline = false): value is string { return typeof value === "string" && Array.from(value).length <= maximum && (empty || value.length > 0) && !(multiline ? /[\u0000-\u0008\u000b-\u001f\u007f]/ : /[\u0000-\u001f\u007f]/).test(value); }
function id(value: unknown): value is string { return typeof value === "string" && UUID.test(value); }
function sha(value: unknown): value is string { return typeof value === "string" && SHA256.test(value); }
function integer(value: unknown, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): value is number { return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum && value <= maximum; }
function timestamp(value: unknown): value is string { return typeof value === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value)); }
function code(value: unknown): value is string { return typeof value === "string" && /^[a-z][a-z0-9_]{0,95}$/.test(value); }
function member<T extends string>(value: unknown, values: readonly T[]): value is T { return typeof value === "string" && values.includes(value as T); }
function array<T extends string>(value: unknown, values: readonly T[], maximum: number): value is T[] { return Array.isArray(value) && value.length <= maximum && new Set(value).size === value.length && value.every(item => member(item, values)); }
function bounded(value: unknown, maximum: number): value is Json {
  let nodes = 0;
  function check(node: unknown, depth: number): boolean {
    if (++nodes > 8192 || depth > 32) return false;
    if (node === null || typeof node === "string" || typeof node === "boolean") return true;
    if (typeof node === "number") return Number.isFinite(node);
    if (Array.isArray(node)) return node.every(child => check(child, depth + 1));
    const row = mcpObject(node); return !!row && Object.entries(row).every(([key, child]) => Array.from(key).length <= 512 && check(child, depth + 1));
  }
  try { return check(value, 0) && new TextEncoder().encode(JSON.stringify(value)).length <= maximum; } catch { return false; }
}
function jsonObject(value: unknown, maximum: number): value is JsonObject { return mcpObject(value) !== null && bounded(value, maximum); }

export function projectMcpCatalogInfo(value: unknown): McpCatalogInfo | null {
  const row = mcpObject(value), collections = row && mcpObject(row.collections), support = row && mcpObject(row.notificationSupport);
  if (!row || !member(row.protocolVersion, PROTOCOLS) || !collections || !keys(collections, COLLECTIONS) || !COLLECTIONS.every(name => member(collections[name], ["observed", "not_advertised"])) ||
      !support || !keys(support, NOTIFICATIONS) || !NOTIFICATIONS.every(name => typeof support[name] === "boolean") || !jsonObject(row.capabilities, 65_536) ||
      !(row.cacheTtlMs === null || integer(row.cacheTtlMs))) return null;
  const info = row.serverInfo;
  if (info !== null && (!jsonObject(info, 16_384) || !text(info.name, 256) || !text(info.version, 128) || ("title" in info && !text(info.title, 512)))) return null;
  for (const claim of Object.values(row.capabilities)) if (!mcpObject(claim)) return null;
  for (const name of COLLECTIONS) if (collections[name] !== (Object.hasOwn(row.capabilities, name === "resourceTemplates" ? "resources" : name) ? "observed" : "not_advertised")) return null;
  for (const name of NOTIFICATIONS) {
    const claim = mcpObject(row.capabilities[name]) ?? {};
    for (const field of ["listChanged", "subscribe"]) if (field in claim && typeof claim[field] !== "boolean") return null;
    if (support[name] !== (claim.listChanged === true)) return null;
  }
  return { protocolVersion: row.protocolVersion, serverInfo: info as JsonObject | null, capabilities: row.capabilities,
    collections: Object.fromEntries(COLLECTIONS.map(name => [name, collections[name]])) as McpCatalogInfo["collections"],
    notificationSupport: Object.fromEntries(NOTIFICATIONS.map(name => [name, support[name]])) as McpCatalogInfo["notificationSupport"], cacheTtlMs: row.cacheTtlMs };
}

export function projectMcpBinding(value: unknown): McpBinding | null {
  const r = mcpObject(value), counts = r && mcpObject(r.counts), info = r?.catalogInfo === null ? null : projectMcpCatalogInfo(r?.catalogInfo);
  if (!r || !id(r.id) || !id(r.agentId) || !text(r.name, 96) || !integer(r.revision, 1) || !member(r.state, MCP_BINDING_STATES) || !sha(r.bindingSha256) || typeof r.approvalValid !== "boolean" ||
      !sha(r.installationSha256) || !sha(r.configurationSha256) || !sha(r.definitionSha256) || !text(r.toolName, 128) || !sha(r.toolFingerprint) || !member(r.transport, ["stdio", "streamable_http"]) ||
      !integer(r.ttlSeconds, 60, 3600) || !(r.watchMs === 0 || integer(r.watchMs, 250, 5000)) || r.watchMs !== 0 && r.transport !== "streamable_http" || !(r.snapshotId === null || id(r.snapshotId)) || !(r.snapshotSha256 === null || sha(r.snapshotSha256)) ||
      !(r.fetchedAt === null || timestamp(r.fetchedAt)) || !(r.expiresAt === null || timestamp(r.expiresAt)) || typeof r.stale !== "boolean" || !(r.lastErrorCode === null || code(r.lastErrorCode)) ||
      !(r.activeAttemptId === null || id(r.activeAttemptId)) || !(r.lastAttemptId === null || id(r.lastAttemptId)) || !counts || !keys(counts, COLLECTIONS) || !COLLECTIONS.every(name => integer(counts[name], 0, 256)) ||
      (r.catalogInfo !== null && !info) || !array(r.actions, MCP_BINDING_ACTIONS, 4) || !timestamp(r.createdAt) || !timestamp(r.updatedAt)) return null;
  if (r.state === "revoked" && r.actions.length || !r.approvalValid && r.actions.some(action => action !== "revoke") ||
      r.actions.includes("enable") && r.state !== "disabled" || r.actions.includes("disable") && r.state !== "enabled" ||
      r.actions.includes("reconcile") && (r.state !== "enabled" || !r.approvalValid || r.activeAttemptId !== null)) return null;
  if (r.fetchedAt !== null && r.expiresAt !== null && Date.parse(r.expiresAt) < Date.parse(r.fetchedAt)) return null;
  const observed = r.snapshotId !== null;
  if ([r.snapshotSha256, r.fetchedAt, r.expiresAt, info].some(value => (value !== null) !== observed) || !observed && (!r.stale || Object.values(counts).some(value => value !== 0))) return null;
  if (info && COLLECTIONS.some(name => info.collections[name] === "not_advertised" && counts[name] !== 0)) return null;
  return { id: r.id, name: r.name, agentId: r.agentId, revision: r.revision, state: r.state, bindingSha256: r.bindingSha256, approvalValid: r.approvalValid,
    installationSha256: r.installationSha256, configurationSha256: r.configurationSha256, definitionSha256: r.definitionSha256, toolName: r.toolName, toolFingerprint: r.toolFingerprint,
    transport: r.transport, ttlSeconds: r.ttlSeconds, watchMs: r.watchMs, snapshotId: r.snapshotId, snapshotSha256: r.snapshotSha256, fetchedAt: r.fetchedAt, expiresAt: r.expiresAt,
    stale: r.stale, lastErrorCode: r.lastErrorCode, activeAttemptId: r.activeAttemptId, lastAttemptId: r.lastAttemptId,
    counts: Object.fromEntries(COLLECTIONS.map(name => [name, counts[name]])) as McpBinding["counts"], catalogInfo: info, actions: [...r.actions], createdAt: r.createdAt, updatedAt: r.updatedAt };
}

export function projectMcpEntry(value: unknown): McpEntry | null {
  const r = mcpObject(value);
  if (!r || !id(r.id) || !id(r.bindingId) || !id(r.snapshotId) || !sha(r.snapshotSha256) || !member(r.kind, MCP_KINDS) || !text(r.identity, 2048) || !text(r.name, 128) || !text(r.description, 4096, true, true) ||
      !jsonObject(r.definition, 131_072) || !sha(r.definitionSha256) || typeof r.configuredTool !== "boolean" || typeof r.referenceAvailable !== "boolean" || r.executable !== false ||
      !Array.isArray(r.reasonCodes) || r.reasonCodes.length > 32 || !r.reasonCodes.every(code) || new Set(r.reasonCodes).size !== r.reasonCodes.length) return null;
  if (r.definition.name !== r.name || (r.definition.description ?? "") !== r.description || r.identity !== (r.kind === "resource" ? r.definition.uri : r.kind === "resource_template" ? r.definition.uriTemplate : r.name) || r.kind !== "tool" && r.configuredTool) return null;
  if (r.kind === "resource" && !/^[A-Za-z][A-Za-z0-9+.-]*:/.test(r.identity) || ["resource", "resource_template"].includes(r.kind) && /[\u0000-\u0020\u007f]/.test(r.identity)) return null;
  return { id: r.id, bindingId: r.bindingId, snapshotId: r.snapshotId, snapshotSha256: r.snapshotSha256, kind: r.kind, identity: r.identity, name: r.name, description: r.description,
    definition: r.definition, definitionSha256: r.definitionSha256, configuredTool: r.configuredTool, referenceAvailable: r.referenceAvailable, reasonCodes: [...r.reasonCodes], executable: false };
}

export function projectMcpAttempt(value: unknown): McpAttempt | null {
  const r = mcpObject(value), changes = r?.changes === null ? null : mcpObject(r?.changes);
  if (!r || !id(r.id) || !id(r.bindingId) || !id(r.agentId) || !integer(r.revision, 1) || !sha(r.bindingSha256) || !member(r.status, MCP_ATTEMPT_STATES) || !sha(r.requestedBindingSha256) ||
      !sha(r.installationSha256) || !sha(r.configurationSha256) || !(r.snapshotId === null || id(r.snapshotId)) || !(r.snapshotSha256 === null || sha(r.snapshotSha256)) ||
      !(r.reasonCode === null || code(r.reasonCode)) || !array(r.actions, ["cancel"], 1) || !timestamp(r.createdAt) || !timestamp(r.updatedAt)) return null;
  if (r.changes !== null && (!changes || !keys(changes, ["added", "removed", "changed", "unchanged"]) || !Object.values(changes).every(value => integer(value, 0, 1024)))) return null;
  if (r.actions.includes("cancel") && !["requested", "running"].includes(r.status)) return null;
  if ((r.snapshotId === null) !== (r.snapshotSha256 === null)) return null;
  return { id: r.id, bindingId: r.bindingId, agentId: r.agentId, revision: r.revision, bindingSha256: r.bindingSha256, status: r.status, requestedBindingSha256: r.requestedBindingSha256,
    installationSha256: r.installationSha256, configurationSha256: r.configurationSha256, snapshotId: r.snapshotId, snapshotSha256: r.snapshotSha256,
    changes: changes === null ? null : { added: changes.added as number, removed: changes.removed as number, changed: changes.changed as number, unchanged: changes.unchanged as number },
    reasonCode: r.reasonCode, actions: [...r.actions], createdAt: r.createdAt, updatedAt: r.updatedAt };
}

function scope(value: unknown, workspaceId: string, principalId?: string): McpScope | null {
  const r = mcpObject(value);
  return r && r.schema === MCP_DISCOVERY_SCHEMA && r.workspaceId === workspaceId && id(r.workspaceId) && id(r.principalId) && (principalId === undefined || r.principalId === principalId) ? { schema: MCP_DISCOVERY_SCHEMA, workspaceId: r.workspaceId, principalId: r.principalId } : null;
}
function page<T extends { id: string }>(value: unknown, workspaceId: string, principalId: string | undefined, project: (raw: unknown) => T | null): McpPage<T> | null {
  const r = mcpObject(value), s = scope(value, workspaceId, principalId);
  if (!r || !s || !Array.isArray(r.items) || r.items.length > 50 || !(r.nextCursor === null || id(r.nextCursor))) return null;
  const items = r.items.map(project); if (items.some(item => item === null) || new Set(items.map(item => item?.id)).size !== items.length) return null;
  if (items.some((item, index) => index > 0 && item!.id <= items[index - 1]!.id) || r.nextCursor !== null && items.at(-1)?.id !== r.nextCursor) return null;
  return { ...s, items: items as T[], nextCursor: r.nextCursor };
}
export function projectMcpBindings(value: unknown, workspaceId: string, principalId?: string): McpPage<McpBinding> | null { return page(value, workspaceId, principalId, projectMcpBinding); }
export function projectMcpAttempts(value: unknown, workspaceId: string, principalId?: string, bindingId?: string): McpPage<McpAttempt> | null {
  const p = page(value, workspaceId, principalId, projectMcpAttempt); return p && (bindingId === undefined || p.items.every(item => item.bindingId === bindingId)) ? p : null;
}
export function projectMcpEntries(value: unknown, workspaceId: string, bindingId: string, principalId?: string, snapshotId?: string): McpPage<McpEntry> | null {
  const p = page(value, workspaceId, principalId, projectMcpEntry);
  return p && p.items.every(item => item.bindingId === bindingId && (snapshotId === undefined || item.snapshotId === snapshotId)) && new Set(p.items.map(item => item.snapshotId + ":" + item.snapshotSha256)).size <= 1 ? p : null;
}
export function projectMcpBindingEnvelope(value: unknown, workspaceId: string, bindingId?: string, principalId?: string): (McpScope & { binding: McpBinding }) | null {
  const r = mcpObject(value), s = scope(value, workspaceId, principalId), item = projectMcpBinding(r?.binding); return s && item && (bindingId === undefined || item.id === bindingId) ? { ...s, binding: item } : null;
}
export function projectMcpAttemptEnvelope(value: unknown, workspaceId: string, attemptId?: string, principalId?: string): (McpScope & { attempt: McpAttempt }) | null {
  const r = mcpObject(value), s = scope(value, workspaceId, principalId), item = projectMcpAttempt(r?.attempt); return s && item && (attemptId === undefined || item.id === attemptId) ? { ...s, attempt: item } : null;
}
export function projectMcpReferenceEnvelope(value: unknown, workspaceId: string, bindingId: string, entryId: string, principalId?: string): (McpScope & { reference: McpReference }) | null {
  const r = mcpObject(value), s = scope(value, workspaceId, principalId), ref = mcpObject(r?.reference);
  if (!s || !ref || ref.schema !== MCP_REFERENCE_SCHEMA || ref.workspaceId !== s.workspaceId || ref.principalId !== s.principalId || ref.bindingId !== bindingId || !id(ref.bindingId) || ref.entryId !== entryId || !id(ref.entryId) ||
      !integer(ref.bindingRevision, 1) || !sha(ref.bindingSha256) || !sha(ref.installationSha256) || !id(ref.snapshotId) || !sha(ref.snapshotSha256) || !member(ref.kind, MCP_KINDS) || !text(ref.identity, 2048) || !sha(ref.definitionSha256) || !timestamp(ref.expiresAt)) return null;
  return { ...s, reference: { schema: MCP_REFERENCE_SCHEMA, workspaceId: s.workspaceId, principalId: s.principalId, bindingId: ref.bindingId, bindingRevision: ref.bindingRevision, bindingSha256: ref.bindingSha256,
    installationSha256: ref.installationSha256, snapshotId: ref.snapshotId, snapshotSha256: ref.snapshotSha256, entryId: ref.entryId, kind: ref.kind, identity: ref.identity, definitionSha256: ref.definitionSha256, expiresAt: ref.expiresAt } };
}

export function validateMcpMutation(path: string[], value: unknown): McpDecision | null {
  const r = mcpObject(value);
  if (path.length !== 3 || !id(path[1]) || !(path[0] === "bindings" && member(path[2], MCP_BINDING_ACTIONS) || path[0] === "attempts" && path[2] === "cancel") ||
      !r || !keys(r, ["revision", "digest", "confirm"]) || !integer(r.revision, 1, Number.MAX_SAFE_INTEGER - 1) || !sha(r.digest) || r.confirm !== true) return null;
  return { revision: r.revision, digest: r.digest, confirm: true };
}
export function projectMcpMutation(value: unknown, workspaceId: string, path: string[], body: unknown, principalId?: string): (McpScope & ({ binding: McpBinding } | { attempt: McpAttempt })) | null {
  const request = validateMcpMutation(path, body); if (!request) return null;
  if (path[0] === "bindings" && path[2] !== "reconcile") {
    const result = projectMcpBindingEnvelope(value, workspaceId, path[1], principalId), target = { enable: "enabled", disable: "disabled", revoke: "revoked" }[path[2]];
    return result && result.binding.revision === request.revision + 1 && result.binding.state === target ? result : null;
  }
  const result = projectMcpAttemptEnvelope(value, workspaceId, path[0] === "attempts" ? path[1] : undefined, principalId);
  if (!result) return null;
  if (path[2] === "reconcile") return result.attempt.bindingId === path[1] && result.attempt.requestedBindingSha256 === request.digest && result.attempt.revision === 1 && result.attempt.status === "requested" ? result : null;
  return result.attempt.revision === request.revision + 1 && result.attempt.status === "cancelled" ? result : null;
}
