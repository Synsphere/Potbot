/** Selection links enter existing acquisition owners without submitting work. */
import { UUID } from "./operation-workbench.ts";

export function acquisitionSelection(search: string, workspaceId: string, field: "package" | "operation" | "check"): string | null {
  if (!UUID.test(workspaceId)) return null;
  const query = new URLSearchParams(search), workspace = query.getAll("workspace"), target = query.getAll(field);
  return workspace.length === 1 && workspace[0] === workspaceId && target.length === 1 && UUID.test(target[0]) ? target[0] : null;
}

export function acquisitionReviewLink(workspaceId: string, kind: "skill_package" | "tool_acquisition", id: string): string | null {
  if (!UUID.test(workspaceId) || !UUID.test(id)) return null;
  if (kind === "skill_package") return `/skills?workspace=${workspaceId}&package=${id}`;
  if (kind === "tool_acquisition") return `/setup/tools?workspace=${workspaceId}&operation=${id}`;
  return null;
}
