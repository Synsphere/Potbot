/** Public portable-skill data contracts. No package text grants execution authority. */
import { UUID, SHA256 } from "./operation-workbench.ts";
export { UUID, SHA256 };
export const SKILL_SCHEMA = "potbot-skill-acquisition/v1" as const;
export const MAX_ARCHIVE_BYTES = 4 * 1024 * 1024, MAX_FILE_BYTES = 1024 * 1024, RESOURCE_CHUNK = 65_536;
const COMMIT = /^[a-f0-9]{40}$/;
const NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
export const ACTIONS = ["activate", "disable", "rollback", "revoke"] as const;
export type SkillAction = typeof ACTIONS[number];
export type SkillScope = { schema: typeof SKILL_SCHEMA; workspaceId: string; principalId: string };
export type SkillSource = { kind: string; archive_sha256?: string; path?: string; verification?: string; url?: string; commit?: string; requested_path?: string; releaseSha256?: string;
  adapter?: string; identity?: string; version?: string; candidate_sha256?: string; registry_adapter?: string; registry_identity?: string; registry_version?: string; registry_candidate_sha256?: string; registry_source_sha256?: string };
export type SkillFile = { path: string; sha256: string; size: number; media_type: string; executable_hint: boolean };
export type SkillManifest = { schema: "potbot-skill-package/v1"; name: string; description: string; content_sha256: string;
  files: SkillFile[]; metadata: Record<string, unknown>; requirements: { kind: string; name: string; status: "unverified" }[];
  compatibility: { status: "portable" | "review_required"; unsupported: string[] };
  findings: { code: string; severity: "notice" | "warning"; message: string }[] };
export type SkillPackage = { id: string; name: string; description: string; releaseSha256: string; contentSha256: string;
  sourceSha256: string; source: SkillSource; manifest: SkillManifest; createdAt: string; files: number; bytes: number; ownerPrincipalId: string };
export type AcquiredSkill = { id: string; name: string; namespace: string; origin: "acquired"; ownerPrincipalId: string; owned: boolean;
  contextObjectId: string; contextName: string; revision: number; state: "active" | "revoked"; enabled: boolean; approvalValid: boolean;
  releaseSha256: string; contentSha256: string; packageId: string; sourceSha256: string; source: SkillSource; manifest: SkillManifest;
  createdAt: string; updatedAt: string };
export type SkillPlan = { sha256: string; packageId: string; skillId: string; name: string; action: "install" | "update";
  expectedRevision: number; previousReleaseSha256: string | null; releaseSha256: string; sourceSha256: string;
  willDisable: true; executable: false; added: string[]; changed: string[]; removed: string[]; sourceChanged: boolean; package: SkillPackage };
export type SkillRelease = { releaseSha256: string; contentSha256: string; packageId: string; sourceSha256: string;
  revision: number; createdAt: string; current: boolean; manifest: SkillManifest; source: SkillSource };
export type PackageEnvelope = SkillScope & { package: SkillPackage; content?: string; contentTruncated?: boolean };
export type SkillEnvelope = SkillScope & { skill: AcquiredSkill };
export type PlanEnvelope = SkillScope & { plan: SkillPlan };
export type SkillHistoryEvent = { revision: number; action: "install" | "update" | SkillAction; releaseSha256: string; packageId: string; sourceSha256: string;
  source: SkillSource; state: "active" | "revoked"; enabled: boolean; createdAt: string };
export type HistoryEnvelope = SkillEnvelope & { releases: SkillRelease[]; events: SkillHistoryEvent[]; nextCursor: string | null };
export type SkillList = SkillScope & { items: AcquiredSkill[]; nextCursor: string | null };
export type PackageList = SkillScope & { items: SkillPackage[]; nextCursor: string | null };
export type ResourceEnvelope = SkillScope & { resource: { path: string; sha256: string; size: number; mediaType: string };
  offset: number; dataBase64: string; nextOffset: number | null };

