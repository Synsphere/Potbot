/** Retained discovery observations and measured prerequisites confer no execution authority. */
import { UUID, SHA256 } from "./operation-workbench.ts";
import { resourcePath as validResourcePath } from "./skill-acquisition.ts";
export { UUID, SHA256 };
export const DISCOVERY_SCHEMA = "potbot-discovery/v1" as const;
export const MAX_DISCOVERY_RESPONSE = 1_572_864;
export const MAX_DISCOVERY_REQUEST = 8192;
// The server binds this opaque ranked cursor to the entire query and retained source heads.
export const DISCOVERY_SEARCH_CURSOR = /^[A-Za-z0-9_-]{1,512}$/;
export const ADAPTERS = ["curated", "clawhub", "mcp_registry", "skills_sh"] as const;
export const CANDIDATE_KINDS = ["skill", "mcp_server", "tool", "workflow", "plugin"] as const;
export const REQUIREMENT_KINDS = ["binary", "any_binary", "environment", "configuration", "operating_system", "tool", "compatibility"] as const;
export const SOURCE_ACTIONS = ["enable", "disable", "revoke"] as const;
export const PUBLISHER_ACTIONS = ["approve", "block", "revoke"] as const;
export const CHECK_ACTIONS = ["handoff", "cancel"] as const;
export const CHECK_STATES = ["awaiting_measurement", "measuring", "measured", "failed", "handed_off", "cancelled"] as const;
export const CAPABILITIES = ["repository_inspection", "code_search", "file_inspection", "text_search", "image_inspection", "document_analysis", "spreadsheet_analysis", "browser_inspection", "web_retrieval", "structured_api_read", "database_query", "satisfiability", "python_debugging", "remote_debugging", "hardware_debugging", "android_inspection", "apple_inspection", "profiling", "skill_authoring", "workflow_guidance"];
export const EFFECTS = ["read_only", "idempotent_write", "non_idempotent_write", "destructive"], PLATFORMS = ["linux", "darwin", "windows", "android", "ios", "remote"];
const PUBLISHER_EVIDENCE = ["registry_claim", "source_owner_claim", "operator_claim"] as const;
const SOURCE_STATES = ["active", "disabled", "revoked"] as const, PUBLISHER_STATES = ["unreviewed", "approved", "blocked", "revoked"] as const;
const COMMIT = /^[a-f0-9]{40}$/, IDENTIFIER = /^[a-z0-9][a-z0-9_.-]{0,127}$/;
const MEASUREMENT_REASONS = ["sandbox_probe_completed", "binary_available", "binary_missing", "binary_not_executable", "binary_hash_unavailable", "binary_measurement_limit", "binary_changed_during_check", "binary_name_unsupported", "binary_path_unsupported", "shared_tool_not_admitted", "shared_tool_held", "shared_tool_pin_mismatch", "any_binary_available", "any_binary_missing", "any_binary_unresolved", "environment_present", "environment_missing", "environment_injection_unsupported", "operating_system_matches", "operating_system_mismatch", "operating_system_unsupported", "configuration_gate_unsupported", "capability_alias_unsupported", "compatibility_text_unsupported"];
const PASSED_REASONS = ["sandbox_probe_completed", "binary_available", "any_binary_available", "environment_present", "operating_system_matches"];
const UNSUPPORTED_REASONS = ["binary_measurement_limit", "binary_name_unsupported", "any_binary_unresolved", "binary_path_unsupported", "environment_injection_unsupported", "operating_system_unsupported", "configuration_gate_unsupported", "capability_alias_unsupported", "compatibility_text_unsupported"];
export const DISCOVERY_FILTERS = ["q", "kind", "sourceId", "platform", "permittedEffects", "permittedScopes", "classes", "inputTypes", "outputTypes", "requireLocal"] as const;
const TYPE_REFERENCE = /^(?:syn|synny):\/\/type\/[a-z0-9]+(?:[-_][a-z0-9]+)*$/;

