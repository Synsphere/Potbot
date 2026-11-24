import { SHA256, UUID } from "./operation-workbench.ts";

export const MAX_REASONING_PLAN = 32_768;
export const REASONING_ROLES = ["hypothesis_generator", "critic", "synthesizer"] as const;
export const REASONING_ROLE_LABELS = ["Generate hypotheses", "Critique hypotheses", "Synthesize answer"];

type Role = typeof REASONING_ROLES[number];
type State = "active" | "completed" | "blocked" | "interrupted";
type Receipt = { decision_id: string; output_sha256: string };
export type ReasoningStatus = {
  run_id: string;
  state: State;
  stage_index: number;
  phase: Role | null;
  decision_id: string | null;
  check_job_id: string | null;
  job_id: string | null;
  error_code: string | null;
  completed: Receipt[];
  operator_constraints?: string[];
};
export type ReasoningResult = Receipt & {
  job_id: string;
  text: string;
  semantic_correctness: "not_evaluated" | "verified_against_frozen_records";
};

export function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function uuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}
function hash(value: unknown): value is string {
  return typeof value === "string" && SHA256.test(value);
}

export function guidanceConstraints(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.length < 1 || value.length > 4 ||
      !value.every((item): item is string => typeof item === "string" &&
        item.length > 0 && item === item.trim() &&
        !/[\p{C}\p{Z}]/u.test(item.replaceAll(" ", "")) &&
        new TextEncoder().encode(item).length <= 256) ||
      new Set(value).size !== value.length) return null;
  return value;
}

export function reasoningPlan(value: unknown): Record<string, unknown> | null {
  const row = object(value);
  const required = ["task", "constraints", "evidence", "stages"];
  const permitted = [...required, "answer_check", "evidence_citations"];
  if (!row || !required.every(key => Object.hasOwn(row, key)) ||
      Object.keys(row).some(key => !permitted.includes(key)) ||
      !Array.isArray(row.stages) || row.stages.length !== REASONING_ROLES.length) return null;
  for (const [index, value] of row.stages.entries()) {
    const stage = object(value);
    if (!stage || Object.keys(stage).sort().join(",") !== "policy_id,request,role" ||
        stage.role !== REASONING_ROLES[index] || !uuid(stage.policy_id) || !object(stage.request)) return null;
  }
  return new TextEncoder().encode(JSON.stringify(row)).length <= MAX_REASONING_PLAN ? row : null;
}

function receipt(value: unknown): Receipt | null {
  const row = object(value);
  return row && uuid(row.decision_id) && hash(row.output_sha256)
    ? { decision_id: row.decision_id, output_sha256: row.output_sha256 } : null;
}

export function reasoningStatus(value: unknown, runId: string): ReasoningStatus | null {
  const row = object(value), index = row?.stage_index, state = row?.state;
  if (!row || !uuid(row.run_id) || row.run_id !== runId ||
      (state !== "active" && state !== "completed" && state !== "blocked" && state !== "interrupted") ||
      typeof index !== "number" || !Number.isInteger(index) || index < 0 || index > 3 ||
      (state === "completed") !== (index === 3) || row.phase !== (REASONING_ROLES[index] ?? null) ||
      !Array.isArray(row.completed) || row.completed.length !== index ||
      (row.decision_id !== null && !uuid(row.decision_id)) ||
      (row.check_job_id !== null && !uuid(row.check_job_id)) ||
      (row.job_id !== null && !uuid(row.job_id)) ||
      (row.error_code !== null && (typeof row.error_code !== "string" || !/^[a-z][a-z0-9_]{0,79}$/.test(row.error_code)))) return null;
  const completed: Receipt[] = [];
  for (const item of row.completed) {
    const record = receipt(item);
    if (!record) return null;
    completed.push(record);
  }
  const constraints = guidanceConstraints(row.operator_constraints);
  if (Object.hasOwn(row, "operator_constraints") && (!constraints || (index !== 1 && index !== 2))) return null;
  return {
    run_id: row.run_id, state, stage_index: index, phase: REASONING_ROLES[index] ?? null,
    decision_id: row.decision_id, check_job_id: row.check_job_id, job_id: row.job_id,
    error_code: row.error_code, completed,
    ...(constraints ? { operator_constraints: constraints } : {}),
  };
}

export function reasoningResult(value: unknown): ReasoningResult | null {
  const row = object(value), final = receipt(value);
  if (!row || !final || !uuid(row.job_id) || typeof row.text !== "string" || !row.text ||
      new TextEncoder().encode(row.text).length > 65_536 ||
      (row.semantic_correctness !== "verified_against_frozen_records" && row.semantic_correctness !== "not_evaluated")) return null;
  return { ...final, job_id: row.job_id, text: row.text, semantic_correctness: row.semantic_correctness };
}