function object(value: unknown): Record<string, unknown> | null { return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null; }
function integer(value: unknown, max = Number.MAX_SAFE_INTEGER): value is number { return Number.isSafeInteger(value) && Number(value) >= 0 && Number(value) <= max; }
function text(value: unknown, max: number): value is string { return typeof value === "string" && Array.from(value).length <= max && !value.includes("\u0000") && new TextDecoder().decode(new TextEncoder().encode(value)) === value; }
function stamp(value: unknown): value is string { return text(value, 64) && /^\d{4}-\d\d-\d\dT/.test(value) && /(?:Z|\+00:00)$/.test(value) && Number.isFinite(Date.parse(value)); }
function id(value: unknown, pattern = UUID): value is string { return typeof value === "string" && pattern.test(value); }
function digest(value: unknown): value is string { return id(value, SHA256); }
function slug(value: unknown): value is string { return text(value, 64) && value.length > 0 && NAME.test(value); }
function media(value: unknown): value is string { return text(value, 127) && /^[a-z0-9.+-]+\/[a-z0-9.+-]+(?:;[ -~]*)?$/i.test(value); }
function publicScope(value: unknown, workspace: string, principal?: string): SkillScope | null {
  const row = object(value);
  return row && row.schema === SKILL_SCHEMA && row.workspaceId === workspace && id(workspace) && id(row.principalId) &&
    (!principal || row.principalId === principal) ? { schema: SKILL_SCHEMA, workspaceId: workspace, principalId: row.principalId } : null;
}
export function resourcePath(value: unknown, empty = false): value is string {
  if (empty && value === "") return true;
  if (!text(value, 512) || !value || new TextEncoder().encode(value).length > 512 || value !== value.normalize("NFC") || /[\x00-\x1f\x7f\\:]/.test(value)) return false;
  const parts = value.split("/");
  return parts.length <= 12 && parts.every(part => part !== "" && part !== "." && part !== ".." && part.toLowerCase() !== ".git" &&
    new TextEncoder().encode(part).length <= 128 && !/[. ]$/.test(part) && !/^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$/i.test(part));
}
export function githubRepository(value: unknown): value is string {
  return typeof value === "string" && /^https:\/\/github\.com\/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(value) && !value.endsWith(".git");
}
function metadata(value: unknown, depth = 0): boolean {
  if (depth > 16) return false;
  if (value === null || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value) && (!Number.isInteger(value) || Number.isSafeInteger(value));
  if (typeof value === "string") return text(value, 32768);
  if (Array.isArray(value)) return value.length <= 256 && value.every(item => metadata(item, depth + 1));
  const row = object(value); return row !== null && Object.keys(row).length <= 256 && Object.entries(row).every(([key, item]) => text(key, 128) && key.length > 0 && metadata(item, depth + 1));
}
export function projectSource(value: unknown): SkillSource | null {
  const row = object(value); if (!row || !["zip", "github", "directory", "local", "registry"].includes(String(row.kind))) return null;
  const source: SkillSource = { kind: row.kind as string };
  if (row.kind === "zip" || row.kind === "github") {
    if (!digest(row.archive_sha256) || !resourcePath(row.path, true) || row.verification !== "content-integrity-only") return null;
    Object.assign(source, { archive_sha256: row.archive_sha256, path: row.path, verification: row.verification });
  }
  if (row.kind === "github") {
    if (!githubRepository(row.url) || !id(row.commit, COMMIT) || !resourcePath(row.requested_path, true)) return null;
    Object.assign(source, { url: row.url, commit: row.commit, requested_path: row.requested_path });
  }
  const registryFields = ["registry_adapter", "registry_identity", "registry_version", "registry_candidate_sha256", "registry_source_sha256"] as const;
  const registryText = (value: unknown, max: number): value is string => text(value, max) && value.length > 0 && !/[\x00-\x1f\x7f]/.test(value);
  if (row.kind === "registry" || registryFields.some(key => row[key] !== undefined)) {
    if (!["curated", "clawhub", "skills_sh"].includes(String(row.registry_adapter)) || !registryText(row.registry_identity, 320) || !registryText(row.registry_version, 128) ||
        ["latest", "main", "master", "head", "*"].includes(row.registry_version.toLowerCase()) || !digest(row.registry_candidate_sha256) || !digest(row.registry_source_sha256)) return null;
    for (const key of registryFields) source[key] = row[key] as string;
  }
  if (row.kind === "registry") {
    if (!["clawhub", "skills_sh"].includes(String(row.adapter)) || row.adapter !== source.registry_adapter || row.identity !== source.registry_identity || row.verification !== "content-integrity-only") return null;
    Object.assign(source, { adapter: row.adapter, identity: row.identity, verification: row.verification });
    if (row.adapter === "clawhub") {
      if (row.version !== source.registry_version || row.candidate_sha256 !== source.registry_candidate_sha256 || !digest(row.archive_sha256) || !resourcePath(row.path, true)) return null;
      Object.assign(source, { version: row.version, candidate_sha256: row.candidate_sha256, archive_sha256: row.archive_sha256, path: row.path });
    } else if (!/^snapshot:[a-f0-9]{64}$/.test(source.registry_version!) || row.archive_sha256 !== undefined || row.path !== undefined || row.version !== undefined || row.candidate_sha256 !== undefined) return null;
  }
  if (row.releaseSha256 !== undefined) { if (!digest(row.releaseSha256)) return null; source.releaseSha256 = row.releaseSha256; }
  return source;
}
export function projectManifest(value: unknown): SkillManifest | null {
  const row = object(value), compatibility = row && object(row.compatibility), rawMetadata = row && object(row.metadata);
  if (!row || row.schema !== "potbot-skill-package/v1" || !slug(row.name) || !text(row.description, 1024) || !row.description.trim() ||
      !digest(row.content_sha256) || !Array.isArray(row.files) || row.files.length < 1 || row.files.length > 128 ||
      !rawMetadata || !metadata(rawMetadata) || !Array.isArray(row.requirements) || row.requirements.length > 1024 ||
      !compatibility || !["portable", "review_required"].includes(String(compatibility.status)) || !Array.isArray(compatibility.unsupported) ||
      compatibility.unsupported.length > 1024 || !compatibility.unsupported.every(item => text(item, 1024)) ||
      !Array.isArray(row.findings) || row.findings.length > 32) return null;
  const files: SkillFile[] = [], paths = new Set<string>(); let bytes = 0;
  for (const item of row.files) {
    const file = object(item);
    if (!file || !resourcePath(file.path) || paths.has(file.path.toLowerCase()) || !digest(file.sha256) || !integer(file.size, MAX_FILE_BYTES) ||
        !media(file.media_type) || typeof file.executable_hint !== "boolean") return null;
    paths.add(file.path.toLowerCase()); bytes += file.size;
    files.push({ path: file.path, sha256: file.sha256, size: file.size, media_type: file.media_type, executable_hint: file.executable_hint });
  }
  const skillFile = files.find(file => file.path === "SKILL.md");
  if (bytes > 8 * 1024 * 1024 || !skillFile || skillFile.sha256 !== row.content_sha256) return null;
  const requirements: SkillManifest["requirements"] = [];
  for (const item of row.requirements) {
    const requirement = object(item);
    if (!requirement || !["binary", "any_binary", "environment", "configuration", "operating_system", "tool", "compatibility"].includes(String(requirement.kind)) ||
        !text(requirement.name, 512) || !requirement.name || requirement.status !== "unverified") return null;
    requirements.push({ kind: requirement.kind as string, name: requirement.name, status: "unverified" });
  }
  const findings: SkillManifest["findings"] = [];
  for (const item of row.findings) {
    const finding = object(item);
    if (!finding || !id(finding.code, /^[a-z][a-z0-9_]{0,63}$/) || !["notice", "warning"].includes(String(finding.severity)) || !text(finding.message, 4096)) return null;
    findings.push({ code: finding.code, severity: finding.severity as "notice" | "warning", message: finding.message });
  }
  const result: SkillManifest = { schema: "potbot-skill-package/v1", name: row.name, description: row.description, content_sha256: row.content_sha256,
    files, metadata: rawMetadata, requirements, compatibility: { status: compatibility.status as "portable" | "review_required", unsupported: compatibility.unsupported as string[] }, findings };
  return new TextEncoder().encode(JSON.stringify(result)).length <= 131_072 ? result : null;
}
function packageView(value: unknown, scope: SkillScope): SkillPackage | null {
  const row = object(value), manifest = row && projectManifest(row.manifest), source = row && projectSource(row.source);
  if (!row || !manifest || !source || !id(row.id) || row.ownerPrincipalId !== scope.principalId || !slug(row.name) || row.name !== manifest.name ||
      row.description !== manifest.description || !digest(row.releaseSha256) || row.contentSha256 !== manifest.content_sha256 || !digest(row.sourceSha256) ||
      !stamp(row.createdAt) || row.files !== manifest.files.length || row.bytes !== manifest.files.reduce((sum, file) => sum + file.size, 0)) return null;
  return { id: row.id, name: row.name, description: manifest.description, releaseSha256: row.releaseSha256, contentSha256: manifest.content_sha256,
    sourceSha256: row.sourceSha256, source, manifest, createdAt: row.createdAt, files: manifest.files.length, bytes: row.bytes as number, ownerPrincipalId: scope.principalId };
}
function skillView(value: unknown, scope: SkillScope): AcquiredSkill | null {
  const row = object(value), manifest = row && projectManifest(row.manifest), source = row && projectSource(row.source);
  if (!row || !manifest || !source || !id(row.id) || row.origin !== "acquired" || !id(row.ownerPrincipalId) || row.owned !== (row.ownerPrincipalId === scope.principalId) ||
      row.namespace !== "imported-" + row.ownerPrincipalId.replaceAll("-", "") || row.contextName !== row.namespace + "." + row.name || !id(row.contextObjectId) ||
      row.name !== manifest.name || !integer(row.revision) || row.revision < 1 || !["active", "revoked"].includes(String(row.state)) ||
      typeof row.enabled !== "boolean" || (row.state === "revoked" && row.enabled) || typeof row.approvalValid !== "boolean" ||
      (row.owned === false && (row.state !== "active" || !row.enabled || !row.approvalValid)) ||
      !digest(row.releaseSha256) || row.contentSha256 !== manifest.content_sha256 || !id(row.packageId) || !digest(row.sourceSha256) || !stamp(row.createdAt) || !stamp(row.updatedAt)) return null;
  return { id: row.id, name: manifest.name, namespace: row.namespace as string, origin: "acquired", ownerPrincipalId: row.ownerPrincipalId, owned: row.owned as boolean,
    contextObjectId: row.contextObjectId, contextName: row.contextName as string, revision: row.revision, state: row.state as "active" | "revoked", enabled: row.enabled,
    approvalValid: row.approvalValid, releaseSha256: row.releaseSha256, contentSha256: manifest.content_sha256, packageId: row.packageId,
    sourceSha256: row.sourceSha256, source, manifest, createdAt: row.createdAt, updatedAt: row.updatedAt };
}
export function projectPackage(value: unknown, workspace: string, packageId?: string, principal?: string): PackageEnvelope | null {
  const row = object(value), scope = publicScope(value, workspace, principal), item = row && scope && packageView(row.package, scope);
  if (!row || !scope || !item || (packageId && item.id !== packageId)) return null;
  const result: PackageEnvelope = { ...scope, package: item };
  if (row.content !== undefined || row.contentTruncated !== undefined) {
    if (!text(row.content, 65_536) || new TextEncoder().encode(row.content).length > 65_536 || typeof row.contentTruncated !== "boolean") return null;
    result.content = row.content; result.contentTruncated = row.contentTruncated;
  }
  return result;
}
export function projectSkill(value: unknown, workspace: string, skillId?: string, principal?: string): SkillEnvelope | null {
  const row = object(value), scope = publicScope(value, workspace, principal), item = row && scope && skillView(row.skill, scope);
  return scope && item && (!skillId || item.id === skillId) ? { ...scope, skill: item } : null;
}
export function projectSkillList(value: unknown, workspace: string, principal?: string): SkillList | null {
  const row = object(value), scope = publicScope(value, workspace, principal);
  if (!row || !scope || !Array.isArray(row.items) || row.items.length > 50 || !(row.nextCursor === null || id(row.nextCursor))) return null;
  const items: AcquiredSkill[] = []; for (const raw of row.items) { const item = skillView(raw, scope); if (!item || items.some(prior => prior.id === item.id)) return null; items.push(item); }
  return { ...scope, items, nextCursor: row.nextCursor };
}
export function projectPackageList(value: unknown, workspace: string, principal?: string): PackageList | null {
  const row = object(value), scope = publicScope(value, workspace, principal);
  if (!row || !scope || !Array.isArray(row.items) || row.items.length > 50 || !(row.nextCursor === null || id(row.nextCursor))) return null;
  const items: SkillPackage[] = []; for (const raw of row.items) { const item = packageView(raw, scope); if (!item || items.some(prior => prior.id === item.id)) return null; items.push(item); }
  return { ...scope, items, nextCursor: row.nextCursor };
}
export function projectPlan(value: unknown, workspace: string, packageId: string, skillId?: string, principal?: string): PlanEnvelope | null {
  const row = object(value), scope = publicScope(value, workspace, principal), plan = row && object(row.plan), item = plan && scope && packageView(plan.package, scope);
  if (!scope || !plan || !item || !digest(plan.sha256) || plan.packageId !== packageId || item.id !== packageId || !id(plan.skillId) ||
      (skillId && plan.skillId !== skillId) || plan.name !== item.name || !["install", "update"].includes(String(plan.action)) ||
      !integer(plan.expectedRevision) || (plan.action === "install" ? plan.expectedRevision !== 0 || plan.previousReleaseSha256 !== null : plan.expectedRevision < 1 || !digest(plan.previousReleaseSha256)) ||
      plan.releaseSha256 !== item.releaseSha256 || plan.sourceSha256 !== item.sourceSha256 || plan.willDisable !== true || plan.executable !== false || typeof plan.sourceChanged !== "boolean") return null;
  const lists: Record<"added" | "changed" | "removed", string[]> = { added: [], changed: [], removed: [] }, paths = new Set<string>();
  for (const key of ["added", "changed", "removed"] as const) {
    if (!Array.isArray(plan[key]) || plan[key].length > 128) return null;
    for (const path of plan[key]) { if (!resourcePath(path) || paths.has(path)) return null; paths.add(path); lists[key].push(path); }
  }
  return { ...scope, plan: { sha256: plan.sha256, packageId, skillId: plan.skillId, name: item.name, action: plan.action as "install" | "update",
    expectedRevision: plan.expectedRevision, previousReleaseSha256: plan.previousReleaseSha256 as string | null, releaseSha256: item.releaseSha256,
    sourceSha256: item.sourceSha256, willDisable: true, executable: false, ...lists, sourceChanged: plan.sourceChanged, package: item } };
}
export function projectHistory(value: unknown, workspace: string, skillId: string, principal?: string, after = 0): HistoryEnvelope | null {
  const row = object(value), selected = projectSkill(value, workspace, skillId, principal);
  if (!row || !selected || !selected.skill.owned || !Array.isArray(row.releases) || row.releases.length > 50 || !Array.isArray(row.events) || row.events.length > 50 ||
      !(row.nextCursor === null || (typeof row.nextCursor === "string" && /^[1-9]\d{0,15}$/.test(row.nextCursor) && integer(Number(row.nextCursor))))) return null;
  const releases: SkillRelease[] = [], seen = new Set<string>();
  for (const raw of row.releases) {
    const release = object(raw), manifest = release && projectManifest(release.manifest), source = release && projectSource(release.source);
    if (!release || !manifest || !source || !digest(release.releaseSha256) || seen.has(release.releaseSha256) || release.contentSha256 !== manifest.content_sha256 ||
        manifest.name !== selected.skill.name || !id(release.packageId) || !digest(release.sourceSha256) || !integer(release.revision) || release.revision < 1 ||
        !stamp(release.createdAt) || typeof release.current !== "boolean" || release.current !== (release.releaseSha256 === selected.skill.releaseSha256)) return null;
    seen.add(release.releaseSha256); releases.push({ releaseSha256: release.releaseSha256, contentSha256: manifest.content_sha256, packageId: release.packageId,
      sourceSha256: release.sourceSha256, revision: release.revision, createdAt: release.createdAt, current: release.current, manifest, source });
  }
  const events: SkillHistoryEvent[] = []; let revision = after || selected.skill.revision + 1;
  for (const raw of row.events) {
    const event = object(raw), source = event && projectSource(event.source);
    if (!event || !source || !integer(event.revision) || event.revision < 1 || event.revision >= revision || event.revision > selected.skill.revision ||
        !["install", "update", ...ACTIONS].includes(event.action as SkillHistoryEvent["action"]) || !digest(event.releaseSha256) ||
        !id(event.packageId) || !digest(event.sourceSha256) || !["active", "revoked"].includes(String(event.state)) || typeof event.enabled !== "boolean" ||
        (event.state === "revoked" && event.enabled) || !stamp(event.createdAt)) return null;
    events.push({ revision: event.revision, action: event.action as SkillHistoryEvent["action"], releaseSha256: event.releaseSha256, packageId: event.packageId,
      sourceSha256: event.sourceSha256, source, state: event.state as "active" | "revoked", enabled: event.enabled, createdAt: event.createdAt });
    revision = event.revision;
  }
  if (row.nextCursor !== null && (!events.length || Number(row.nextCursor) !== revision)) return null;
  return { ...selected, releases, events, nextCursor: row.nextCursor as string | null };
}
export function decodeBase64(value: string, maximum: number): Uint8Array<ArrayBuffer> {
  if (value.length > Math.ceil(maximum / 3) * 4 || value.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) throw new Error("Invalid bounded base64");
  const decoded = atob(value); if (btoa(decoded) !== value || decoded.length > maximum) throw new Error("Invalid canonical base64");
  return Uint8Array.from(decoded, char => char.charCodeAt(0));
}
export function projectResource(value: unknown, workspace: string, path: string, offset: number, length: number, principal?: string): ResourceEnvelope | null {
  const row = object(value), scope = publicScope(value, workspace, principal), resource = row && object(row.resource);
  if (!row || !scope || !resource || !resourcePath(path) || resource.path !== path || !digest(resource.sha256) || !integer(resource.size, MAX_FILE_BYTES) || !media(resource.mediaType) ||
      row.offset !== offset || !integer(offset) || !integer(length, RESOURCE_CHUNK) || length < 1 || typeof row.dataBase64 !== "string" || !(row.nextOffset === null || integer(row.nextOffset))) return null;
  let bytes: Uint8Array; try { bytes = decodeBase64(row.dataBase64, length); } catch { return null; }
  const next = offset + bytes.length;
  if (next > resource.size || (row.nextOffset === null ? next !== resource.size : row.nextOffset !== next || !bytes.length || next >= resource.size)) return null;
  return { ...scope, resource: { path, sha256: resource.sha256, size: resource.size, mediaType: resource.mediaType }, offset, dataBase64: row.dataBase64, nextOffset: row.nextOffset };
}

