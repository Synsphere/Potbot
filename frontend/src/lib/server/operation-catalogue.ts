/** Bounded read from the existing workspace backend. No discovery or execution. */
import { safeError } from "./security.ts";

export async function catalogueJSON(response: Response): Promise<unknown> {
  if (!response.body) throw new Error("Missing catalogue body");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      total += part.value.byteLength;
      if (total > 1_048_576) { await reader.cancel(); throw new Error("Catalogue body exceeds bound"); }
      chunks.push(part.value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}

export function catalogueChanged(): Response {
  return safeError("The operation catalogue changed. Refresh to start a new snapshot.", 409);
}
