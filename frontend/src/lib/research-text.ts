/** Citation spans use UTF-8 bytes: start inclusive, end exclusive. */
import { SHA256 } from "./operation-workbench.ts";

export function researchUTF8(value: unknown): Uint8Array<ArrayBuffer> | null {
  if (typeof value !== "string") return null;
  const bytes = new TextEncoder().encode(value);
  return new TextDecoder("utf-8", { fatal: true }).decode(bytes) === value ? bytes : null;
}

export function researchQuote(text: string, startByte: number, endByte: number): { text: string; bytes: Uint8Array<ArrayBuffer> } | null {
  const source = researchUTF8(text);
  if (!source || !Number.isSafeInteger(startByte) || !Number.isSafeInteger(endByte) || startByte < 0 || endByte <= startByte || endByte > source.length) return null;
  const bytes = source.slice(startByte, endByte);
  try { return { text: new TextDecoder("utf-8", { fatal: true }).decode(bytes), bytes }; }
  catch { return null; }
}

export async function researchTextSha256(text: string): Promise<string> {
  const bytes = researchUTF8(text); if (!bytes) throw new Error("Evidence text must be valid UTF-8.");
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
}

export async function researchQuoteMatches(source: string, startByte: number, endByte: number, quote: string, quoteSha256: string): Promise<boolean> {
  if (!SHA256.test(quoteSha256)) return false;
  const selected = researchQuote(source, startByte, endByte);
  return !!selected && selected.text === quote && await researchTextSha256(selected.text) === quoteSha256;
}