/** CSV omission is unconstrained; an explicit empty permission CSV permits none. */
export function discoverySearchParams(query: URLSearchParams): URLSearchParams | null {
  const result = new URLSearchParams();
  for (const key of DISCOVERY_FILTERS) {
    if (!query.has(key)) continue;
    if (query.getAll(key).length !== 1) return null;
    const value = query.get(key)!;
    if (new TextEncoder().encode(value).length > 4096 || /[\u0000-\u001f\u007f]/.test(value)) return null;
    if (key === "q") { if (Array.from(value).length > 500) return null; result.set(key, value); }
    else if (key === "kind") { if (!CANDIDATE_KINDS.includes(value as typeof CANDIDATE_KINDS[number])) return null; result.set(key, value); }
    else if (key === "sourceId") { if (!UUID.test(value)) return null; result.set(key, value); }
    else if (key === "platform") { if (!PLATFORMS.includes(value)) return null; result.set(key, value); }
    else if (key === "requireLocal") { if (!["true", "false"].includes(value)) return null; result.set(key, value); }
    else {
      const values = value === "" ? [] : value.split(",").map(item => item.trim());
      if (values.length > 32 || new Set(values).size !== values.length || values.some(item => !item || Array.from(item).length > 256)) return null;
      if (key === "permittedEffects" && !values.every(item => EFFECTS.includes(item))) return null;
      if (key === "permittedScopes" && !values.every(item => IDENTIFIER.test(item))) return null;
      if (key === "classes" && !values.every(item => CAPABILITIES.includes(item))) return null;
      if ((key === "inputTypes" || key === "outputTypes") && !values.every(item => TYPE_REFERENCE.test(item))) return null;
      result.set(key, values.join(","));
    }
  }
  return new TextEncoder().encode(query.toString()).length <= 8192 && new TextEncoder().encode(result.toString()).length <= 8192 ? result : null;
}
const MEASUREMENT_HASHES = ["runtimeSha256", "imageSha256", "qualificationSha256", "probeSha256", "interpreterSha256", "observationSha256", "isolationSha256"] as const;

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type DiscoveryScope = { schema: typeof DISCOVERY_SCHEMA; workspaceId: string; principalId: string };
export type Requirement = { kind: typeof REQUIREMENT_KINDS[number]; name: string; group?: string };
export type Distribution = { kind: "inspect_only" } | { kind: "github_skill"; repository: string; commit: string; path: string } |
  { kind: "tool_recipe"; repository: string; commit: string; recipeId: string } |
  { kind: "clawhub_skill"; owner: string; slug: string; version: string; files: { path: string; size: number; sha256: string }[] } |
  { kind: "skills_sh_snapshot"; id: string; responseSha256: string; files: { path: string; size: number; sha256: string }[] };
export type RegistryObservation = { schema: "potbot-registry-candidate/v1"; adapter: typeof ADAPTERS[number]; canonicalId: string; kind: typeof CANDIDATE_KINDS[number];
  version: string | null; name: string; description: string; publisher: { id: string; evidence: typeof PUBLISHER_EVIDENCE[number] }; sourceUrl: string;
  status: "active" | "deprecated" | "deleted" | "unresolved" | "unsupported"; capabilities: { classes: string[]; inputTypes: string[]; outputTypes: string[]; effects: string[]; scopes: string[]; platforms: string[] };
  requirements: Requirement[]; distribution: Distribution; manifest: { [key: string]: Json }; manifestSha256: string; sourceSha256: string; unsupported: string[] };
export type DiscoverySource = { id: string; name: string; adapter: typeof ADAPTERS[number]; revision: number; state: typeof SOURCE_STATES[number]; configSha256: string;
  bindingSha256: string; approvalValid: boolean; snapshotId: string | null; snapshotSha256: string | null; fetchedAt: string | null; expiresAt: string | null;
  query: string | null; lastErrorCode: string | null; createdAt: string; updatedAt: string; actions: typeof SOURCE_ACTIONS[number][] };
export type DiscoveryPublisher = { id: string; sourceId: string; name: string; revision: number; state: typeof PUBLISHER_STATES[number]; bindingSha256: string;
  approvalValid: boolean; evidence: typeof PUBLISHER_EVIDENCE[number]; updatedAt: string; actions: typeof PUBLISHER_ACTIONS[number][] };
export type DiscoveryCandidate = { id: string; sourceId: string; snapshotId: string; candidateSha256: string; observation: RegistryObservation; sourceState: typeof SOURCE_STATES[number];
  publisherId: string; publisherState: typeof PUBLISHER_STATES[number]; sourceApprovalValid: boolean; publisherApprovalValid: boolean; stale: boolean; canReview: boolean;
  reasonCodes: string[]; executable: false; score: number | null; scoreReasons: string[] };
export type PrerequisiteMeasurement = { schema: "potbot-skill-eligibility/v1"; checkId: string; agentId: string; candidateSha256: string; sourceBindingSha256: string;
  requirementsSha256: string; checkedAt: string; expiresAt: string; eligible: boolean; checks: { kind: "runtime" | Requirement["kind"]; name: string; status: "passed" | "blocked" | "unsupported"; reasonCode: string }[];
  bindings: { workspaceId: string; principalId: string; agentHomeRevision: number } & Record<typeof MEASUREMENT_HASHES[number], string> };
export type DiscoveryCheck = { id: string; candidateId: string; sourceId: string; snapshotId: string; requestedCandidateSha256: string; candidateSha256: string; revision: number;
  bindingSha256: string; agentId: string; status: typeof CHECK_STATES[number]; candidate: DiscoveryCandidate; requirements: Requirement[]; packageId: string | null; recipeId: string | null;
  measurement: PrerequisiteMeasurement | null; measurementCurrent: boolean; target: { kind: "skill_package" | "tool_acquisition"; id: string } | null;
  reasonCodes: string[]; actions: typeof CHECK_ACTIONS[number][]; createdAt: string; updatedAt: string };
