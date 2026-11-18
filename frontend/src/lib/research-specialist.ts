/** Scientific metadata is a retained provider claim, never a citation or verdict. */
import { researchObject as object, researchKeys as keys, researchBytes as bytes, researchTime as time } from "./research.ts";

export type ResearchMetadataDate = { value: string; precision: "year" | "month" | "day" | "instant" | "reported"; provenance: string };
export type ResearchNotice = { kind: "retraction" | "correction" | "update" | "expression_of_concern" | "withdrawal"; relation: "updates" | "updated_by" | "record_flag"; targetDoi: string | null; date: ResearchMetadataDate | null };
export type ResearchSpecialistMetadata = {
  schema: "potbot-specialist-metadata/v1";
  recordId: string;
  doi: string | null;
  authors: string[];
  publicationDate: ResearchMetadataDate | null;
  eventDate: ResearchMetadataDate | null;
  updatedDate: ResearchMetadataDate | null;
  sourceRole: "primary" | "secondary" | "unknown";
  recordKind: "publisher_registry" | "repository" | "bibliographic_index" | "knowledge_graph" | "archive_catalogue";
  retraction: { state: "reported_retracted" | "not_reported" | "unknown"; notices: ResearchNotice[] };
};

const DOI = /^10\.\d{4,9}\/[!-~]{1,230}$/;
export const researchDOI = (value: unknown): value is string => bytes(value, 255) && DOI.test(value) && !/[<>\\]/.test(value) && value === value.toLowerCase();

export function projectResearchMetadataDate(value: unknown): ResearchMetadataDate | null {
  const row = object(value);
  if (!row || !keys(row, ["value", "precision", "provenance"]) || !bytes(row.value, 128) || !bytes(row.provenance, 128) || row.precision !== "reported" && row.value.startsWith("0000")) return null;
  const valid = row.precision === "year" ? /^\d{4}$/.test(row.value)
    : row.precision === "month" ? /^\d{4}-(?:0[1-9]|1[0-2])$/.test(row.value)
    : row.precision === "day" ? /^\d{4}-\d{2}-\d{2}$/.test(row.value) && Number.isFinite(Date.parse(`${row.value}T00:00:00Z`)) && new Date(`${row.value}T00:00:00Z`).toISOString().slice(0, 10) === row.value
    : row.precision === "instant" ? time(row.value)
    : row.precision === "reported";
  return valid ? { ...row } as ResearchMetadataDate : null;
}

export function projectResearchSpecialistMetadata(value: unknown): ResearchSpecialistMetadata | null {
  const row = object(value), retraction = object(row?.retraction);
  if (!row || !keys(row, ["schema", "recordId", "doi", "authors", "publicationDate", "eventDate", "updatedDate", "sourceRole", "recordKind", "retraction"]) || row.schema !== "potbot-specialist-metadata/v1" || !bytes(row.recordId, 512) || !(row.doi === null || researchDOI(row.doi)) || !Array.isArray(row.authors) || row.authors.length > 64 || !row.authors.every(item => bytes(item, 512)) || !["primary", "secondary", "unknown"].includes(String(row.sourceRole)) || !["publisher_registry", "repository", "bibliographic_index", "knowledge_graph", "archive_catalogue"].includes(String(row.recordKind)) || !retraction || !keys(retraction, ["state", "notices"]) || !["reported_retracted", "not_reported", "unknown"].includes(String(retraction.state)) || !Array.isArray(retraction.notices) || retraction.notices.length > 20) return null;
  const publicationDate = row.publicationDate === null ? null : projectResearchMetadataDate(row.publicationDate), eventDate = row.eventDate === null ? null : projectResearchMetadataDate(row.eventDate), updatedDate = row.updatedDate === null ? null : projectResearchMetadataDate(row.updatedDate);
  if (row.publicationDate !== null && !publicationDate || row.eventDate !== null && !eventDate || row.updatedDate !== null && !updatedDate) return null;
  const notices: ResearchNotice[] = [];
  for (const raw of retraction.notices) {
    const notice = object(raw);
    if (!notice || !keys(notice, ["kind", "relation", "targetDoi", "date"]) || !["retraction", "correction", "update", "expression_of_concern", "withdrawal"].includes(String(notice.kind)) || !["updates", "updated_by", "record_flag"].includes(String(notice.relation)) || !(notice.targetDoi === null || researchDOI(notice.targetDoi))) return null;
    const date = notice.date === null ? null : projectResearchMetadataDate(notice.date);
    if (notice.date !== null && !date) return null;
    notices.push({ ...notice, date } as ResearchNotice);
  }
  return { ...structuredClone(row), publicationDate, eventDate, updatedDate, retraction: { state: retraction.state, notices } } as ResearchSpecialistMetadata;
}

export function researchMetadataDateLabel(date: ResearchMetadataDate | null): string {
  return date ? `${date.value} · ${date.precision} precision · ${date.provenance}` : "Not reported";
}

export function researchRetractionLabel(state: ResearchSpecialistMetadata["retraction"]["state"]): string {
  return state === "reported_retracted" ? "Retraction reported by this provider" : state === "not_reported" ? "No retraction reported in this record" : "Retraction status unavailable";
}