export function validateSkillMutation(route: string[], value: unknown): Record<string, unknown> | null {
  const row = object(value); if (!row) return null;
  const keys = Object.keys(row).sort().join(",");
  if (route.join("/") === "imports/zip") {
    if (keys !== "archiveBase64,subdirectory" || typeof row.archiveBase64 !== "string" || !resourcePath(row.subdirectory, true)) return null;
    try { if (!decodeBase64(row.archiveBase64, MAX_ARCHIVE_BYTES).length) return null; } catch { return null; }
    return { archiveBase64: row.archiveBase64, subdirectory: row.subdirectory };
  }
  if (route.join("/") === "imports/github") return keys === "commit,path,repository" && githubRepository(row.repository) && id(row.commit, COMMIT) && resourcePath(row.path, true) ?
    { repository: row.repository, commit: row.commit, path: row.path } : null;
  if (route.length === 3 && route[0] === "packages" && id(route[1]) && route[2] === "install") return keys === "confirm,expectedRevision,planSha256,skillId" &&
    row.confirm === true && integer(row.expectedRevision) && digest(row.planSha256) && (row.skillId === null || id(row.skillId)) ?
    { planSha256: row.planSha256, expectedRevision: row.expectedRevision, skillId: row.skillId, confirm: true } : null;
  if (route.length === 2 && id(route[0]) && ACTIONS.includes(route[1] as SkillAction)) return keys === "confirm,expectedRevision,releaseSha256" && row.confirm === true &&
    integer(row.expectedRevision) && row.expectedRevision > 0 && (route[1] === "rollback" ? digest(row.releaseSha256) : row.releaseSha256 === null) ?
    { expectedRevision: row.expectedRevision, releaseSha256: row.releaseSha256, confirm: true } : null;
  return null;
}

