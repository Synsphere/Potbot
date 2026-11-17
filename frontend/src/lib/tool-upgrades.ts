/** Public S11 result/approval projection. Never hydrate subprocess args, paths or credentials. */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA = /^[a-f0-9]{64}$/;
const COMMIT = /^[a-f0-9]{40}$/;
const SLUG = /^[a-z][a-z0-9-]{1,62}$/;
const STATES = new Set(["awaiting_approval", "approved", "rejected", "executing", "awaiting_repin", "active", "indeterminate", "rolled_back"]);
const RELEASE_STATES = new Set(["none", "repinned", "testing", "failed", "indeterminate", "released"]);

export type ReleaseState = "none" | "repinned" | "testing" | "failed" | "indeterminate" | "released";

export type UpgradeRecord = {
  id: string; workspaceId: string; slug: string; oldCommit: string; newCommit: string;
  suiteSha256: string; evidenceSha256: string; passedTests: number;
  tests: { name: string; kind: string; elapsedMs: number; passed: true }[]; revision: number;
  state: string; updatedAt: string;
  releaseState?: ReleaseState; releaseGeneration?: number | null;
  releaseBindingSha256?: string | null; releaseEvidenceSha256?: string | null;
};
export type ToolUpgradeListing = { schema: "potbot-tool-upgrades-s11/v1"; items: UpgradeRecord[] };

function doc(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
export function upgradeRecord(value: unknown, workspaceId: string): UpgradeRecord | null {
  const data = doc(value);
  if (!data || !UUID.test(workspaceId) || data.workspaceId !== workspaceId ||
      typeof data.id !== "string" || !UUID.test(data.id) ||
      typeof data.slug !== "string" || !SLUG.test(data.slug) ||
      typeof data.oldCommit !== "string" || !COMMIT.test(data.oldCommit) ||
      typeof data.newCommit !== "string" || !COMMIT.test(data.newCommit) ||
      data.newCommit === data.oldCommit ||
      typeof data.suiteSha256 !== "string" || !SHA.test(data.suiteSha256) ||
      typeof data.evidenceSha256 !== "string" || !SHA.test(data.evidenceSha256) ||
      !Number.isSafeInteger(data.passedTests) || Number(data.passedTests) < 2 || Number(data.passedTests) > 16 ||
      !Array.isArray(data.tests) || data.tests.length !== data.passedTests ||
      data.tests.some((raw: unknown) => {
        const item = doc(raw);
        return !item || typeof item.name !== "string" || !/^[a-z][a-z0-9_.-]{1,63}$/.test(item.name) ||
          typeof item.kind !== "string" || !["smoke", "unit", "integration", "regression"].includes(item.kind) ||
          !Number.isSafeInteger(item.elapsedMs) || Number(item.elapsedMs) < 0 || Number(item.elapsedMs) > 180000 ||
          item.passed !== true;
      }) ||
      !Number.isSafeInteger(data.revision) || Number(data.revision) < 0 ||
      typeof data.state !== "string" || !STATES.has(data.state) ||
      typeof data.updatedAt !== "string" || data.updatedAt.length > 48 || !data.updatedAt.includes("T")) return null;
  // Older S11 servers omit the complete release projection. A partial or
  // malformed S12 projection must not turn an upgrade into a usable release.
  const releaseKeys = ["releaseState", "releaseGeneration", "releaseBindingSha256", "releaseEvidenceSha256"];
  const hasRelease = releaseKeys.some(key => Object.hasOwn(data, key));
  if (hasRelease && (
      !releaseKeys.every(key => Object.hasOwn(data, key)) ||
      typeof data.releaseState !== "string" || !RELEASE_STATES.has(data.releaseState) ||
      !(data.releaseGeneration === null || (Number.isSafeInteger(data.releaseGeneration) && Number(data.releaseGeneration) >= 0)) ||
      ![data.releaseBindingSha256, data.releaseEvidenceSha256].every(value => value === null || (typeof value === "string" && SHA.test(value))) ||
      (data.releaseState === "released" && (data.releaseGeneration === null || data.releaseBindingSha256 === null || data.releaseEvidenceSha256 === null))
  )) return null;
  return { id: data.id, workspaceId, slug: data.slug, oldCommit: data.oldCommit, newCommit: data.newCommit,
    suiteSha256: data.suiteSha256, evidenceSha256: data.evidenceSha256,
    passedTests: Number(data.passedTests),
    tests: (data.tests as Record<string, unknown>[]).map(item => ({
      name: item.name as string, kind: item.kind as string,
      elapsedMs: item.elapsedMs as number, passed: true as const,
    })), revision: Number(data.revision),
    state: data.state, updatedAt: data.updatedAt,
    ...(hasRelease ? {
      releaseState: data.releaseState as ReleaseState,
      releaseGeneration: data.releaseGeneration as number | null,
      releaseBindingSha256: data.releaseBindingSha256 as string | null,
      releaseEvidenceSha256: data.releaseEvidenceSha256 as string | null,
    } : {}),
  };
}

export function releaseStatus(record: UpgradeRecord): { label: string; detail: string; attention: boolean } {
  switch (record.releaseState) {
    case "repinned": return { label: "Awaiting postflight", detail: "The installed tool has been verified. It stays unavailable until postflight checks pass.", attention: false };
    case "testing": return { label: "Postflight running", detail: "The host is checking the installed tool before releasing it for use.", attention: false };
    case "failed": return { label: "Postflight failed", detail: "The updated tool is unavailable. Review the host's postflight result before continuing.", attention: true };
    case "indeterminate": return { label: "Release unresolved", detail: "The tool remains unavailable while the operator reconciles the interrupted release.", attention: true };
    case "released": return { label: "Released", detail: "The approved version passed postflight and was released for use. Normal execution permissions still apply.", attention: false };
    default: return { label: "Release not established", detail: "This record does not establish a postflight release for the updated tool.", attention: false };
  }
}
export function upgradeListing(value: unknown, workspaceId: string): ToolUpgradeListing | null {
  const data = doc(value);
  if (!data || data.schema !== "potbot-tool-upgrades-s11/v1" || !Array.isArray(data.items) || data.items.length > 100) return null;
  const items: UpgradeRecord[] = [];
  for (const entry of data.items) {
    const parsed = upgradeRecord(entry, workspaceId);
    if (!parsed) return null;
    items.push(parsed);
  }
  return { schema: "potbot-tool-upgrades-s11/v1", items };
}
