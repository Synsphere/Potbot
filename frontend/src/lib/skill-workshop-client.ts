/** Browser-only lifecycle helpers. A recovered mutation is never resubmitted here. */
import { MAX_FILE_BYTES, RESOURCE_CHUNK, decodeBase64, projectResource, validateSkillMutation, type SkillFile } from "./skill-acquisition.ts";
export type RequestTicket = { signal: AbortSignal; current: () => boolean; finish: () => void };

export class WorkshopRequests {
  private generation = 0;
  private requests = new Set<AbortController>();

  open(): RequestTicket {
    const controller = new AbortController(), generation = this.generation;
    this.requests.add(controller);
    return { signal: controller.signal, current: () => !controller.signal.aborted && generation === this.generation,
      finish: () => this.requests.delete(controller) };
  }

  reset(): void {
    this.generation++;
    for (const request of this.requests) request.abort();
    this.requests.clear();
  }
}

export type WorkshopAttempt = { path: string; body: string; key: string };
type JournalStorage = { read: (scope: string) => Promise<unknown>; write: (scope: string, value: WorkshopAttempt) => Promise<void>; remove: (scope: string, expectedKey: string) => Promise<void> };
const memory = new Map<string, WorkshopAttempt>();
let fallbackTab: string | undefined;

export function workshopJournalScope(workspace: string, principal: string): string {
  let tab: string | null = null;
  try {
    tab = window.sessionStorage.getItem("potbot-skill-workshop-tab");
    if (!tab || !/^[a-f0-9-]{36}$/.test(tab)) { tab = crypto.randomUUID(); window.sessionStorage.setItem("potbot-skill-workshop-tab", tab); }
  } catch { fallbackTab ??= crypto.randomUUID(); tab = fallbackTab; }
  return `${tab}:${workspace}:${principal}`;
}

function database(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const request = indexedDB.open("potbot-skill-workshop-v1", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("pending");
    request.onsuccess = () => { if (settled) request.result.close(); else { settled = true; resolve(request.result); } };
    request.onerror = () => { settled = true; reject(request.error ?? new Error("Recovery storage is unavailable")); };
    request.onblocked = () => { settled = true; reject(new Error("Recovery storage is busy")); };
  });
}

async function transaction(scope: string, action: "read" | "write" | "remove", value?: WorkshopAttempt, expectedKey?: string): Promise<unknown> {
  const db = await database();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction("pending", action === "read" ? "readonly" : "readwrite"), store = tx.objectStore("pending");
      const request = store.get(scope);
      let result: unknown;
      request.onsuccess = () => {
        result = request.result;
        if (action === "write") {
          // Duplicate Tab can copy sessionStorage's tab identity. Preserve an
          // unresolved slot belonging to the other tab in this same transaction.
          if (!value || (result !== undefined && (!result || typeof result !== "object" || !("key" in result) || result.key !== value.key ||
              !("body" in result) || result.body !== value.body || !("path" in result) || result.path !== value.path))) tx.abort();
          else store.put(value, scope);
        }
        if (action === "remove" && result && typeof result === "object" && "key" in result && result.key === expectedKey) store.delete(scope);
      };
      tx.oncomplete = () => resolve(result);
      tx.onerror = () => reject(tx.error ?? new Error("Recovery storage is unavailable"));
      tx.onabort = () => reject(tx.error ?? new Error("Recovery storage was interrupted"));
    });
  } finally { db.close(); }
}

const browserStorage: JournalStorage = {
  read: scope => transaction(scope, "read"),
  write: async (scope, value) => { await transaction(scope, "write", value); },
  remove: async (scope, key) => { await transaction(scope, "remove", undefined, key); },
};

export async function retainAttempt(scope: string, attempt: WorkshopAttempt, storage: JournalStorage = browserStorage): Promise<boolean> {
  const current = memory.get(scope);
  if (current && (current.key !== attempt.key || current.body !== attempt.body || current.path !== attempt.path)) throw new Error("Another exact request is unresolved in this tab.");
  memory.set(scope, attempt);
  try { await storage.write(scope, attempt); return true; } catch { return false; }
}