export function skillInstallBody(plan: SkillPlan): Record<string, unknown> {
  return { planSha256: plan.sha256, expectedRevision: plan.expectedRevision, skillId: plan.action === "install" ? null : plan.skillId, confirm: true };
}

export function skillControls(skill: AcquiredSkill): Record<SkillAction, boolean> {
  const owner = skill.owned && skill.state === "active", approved = owner && skill.approvalValid;
  return { activate: approved && !skill.enabled, disable: approved && skill.enabled, rollback: approved, revoke: owner };
}

/** A successful receipt must describe the exact source or control action submitted. */
export async function projectSkillMutation(value: unknown, workspace: string, route: string[], body: unknown, principal?: string): Promise<PackageEnvelope | SkillEnvelope | null> {
  const request = validateSkillMutation(route, body); if (!request) return null;
  if (route[0] === "imports") {
    const result = projectPackage(value, workspace, undefined, principal); if (!result || result.package.source.kind !== route[1]) return null;
    const source = result.package.source;
    if (route[1] === "github") return source.url === request.repository && source.commit === request.commit && source.requested_path === request.path ? result : null;
    const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", decodeBase64(request.archiveBase64 as string, MAX_ARCHIVE_BYTES))), byte => byte.toString(16).padStart(2, "0")).join("");
    return source.archive_sha256 === digest && (!request.subdirectory || source.path === request.subdirectory) ? result : null;
  }
  const result = projectSkill(value, workspace, route[0] === "packages" ? undefined : route[0], principal);
  if (!result || !result.skill.owned || result.skill.revision !== Number(request.expectedRevision) + 1 || !result.skill.approvalValid) return null;
  const skill = result.skill;
  if (route[0] === "packages") return skill.packageId === route[1] && (!request.skillId || skill.id === request.skillId) && skill.state === "active" && !skill.enabled ? result : null;
  const action = route[1];
  return skill.state === (action === "revoke" ? "revoked" : "active") && skill.enabled === (action === "activate") &&
    (action !== "rollback" || skill.releaseSha256 === request.releaseSha256) ? result : null;
}
