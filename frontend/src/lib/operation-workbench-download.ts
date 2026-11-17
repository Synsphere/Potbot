/** Bounded, owned artifact consumption; bytes are released only after full digest verification. */
import { artifactBytes, projectArtifact, type Artifact } from "./operation-workbench.ts";

export const MAX_ARTIFACT_DOWNLOAD_BYTES = 16 * 1024 * 1024;
type Download = {
  workspaceId: string; principalId: string; operationId: string; jobId: string; artifact: Artifact;
  signal: AbortSignal; current: () => boolean; progress?: (bytes: number, total: number) => void;
  read?: typeof fetch;
};

export async function readVerifiedArtifact(options: Download): Promise<Uint8Array<ArrayBuffer>> {
  const { workspaceId, principalId, operationId, jobId, artifact, signal, current, progress, read = fetch } = options;
  if (!Number.isSafeInteger(artifact.bytes) || artifact.bytes < 0 || artifact.bytes > MAX_ARTIFACT_DOWNLOAD_BYTES)
    throw new Error("The result artifact exceeds the browser's 16 MiB download bound.");
  const active = () => { if (signal.aborted || !current()) throw new DOMException("Artifact read was cancelled", "AbortError"); };
  active();
  const bytes = new Uint8Array(artifact.bytes); let offset = 0;
  for (let range = 0; range <= 64; range++) {
    const response = await read(`/api/ui/operations/${operationId}/jobs/${jobId}/artifact?offset=${offset}&length=262144`, {
      credentials: "same-origin", cache: "no-store", headers: { "X-Potbot-Workspace": workspaceId }, signal,
    });
    active();
    if (!response.ok) throw new Error("The owned result artifact is unavailable.");
    const part = projectArtifact(await response.json(), workspaceId, operationId, offset, 262_144, principalId);
    active();
    if (!part || part.artifact.id !== artifact.id || part.artifact.sha256 !== artifact.sha256 ||
        part.artifact.bytes !== artifact.bytes || part.artifact.mediaType !== artifact.mediaType)
      throw new Error("The result artifact identity or range changed while reading it.");
    const chunk = artifactBytes(part.dataBase64); bytes.set(chunk, offset); offset += chunk.length;
    progress?.(offset, artifact.bytes);
    if (part.nextOffset === null) break;
    if (range === 64) throw new Error("The result artifact exceeded the bounded download window.");
  }
  if (offset !== artifact.bytes) throw new Error("The result artifact is incomplete.");
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), value => value.toString(16).padStart(2, "0")).join("");
  active();
  if (digest !== artifact.sha256) throw new Error("The result artifact failed its SHA-256 integrity check.");
  return bytes;
}