export type DiscoveryPage<T> = DiscoveryScope & { items: T[]; nextCursor: string | null };
export type SourceEnvelope = DiscoveryScope & { source: DiscoverySource };
export type PublisherEnvelope = DiscoveryScope & { publisher: DiscoveryPublisher };
export type CandidateEnvelope = DiscoveryScope & { candidate: DiscoveryCandidate };
export type CheckEnvelope = DiscoveryScope & { check: DiscoveryCheck };

export function discoveryObject(value: unknown): Record<string, unknown> | null { return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null; }
function text(value: unknown, max: number, empty = false, multiline = false): value is string {
  return typeof value === "string" && Array.from(value).length <= max && (empty || (multiline ? value.length > 0 : value.trim().length > 0)) &&
    !(multiline ? /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/ : /[\u0000-\u001f\u007f]/).test(value) && new TextDecoder().decode(new TextEncoder().encode(value)) === value;
}
function id(value: unknown): value is string { return typeof value === "string" && UUID.test(value); }
function sha(value: unknown): value is string { return typeof value === "string" && SHA256.test(value); }
function number(value: unknown, maximum = Number.MAX_SAFE_INTEGER, minimum = 0): value is number { return Number.isSafeInteger(value) && Number(value) >= minimum && Number(value) <= maximum; }
function stamp(value: unknown): value is string { return text(value, 64) && /^\d{4}-\d\d-\d\dT/.test(value) && /(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value)); }
function keys(row: Record<string, unknown>, fields: string[]): boolean { return Object.keys(row).sort().join(",") === fields.slice().sort().join(","); }
function strings(value: unknown, maximum = 64, size = 256): string[] | null { return Array.isArray(value) && value.length <= maximum && value.every(item => text(item, size)) && new Set(value).size === value.length ? value as string[] : null; }
function choices<T extends string>(value: unknown, allowed: readonly T[]): T[] | null { const result = strings(value, allowed.length, 96); return result?.every(item => allowed.includes(item as T)) ? result as T[] : null; }
function codes(value: unknown): string[] | null { return strings(value, 64, 96); }
function scope(value: unknown, workspace: string, principal?: string): DiscoveryScope | null {
  const row = discoveryObject(value); return row?.schema === DISCOVERY_SCHEMA && row.workspaceId === workspace && id(workspace) && id(row.principalId) && (!principal || row.principalId === principal) ?
    { schema: DISCOVERY_SCHEMA, workspaceId: workspace, principalId: row.principalId } : null;
}
function json(value: unknown, maximum = 128 * 1024): value is Json {
  let remaining = 8192;
  const visit = (item: unknown, depth: number): boolean => {
    if (--remaining < 0 || depth > 20) return false;
    if (item === null || typeof item === "boolean") return true;
    if (typeof item === "number") return Number.isFinite(item) && Math.abs(item) <= Number.MAX_SAFE_INTEGER;
    if (typeof item === "string") return !item.includes("\u0000") && new TextDecoder().decode(new TextEncoder().encode(item)) === item;
    if (Array.isArray(item)) return item.every(child => visit(child, depth + 1));
    if (!item || typeof item !== "object" || ![Object.prototype, null].includes(Object.getPrototypeOf(item))) return false;
    return Object.entries(item).every(([key, child]) => visit(key, depth + 1) && visit(child, depth + 1));
  };
  try { return visit(value, 0) && new TextEncoder().encode(JSON.stringify(value)).length <= maximum; } catch { return false; }
}
function github(value: unknown): value is string { return text(value, 300) && /^https:\/\/github\.com\/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(value) && !value.endsWith(".git"); }
export function discoverySourceUrl(value: unknown, local = false): value is string {
  if (!text(value, 2048) || /\s|\\/.test(value)) return false;
  try { const url = new URL(value); return !url.hash && ((local && url.protocol === "potbot:" && url.host === "curated" && url.pathname.startsWith("/") && !url.search) ||
    (url.protocol === "https:" && Boolean(url.hostname) && !url.username && !url.password && (!url.port || url.port === "443"))); } catch { return false; }
}
function requirements(value: unknown, measured = false): Requirement[] | null {
  if (!Array.isArray(value) || value.length > 128) return null;
  const result: Requirement[] = [], seen = new Set<string>();
  for (const raw of value) { const row = discoveryObject(raw); if (!row || (!keys(row, ["kind", "name"]) && !keys(row, ["kind", "name", "group"])) ||
      !REQUIREMENT_KINDS.includes(row.kind as Requirement["kind"]) || !text(row.name, 512, false, measured) || (row.group !== undefined && (!["any_binary", "operating_system"].includes(String(row.kind)) || row.group !== row.kind))) return null;
    const identity = JSON.stringify([row.kind, row.name]); if (seen.has(identity)) return null; seen.add(identity);
    result.push({ kind: row.kind as Requirement["kind"], name: row.name, ...(row.group === undefined ? {} : { group: row.group as string }) }); }
  return new TextEncoder().encode(JSON.stringify(result)).length <= 65_536 ? result : null;
}
function inventory(value: unknown): { path: string; size: number; sha256: string }[] | null {
  if (!Array.isArray(value) || value.length < 1 || value.length > 128) return null;
  const result: { path: string; size: number; sha256: string }[] = []; let bytes = 0;
  for (const raw of value) { const row = discoveryObject(raw); if (!row || !keys(row, ["path", "size", "sha256"]) || !validResourcePath(row.path) || !number(row.size, 1024 ** 2) || !sha(row.sha256) || result.some(item => item.path.toLowerCase() === (row.path as string).toLowerCase())) return null;
    bytes += row.size; result.push({ path: row.path, size: row.size, sha256: row.sha256 }); }
  return bytes <= 8 * 1024 ** 2 && result.some(item => item.path === "SKILL.md") ? result : null;
}
function distribution(value: unknown, adapter: string, kind: string, version: string | null): Distribution | null {
  const row = discoveryObject(value); if (!row) return null;
  if (row.kind === "inspect_only") return keys(row, ["kind"]) ? { kind: "inspect_only" } : null;
  if (version === null) return null;
  if (row.kind === "github_skill" && keys(row, ["kind", "repository", "commit", "path"]) && adapter === "curated" && kind === "skill" && github(row.repository) && typeof row.commit === "string" && COMMIT.test(row.commit) && typeof row.path === "string" && (row.path === "" || validResourcePath(row.path))) return { kind: row.kind, repository: row.repository, commit: row.commit, path: row.path };
  if (row.kind === "tool_recipe" && keys(row, ["kind", "repository", "commit", "recipeId"]) && adapter === "curated" && kind === "tool" && github(row.repository) && typeof row.commit === "string" && COMMIT.test(row.commit) && id(row.recipeId)) return { kind: row.kind, repository: row.repository, commit: row.commit, recipeId: row.recipeId };
  if (row.kind === "clawhub_skill" && keys(row, ["kind", "owner", "slug", "version", "files"]) && adapter === "clawhub" && kind === "skill" && typeof row.owner === "string" && IDENTIFIER.test(row.owner) && typeof row.slug === "string" && IDENTIFIER.test(row.slug) && row.version === version) { const files = inventory(row.files); return files ? { kind: row.kind, owner: row.owner, slug: row.slug, version, files } : null; }
  if (row.kind === "skills_sh_snapshot" && keys(row, ["kind", "id", "files", "responseSha256"]) && adapter === "skills_sh" && kind === "skill" && text(row.id, 320) && sha(row.responseSha256) && version === "snapshot:" + row.responseSha256) { const files = inventory(row.files); return files ? { kind: row.kind, id: row.id, responseSha256: row.responseSha256, files } : null; }
  return null;
}
export function projectRegistryObservation(value: unknown): RegistryObservation | null {
  const row = discoveryObject(value), publisher = row && discoveryObject(row.publisher), capability = row && discoveryObject(row.capabilities);
  if (!row || !json(row) || !keys(row, ["schema", "adapter", "canonicalId", "kind", "version", "name", "description", "publisher", "sourceUrl", "status", "capabilities", "requirements", "distribution", "manifest", "manifestSha256", "sourceSha256", "unsupported"]) ||
      row.schema !== "potbot-registry-candidate/v1" || !ADAPTERS.includes(row.adapter as typeof ADAPTERS[number]) || !CANDIDATE_KINDS.includes(row.kind as typeof CANDIDATE_KINDS[number]) ||
      !text(row.canonicalId, 320) || !text(row.name, 128) || !text(row.description, 4096, true) || !(row.version === null || (text(row.version, 128) && !["latest", "main", "master", "head", "*"].includes(row.version.toLowerCase()))) ||
      !publisher || !keys(publisher, ["id", "evidence"]) || !text(publisher.id, 320) || !PUBLISHER_EVIDENCE.includes(publisher.evidence as typeof PUBLISHER_EVIDENCE[number]) ||
      !discoverySourceUrl(row.sourceUrl, row.adapter === "curated") || !["active", "deprecated", "deleted", "unresolved", "unsupported"].includes(String(row.status)) ||
      !capability || !keys(capability, ["classes", "inputTypes", "outputTypes", "effects", "scopes", "platforms"]) || !discoveryObject(row.manifest) || !sha(row.manifestSha256) || !sha(row.sourceSha256)) return null;
  const classes = choices(capability.classes, CAPABILITIES), effects = choices(capability.effects, EFFECTS), platforms = choices(capability.platforms, PLATFORMS), inputTypes = strings(capability.inputTypes), outputTypes = strings(capability.outputTypes), scopes = strings(capability.scopes, 64, 128);
  const required = requirements(row.requirements), unsupported = strings(row.unsupported, 128, 256), target = distribution(row.distribution, row.adapter as string, row.kind as string, row.version);
  if (!classes || !effects || !platforms || !inputTypes || !outputTypes || !scopes || !scopes.every(item => IDENTIFIER.test(item)) || !required || !unsupported || !target ||
      ![...inputTypes, ...outputTypes].every(item => TYPE_REFERENCE.test(item))) return null;
  return { schema: "potbot-registry-candidate/v1", adapter: row.adapter as RegistryObservation["adapter"], canonicalId: row.canonicalId, kind: row.kind as RegistryObservation["kind"], version: row.version,
    name: row.name, description: row.description, publisher: { id: publisher.id, evidence: publisher.evidence as RegistryObservation["publisher"]["evidence"] }, sourceUrl: row.sourceUrl,
    status: row.status as RegistryObservation["status"], capabilities: { classes, inputTypes, outputTypes, effects, scopes, platforms }, requirements: required, distribution: target,
    manifest: row.manifest as RegistryObservation["manifest"], manifestSha256: row.manifestSha256, sourceSha256: row.sourceSha256, unsupported };
}

