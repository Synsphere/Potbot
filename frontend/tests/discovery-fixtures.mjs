import { readFileSync } from 'node:fs';
export const wire = JSON.parse(readFileSync(new URL('./fixtures/discovery-http.json', import.meta.url), 'utf8'));
export const { workspaceId, principalId } = wire.sources;
export const scope = { schema: 'potbot-discovery/v1', workspaceId, principalId };
export const source = wire.sources.items[0], publisher = wire.approvedPublisher.publisher, candidate = wire.candidate.candidate;
export const pending = wire.pendingCheck.check, measured = wire.measuredCheck.check;
export const other = '11111111-1111-4111-8111-111111111111', clone = value => structuredClone(value);
