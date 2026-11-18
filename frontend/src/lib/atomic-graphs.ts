/** Strict, read-only presentation contracts for workspace-owned atomic graphs. */
export const UUID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/;
export const SHA = /^[0-9a-f]{64}$/;
const NODE = /^[A-Za-z0-9_-]{1,64}$/;
const STATES = new Set(["ready", "waiting", "interrupted", "queued", "running", "cancelling", "succeeded", "failed", "cancelled", "indeterminate", "blocked_dependency"]);
const OUTCOMES = new Set(["interrupted", "succeeded", "needs_reconciliation", "running", "blocked", "ready", "waiting"]);
const asObject = (x: unknown): Record<string, unknown> | null => x !== null && typeof x === "object" && !Array.isArray(x) ? x as Record<string, unknown> : null;

export type GraphListItem = {
  graph_id: string; objective: string; state: "active" | "interrupted";
  max_parallel: number; manifest_hash: string; created_at: string;
};
export type GraphList = {
  schema: "potbot-atomic-graph-list/v1"; workspace_id: string; principal_id: string;
  items: GraphListItem[]; next_cursor: string | null; has_more: boolean;
};
export type GraphNode = {
  node_id: string; operation_name: string; dependencies: string[];
  job_id: string | null; state: string; error_code: string | null;
  possible_effect: boolean; resource_holds: number;
};
export type GraphStatus = {
  schema: "potbot-atomic-graph-status/v1"; graph_id: string;
  state: "active" | "interrupted"; outcome: string; max_parallel: number;
  manifest_hash: string; nodes: GraphNode[];
};

export function graphList(value: unknown, workspace: string): GraphList | null {
  const x = asObject(value);
  if (!x || x.schema !== "potbot-atomic-graph-list/v1" || x.workspace_id !== workspace ||
      !UUID.test(String(x.principal_id)) || !Array.isArray(x.items) || x.items.length > 100 ||
      typeof x.has_more !== "boolean" || !(x.next_cursor === null ||
      typeof x.next_cursor === "string" && x.next_cursor.length <= 200)) return null;
  const items: GraphListItem[] = [];
  const seen = new Set<string>();
  for (const raw of x.items) {
    const r = asObject(raw);
    if (!r || typeof r.graph_id !== "string" || !UUID.test(r.graph_id) || seen.has(r.graph_id) ||
        typeof r.objective !== "string" || r.objective.length > 32768 ||
        !["active", "interrupted"].includes(String(r.state)) ||
        !Number.isInteger(r.max_parallel) || Number(r.max_parallel) < 1 || Number(r.max_parallel) > 64 ||
        typeof r.manifest_hash !== "string" || !SHA.test(r.manifest_hash) ||
        typeof r.created_at !== "string" || r.created_at.length > 80 || !Number.isFinite(Date.parse(r.created_at))) return null;
    seen.add(r.graph_id);
    items.push(r as GraphListItem);
  }
  return x as GraphList;
}

export function graphStatus(value: unknown, graphId: string): GraphStatus | null {
  const x = asObject(value);
  if (!x || x.schema !== "potbot-atomic-graph-status/v1" || x.graph_id !== graphId ||
      !["active", "interrupted"].includes(String(x.state)) || !OUTCOMES.has(String(x.outcome)) ||
      !Number.isInteger(x.max_parallel) || Number(x.max_parallel) < 1 || Number(x.max_parallel) > 64 ||
      typeof x.manifest_hash !== "string" || !SHA.test(x.manifest_hash) ||
      !Array.isArray(x.nodes) || x.nodes.length > 128) return null;
  const ids = new Set<string>();
  for (const raw of x.nodes) {
    const node = asObject(raw);
    if (!node || typeof node.node_id !== "string" || !NODE.test(node.node_id) || ids.has(node.node_id) ||
        typeof node.operation_name !== "string" || node.operation_name.length > 128 ||
        !Array.isArray(node.dependencies) || node.dependencies.length > 16 ||
        node.dependencies.some(d => typeof d !== "string" || !NODE.test(d)) ||
        !(node.job_id === null || typeof node.job_id === "string" && UUID.test(node.job_id)) ||
        !STATES.has(String(node.state)) ||
        !(node.error_code === null || typeof node.error_code === "string" && node.error_code.length <= 120) ||
        typeof node.possible_effect !== "boolean" || !Number.isSafeInteger(node.resource_holds) ||
        Number(node.resource_holds) < 0 || Number(node.resource_holds) > 32) return null;
    ids.add(node.node_id);
  }
  if (x.nodes.some(raw => (raw as GraphNode).dependencies.some(d => !ids.has(d)))) return null;
  return x as GraphStatus;
}