export function projectDiscoverySource(value: unknown): DiscoverySource | null {
  const row = discoveryObject(value), actions = row && choices(row.actions, SOURCE_ACTIONS);
  if (!row || !actions || !id(row.id) || !text(row.name, 96) || !ADAPTERS.includes(row.adapter as typeof ADAPTERS[number]) || !number(row.revision, Number.MAX_SAFE_INTEGER - 1, 1) ||
      !SOURCE_STATES.includes(row.state as typeof SOURCE_STATES[number]) || !sha(row.configSha256) || !sha(row.bindingSha256) || typeof row.approvalValid !== "boolean" ||
      !(row.snapshotId === null || id(row.snapshotId)) || !(row.snapshotSha256 === null || sha(row.snapshotSha256)) || !(row.fetchedAt === null || stamp(row.fetchedAt)) || !(row.expiresAt === null || stamp(row.expiresAt)) ||
      !(row.query === null || text(row.query, 500, true)) || !(row.lastErrorCode === null || text(row.lastErrorCode, 96)) || !stamp(row.createdAt) || !stamp(row.updatedAt)) return null;
  // An invalidated snapshot retains its UUID while its unverifiable metadata is withheld.
  const emptySnapshot = row.snapshotSha256 === null && row.fetchedAt === null && row.expiresAt === null;
  if (!emptySnapshot && (row.snapshotId === null || !row.snapshotSha256 || !row.fetchedAt || !row.expiresAt)) return null;
  if ((row.state === "revoked" && actions.length) || (!row.approvalValid && actions.some(action => action !== "revoke"))) return null;
  if (row.fetchedAt !== null && row.expiresAt !== null && !(Date.parse(row.expiresAt) > Date.parse(row.fetchedAt) && Date.parse(row.expiresAt) - Date.parse(row.fetchedAt) <= 86_400_000)) return null;
  return { id: row.id, name: row.name, adapter: row.adapter as DiscoverySource["adapter"], revision: row.revision, state: row.state as DiscoverySource["state"], configSha256: row.configSha256,
    bindingSha256: row.bindingSha256, approvalValid: row.approvalValid, snapshotId: row.snapshotId, snapshotSha256: row.snapshotSha256, fetchedAt: row.fetchedAt, expiresAt: row.expiresAt,
    query: row.query, lastErrorCode: row.lastErrorCode, createdAt: row.createdAt, updatedAt: row.updatedAt, actions };
}
export function projectDiscoveryPublisher(value: unknown, sourceId?: string): DiscoveryPublisher | null {
  const row = discoveryObject(value), actions = row && choices(row.actions, PUBLISHER_ACTIONS);
  if (!row || !actions || !id(row.id) || !id(row.sourceId) || (sourceId && row.sourceId !== sourceId) || !text(row.name, 256) || !number(row.revision, Number.MAX_SAFE_INTEGER - 1) ||
      !PUBLISHER_STATES.includes(row.state as typeof PUBLISHER_STATES[number]) || !sha(row.bindingSha256) || typeof row.approvalValid !== "boolean" ||
      !PUBLISHER_EVIDENCE.includes(row.evidence as typeof PUBLISHER_EVIDENCE[number]) || !stamp(row.updatedAt)) return null;
  if ((row.state === "revoked" && actions.length) || (!row.approvalValid && actions.some(action => action !== "revoke"))) return null;
  return { id: row.id, sourceId: row.sourceId, name: row.name, revision: row.revision, state: row.state as DiscoveryPublisher["state"], bindingSha256: row.bindingSha256,
    approvalValid: row.approvalValid, evidence: row.evidence as DiscoveryPublisher["evidence"], updatedAt: row.updatedAt, actions };
}
export function projectDiscoveryCandidate(value: unknown): DiscoveryCandidate | null {
  const row = discoveryObject(value), observation = row && projectRegistryObservation(row.observation), reasons = row && codes(row.reasonCodes), scoreReasons = row && strings(row.scoreReasons, 64, 512);
  if (!row || !observation || !reasons || !scoreReasons || !id(row.id) || !id(row.sourceId) || !id(row.snapshotId) || !sha(row.candidateSha256) ||
      !SOURCE_STATES.includes(row.sourceState as typeof SOURCE_STATES[number]) || !id(row.publisherId) || !PUBLISHER_STATES.includes(row.publisherState as typeof PUBLISHER_STATES[number]) ||
      typeof row.sourceApprovalValid !== "boolean" || typeof row.publisherApprovalValid !== "boolean" || typeof row.stale !== "boolean" || typeof row.canReview !== "boolean" || row.executable !== false ||
      !(row.score === null || Number.isSafeInteger(row.score))) return null;
  if (row.canReview && (row.sourceState !== "active" || !row.sourceApprovalValid || row.publisherState !== "approved" || !row.publisherApprovalValid || row.stale)) return null;
  return { id: row.id, sourceId: row.sourceId, snapshotId: row.snapshotId, candidateSha256: row.candidateSha256, observation, sourceState: row.sourceState as DiscoveryCandidate["sourceState"],
    publisherId: row.publisherId, publisherState: row.publisherState as DiscoveryCandidate["publisherState"], sourceApprovalValid: row.sourceApprovalValid, publisherApprovalValid: row.publisherApprovalValid,
    stale: row.stale, canReview: row.canReview, reasonCodes: reasons, executable: false, score: row.score as number | null, scoreReasons };
}
export function projectPrerequisiteMeasurement(value: unknown, identity: DiscoveryScope, checkId: string, agentId: string, candidateSha256: string): PrerequisiteMeasurement | null {
  const row = discoveryObject(value), bindings = row && discoveryObject(row.bindings);
  if (!row || !json(row, 131_072) || row.schema !== "potbot-skill-eligibility/v1" || row.checkId !== checkId || row.agentId !== agentId || row.candidateSha256 !== candidateSha256 ||
      !sha(row.sourceBindingSha256) || !sha(row.requirementsSha256) || !stamp(row.checkedAt) || !stamp(row.expiresAt) || typeof row.eligible !== "boolean" ||
      !bindings || !keys(bindings, ["workspaceId", "principalId", "agentHomeRevision", ...MEASUREMENT_HASHES]) || bindings.workspaceId !== identity.workspaceId || bindings.principalId !== identity.principalId ||
      !number(bindings.agentHomeRevision, Number.MAX_SAFE_INTEGER, 1) || !MEASUREMENT_HASHES.every(key => sha(bindings[key])) || !Array.isArray(row.checks) || row.checks.length < 1 || row.checks.length > 129) return null;
  const lifetime = Date.parse(row.expiresAt) - Date.parse(row.checkedAt); if (!(lifetime > 0 && lifetime <= 120_000)) return null;
  const checks: PrerequisiteMeasurement["checks"] = [], seen = new Set<string>();
  for (const raw of row.checks) {
    const result = discoveryObject(raw); if (!result || !["runtime", ...REQUIREMENT_KINDS].includes(String(result.kind)) || !text(result.name, 512, false, true) ||
      !["passed", "blocked", "unsupported"].includes(String(result.status)) || !MEASUREMENT_REASONS.includes(String(result.reasonCode))) return null;
    const reason = String(result.reasonCode), expectedStatus = PASSED_REASONS.includes(reason) ? "passed" : UNSUPPORTED_REASONS.includes(reason) ? "unsupported" : "blocked";
    const expectedKind = [["sandbox_", "runtime"], ["any_binary_", "any_binary"], ["binary_", "binary"], ["shared_tool_", "binary"], ["environment_", "environment"], ["operating_system_", "operating_system"], ["configuration_", "configuration"], ["capability_", "tool"], ["compatibility_", "compatibility"]].find(([prefix]) => reason.startsWith(prefix))?.[1];
    const groupName = { any_binary: "any declared binary", operating_system: "any declared operating system", runtime: "selected agent sandbox" }[String(result.kind)];
    const identity = JSON.stringify([result.kind, result.name]);
    if (result.status !== expectedStatus || result.kind !== expectedKind || (groupName && result.name !== groupName) || seen.has(identity)) return null;
    seen.add(identity);
    checks.push({ kind: result.kind as PrerequisiteMeasurement["checks"][number]["kind"], name: result.name, status: result.status as PrerequisiteMeasurement["checks"][number]["status"], reasonCode: result.reasonCode as string });
  }
  if (checks[0].kind !== "runtime" || checks[0].name !== "selected agent sandbox" || checks[0].status !== "passed" || checks[0].reasonCode !== "sandbox_probe_completed" ||
      row.eligible !== checks.every(check => check.status === "passed")) return null;
  return { schema: "potbot-skill-eligibility/v1", checkId, agentId, candidateSha256, sourceBindingSha256: row.sourceBindingSha256, requirementsSha256: row.requirementsSha256,
    checkedAt: row.checkedAt, expiresAt: row.expiresAt, eligible: row.eligible, checks, bindings: { workspaceId: identity.workspaceId, principalId: identity.principalId, agentHomeRevision: bindings.agentHomeRevision,
      runtimeSha256: bindings.runtimeSha256 as string, imageSha256: bindings.imageSha256 as string, qualificationSha256: bindings.qualificationSha256 as string, probeSha256: bindings.probeSha256 as string,
      interpreterSha256: bindings.interpreterSha256 as string, observationSha256: bindings.observationSha256 as string, isolationSha256: bindings.isolationSha256 as string } };
}
export function projectDiscoveryCheck(value: unknown, identity: DiscoveryScope): DiscoveryCheck | null {
  const row = discoveryObject(value), candidate = row && projectDiscoveryCandidate(row.candidate), required = row && requirements(row.requirements, true), reasons = row && codes(row.reasonCodes), actions = row && choices(row.actions, CHECK_ACTIONS);
  if (!row || !candidate || !required || !reasons || !actions || !id(row.id) || row.candidateId !== candidate.id || row.sourceId !== candidate.sourceId || row.snapshotId !== candidate.snapshotId ||
      !sha(row.requestedCandidateSha256) || row.candidateSha256 !== candidate.candidateSha256 || !number(row.revision, Number.MAX_SAFE_INTEGER - 1, 1) || !sha(row.bindingSha256) || !id(row.agentId) || !CHECK_STATES.includes(row.status as typeof CHECK_STATES[number]) ||
      !(row.packageId === null || id(row.packageId)) || !(row.recipeId === null || id(row.recipeId)) || typeof row.measurementCurrent !== "boolean" || !stamp(row.createdAt) || !stamp(row.updatedAt)) return null;
  const measurement = row.measurement === null ? null : projectPrerequisiteMeasurement(row.measurement, identity, row.id, row.agentId, candidate.candidateSha256);
  if ((row.measurement !== null && !measurement) || (row.measurementCurrent && !measurement)) return null;
  let target: DiscoveryCheck["target"] = null;
  if (row.target !== null) { const raw = discoveryObject(row.target); if (!raw || !keys(raw, ["kind", "id"]) || !["skill_package", "tool_acquisition"].includes(String(raw.kind)) || !id(raw.id)) return null;
    target = { kind: raw.kind as NonNullable<DiscoveryCheck["target"]>["kind"], id: raw.id }; }
  if (row.status === "handed_off" && !target) return null;
  if (target && (row.status !== "handed_off" || (target.kind === "skill_package" && target.id !== row.packageId))) return null;
  return { id: row.id, candidateId: candidate.id, sourceId: candidate.sourceId, snapshotId: candidate.snapshotId, requestedCandidateSha256: row.requestedCandidateSha256, candidateSha256: candidate.candidateSha256,
    revision: row.revision, bindingSha256: row.bindingSha256, agentId: row.agentId, status: row.status as DiscoveryCheck["status"], candidate, requirements: required, packageId: row.packageId, recipeId: row.recipeId,
    measurement, measurementCurrent: row.measurementCurrent, target, reasonCodes: reasons, actions, createdAt: row.createdAt, updatedAt: row.updatedAt };
}
function page<T extends { id: string }>(value: unknown, workspace: string, principal: string | undefined, project: (value: unknown, identity: DiscoveryScope) => T | null, ranked = false): DiscoveryPage<T> | null {
  const row = discoveryObject(value), identity = scope(value, workspace, principal);
  if (!row || !identity || !Array.isArray(row.items) || row.items.length > 50 || !(row.nextCursor === null || (ranked ? typeof row.nextCursor === "string" && DISCOVERY_SEARCH_CURSOR.test(row.nextCursor) : id(row.nextCursor)))) return null;
  const items: T[] = [];
  for (const raw of row.items) { const item = project(raw, identity); if (!item || items.some(prior => prior.id === item.id)) return null; items.push(item); }
  if (row.nextCursor !== null && (!items.length || (!ranked && row.nextCursor !== items.at(-1)!.id))) return null;
  return { ...identity, items, nextCursor: row.nextCursor as string | null };
}
export function projectDiscoverySources(value: unknown, workspace: string, principal?: string): DiscoveryPage<DiscoverySource> | null { return page(value, workspace, principal, projectDiscoverySource); }
export function projectDiscoveryPublishers(value: unknown, workspace: string, principal?: string, sourceId?: string): DiscoveryPage<DiscoveryPublisher> | null { return page(value, workspace, principal, raw => projectDiscoveryPublisher(raw, sourceId)); }
export function projectDiscoveryCandidates(value: unknown, workspace: string, principal?: string): DiscoveryPage<DiscoveryCandidate> | null { return page(value, workspace, principal, projectDiscoveryCandidate, true); }
export function projectDiscoveryChecks(value: unknown, workspace: string, principal?: string): DiscoveryPage<DiscoveryCheck> | null { return page(value, workspace, principal, projectDiscoveryCheck); }
export function projectDiscoverySourceEnvelope(value: unknown, workspace: string, expectedId?: string, principal?: string): SourceEnvelope | null {
  const row = discoveryObject(value), identity = scope(value, workspace, principal), source = row && projectDiscoverySource(row.source);
  return identity && source && (!expectedId || source.id === expectedId) ? { ...identity, source } : null;
}
export function projectDiscoveryPublisherEnvelope(value: unknown, workspace: string, expectedId?: string, principal?: string): PublisherEnvelope | null {
  const row = discoveryObject(value), identity = scope(value, workspace, principal), publisher = row && projectDiscoveryPublisher(row.publisher);
  return identity && publisher && (!expectedId || publisher.id === expectedId) ? { ...identity, publisher } : null;
}
export function projectDiscoveryCandidateEnvelope(value: unknown, workspace: string, expectedId?: string, principal?: string): CandidateEnvelope | null {
  const row = discoveryObject(value), identity = scope(value, workspace, principal), candidate = row && projectDiscoveryCandidate(row.candidate);
  return identity && candidate && (!expectedId || candidate.id === expectedId) ? { ...identity, candidate } : null;
}
export function projectDiscoveryCheckEnvelope(value: unknown, workspace: string, expectedId?: string, principal?: string): CheckEnvelope | null {
  const row = discoveryObject(value), identity = scope(value, workspace, principal), check = row && identity && projectDiscoveryCheck(row.check, identity);
  return identity && check && (!expectedId || check.id === expectedId) ? { ...identity, check } : null;
}
export function validateDiscoveryMutation(route: string[], value: unknown): Record<string, unknown> | null {
  const row = discoveryObject(value); if (!row || row.confirm !== true || route.length !== 3 || !id(route[1])) return null;
  if (route[0] === "candidates" && route[2] === "checks") return keys(row, ["snapshotId", "candidateSha256", "agentId", "confirm"]) && id(row.snapshotId) && sha(row.candidateSha256) && id(row.agentId) ?
    { snapshotId: row.snapshotId, candidateSha256: row.candidateSha256, agentId: row.agentId, confirm: true } : null;
  if (!((route[0] === "sources" && SOURCE_ACTIONS.includes(route[2] as typeof SOURCE_ACTIONS[number])) || (route[0] === "publishers" && PUBLISHER_ACTIONS.includes(route[2] as typeof PUBLISHER_ACTIONS[number])) ||
      (route[0] === "checks" && CHECK_ACTIONS.includes(route[2] as typeof CHECK_ACTIONS[number])))) return null;
  return keys(row, ["revision", "digest", "confirm"]) && number(row.revision, Number.MAX_SAFE_INTEGER - 1) && sha(row.digest) ? { revision: row.revision, digest: row.digest, confirm: true } : null;
}
export function discoveryControlBody(record: DiscoverySource | DiscoveryPublisher | DiscoveryCheck, action: string): Record<string, unknown> | null {
  if (!(record.actions as string[]).includes(action)) return null;
  return { revision: record.revision, digest: record.bindingSha256, confirm: true };
}
export function projectDiscoveryMutation(value: unknown, workspace: string, route: string[], body: unknown, principal?: string): SourceEnvelope | PublisherEnvelope | CheckEnvelope | null {
  const request = validateDiscoveryMutation(route, body); if (!request) return null;
  if (route[0] === "sources") { const result = projectDiscoverySourceEnvelope(value, workspace, route[1], principal); const state = { enable: "active", disable: "disabled", revoke: "revoked" }[route[2]];
    return result && result.source.revision === Number(request.revision) + 1 && result.source.state === state ? result : null; }
  if (route[0] === "publishers") { const result = projectDiscoveryPublisherEnvelope(value, workspace, route[1], principal); const state = { approve: "approved", block: "blocked", revoke: "revoked" }[route[2]];
    return result && result.publisher.revision === Number(request.revision) + 1 && result.publisher.state === state ? result : null; }
  const result = projectDiscoveryCheckEnvelope(value, workspace, route[0] === "checks" ? route[1] : undefined, principal); if (!result) return null;
  if (route[0] === "candidates") return result.check.candidateId === route[1] && result.check.snapshotId === request.snapshotId && result.check.requestedCandidateSha256 === request.candidateSha256 &&
    result.check.agentId === request.agentId && result.check.revision === 1 && result.check.status === "awaiting_measurement" ? result : null;
  return result.check.revision === Number(request.revision) + 1 && result.check.status === (route[2] === "handoff" ? "handed_off" : "cancelled") ? result : null;
}