export async function recoverAttempt(scope: string, valid: (value: unknown) => WorkshopAttempt | null, storage: JournalStorage = browserStorage): Promise<WorkshopAttempt | null> {
  const current = memory.get(scope); if (current) return valid(current);
  try { return valid(await storage.read(scope)); } catch { return null; }
}

export async function clearAttempt(scope: string, key: string, storage: JournalStorage = browserStorage): Promise<void> {
  const current = memory.get(scope);
  if (current && current.key !== key) return;
  memory.delete(scope);
  try { await storage.remove(scope, key); } catch { /* The confirmed receipt remains recoverable if browser storage is unavailable. */ }
}

export async function clearRejectedAttempt(scope: string, key: string, status: number, recovering: boolean, storage: JournalStorage = browserStorage): Promise<boolean> {
  // Rejected authorization on a retry says nothing about an earlier commit.
  if (recovering || ![400, 401, 403, 404, 409, 413, 422].includes(status)) return false;
  await clearAttempt(scope, key, storage); return true;
}

export function newAttempt(path: string, body: unknown): WorkshopAttempt {
  return { path, body: JSON.stringify(body), key: "skill:" + crypto.randomUUID() };
}

export function validateWorkshopAttempt(value: unknown): WorkshopAttempt | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  if (Object.keys(row).sort().join(",") !== "body,key,path" || typeof row.path !== "string" || !row.path.startsWith("/api/ui/skills/") ||
      typeof row.body !== "string" || new TextEncoder().encode(row.body).length > 6 * 1024 * 1024 ||
      typeof row.key !== "string" || !/^[A-Za-z0-9_.:-]{1,128}$/.test(row.key)) return null;
  try {
    return validateSkillMutation(row.path.slice("/api/ui/skills/".length).split("/"), JSON.parse(row.body)) ? { path: row.path, body: row.body, key: row.key } : null;
  } catch { return null; }
}

export async function readSkillResource(options: { base: string; workspaceId: string; principalId: string; file: SkillFile;
  releaseSha256?: string; ticket: RequestTicket; read?: typeof fetch }): Promise<Uint8Array<ArrayBuffer>> {
  const { base, workspaceId, principalId, file, releaseSha256, ticket, read = fetch } = options;
  if (file.size > MAX_FILE_BYTES || !Number.isSafeInteger(file.size) || file.size < 0) throw new Error("The resource exceeds its download bound.");
  const current = () => { if (!ticket.current()) throw new DOMException("Resource read cancelled", "AbortError"); };
  current(); const bytes = new Uint8Array(file.size); let offset = 0;
  for (let index = 0; index <= 16; index++) {
    const query = new URLSearchParams({ path: file.path, offset: String(offset), length: String(RESOURCE_CHUNK) });
    if (releaseSha256) query.set("releaseSha256", releaseSha256);
    const response = await read(`${base}/resources?${query}`, { credentials: "same-origin", cache: "no-store", signal: ticket.signal, headers: { "X-Potbot-Workspace": workspaceId } });
    current(); if (!response.ok) throw new Error("The exact resource is unavailable or its release is no longer permitted.");
    const page = projectResource(await response.json(), workspaceId, file.path, offset, RESOURCE_CHUNK, principalId);
    current();
    if (!page || page.resource.sha256 !== file.sha256 || page.resource.size !== file.size || page.resource.mediaType !== file.media_type) throw new Error("The resource identity changed during the read.");
    const chunk = decodeBase64(page.dataBase64, RESOURCE_CHUNK); bytes.set(chunk, offset); offset += chunk.length;
    if (page.nextOffset === null) break;
    if (index === 16) throw new Error("The resource exceeded its bounded read window.");
  }
  if (offset !== file.size) throw new Error("The resource is incomplete.");
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
  current(); if (digest !== file.sha256) throw new Error("The resource failed its SHA-256 integrity check.");
  return bytes;
}

export function base64Bytes(value: Uint8Array): string {
  let text = "";
  for (let offset = 0; offset < value.length; offset += 32_768) text += String.fromCharCode(...value.subarray(offset, offset + 32_768));
  return btoa(text);
}
