// Synthetic unit fixtures. The separate contract fixture is exported by real HTTP/PostgreSQL tests.
export const workspaceId = '11111111-1111-4111-8111-111111111111', principalId = '22222222-2222-4222-8222-222222222222';
export const agentId = '33333333-3333-4333-8333-333333333333', bindingId = '44444444-4444-4444-8444-444444444444', snapshotId = '55555555-5555-4555-8555-555555555555', other = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';
export const scope = { schema: 'potbot-mcp-discovery/v1', workspaceId, principalId };
export const hash = digit => digit.repeat(64), clone = value => structuredClone(value);
export const catalogInfo = {
  protocolVersion: '2026-07-28', serverInfo: { name: 'fixture', version: '1.0', websiteUrl: 'https://untrusted.example' },
  capabilities: { tools: { listChanged: true }, resources: { listChanged: false, subscribe: true }, prompts: {} },
  collections: { tools: 'observed', resources: 'observed', resourceTemplates: 'observed', prompts: 'observed' },
  notificationSupport: { tools: true, resources: false, prompts: false }, cacheTtlMs: null,
};
export const binding = {
  id: bindingId, name: 'Configured fixture', agentId, revision: 3, state: 'enabled', bindingSha256: hash('a'), approvalValid: true,
  installationSha256: hash('b'), configurationSha256: hash('c'), definitionSha256: hash('d'), toolName: 'inspect', toolFingerprint: hash('e'),
  transport: 'stdio', ttlSeconds: 300, watchMs: 0, snapshotId, snapshotSha256: hash('f'), fetchedAt: '2026-11-18T12:00:00+00:00', expiresAt: '2026-11-18T12:05:00+00:00',
  stale: false, lastErrorCode: null, activeAttemptId: null, lastAttemptId: null,
  counts: { tools: 1, resources: 1, resourceTemplates: 1, prompts: 1 }, catalogInfo,
  actions: ['disable', 'reconcile', 'revoke'], createdAt: '2026-11-18T11:59:00+00:00', updatedAt: '2026-11-18T12:00:00+00:00',
};
const commonEntry = { bindingId, snapshotId, snapshotSha256: binding.snapshotSha256, configuredTool: false, referenceAvailable: true, reasonCodes: [], executable: false };
export const tool = { ...commonEntry, id: '60000000-0000-4000-8000-000000000000', kind: 'tool', identity: 'inspect', name: 'inspect', description: 'Inspect retained data',
  definition: { name: 'inspect', description: 'Inspect retained data', inputSchema: { type: 'object', properties: {} }, annotations: { readOnlyHint: true } }, definitionSha256: hash('1'), configuredTool: true };
export const resource = { ...commonEntry, id: '70000000-0000-4000-8000-000000000000', kind: 'resource', identity: 'file:///retained/report.txt', name: 'report', description: 'Read-only advertised report',
  definition: { name: 'report', description: 'Read-only advertised report', uri: 'file:///retained/report.txt', mimeType: 'text/plain' }, definitionSha256: hash('2') };
export const template = { ...commonEntry, id: '80000000-0000-4000-8000-000000000000', kind: 'resource_template', identity: 'report://{id}', name: 'reports', description: '',
  definition: { name: 'reports', uriTemplate: 'report://{id}' }, definitionSha256: hash('3') };
export const prompt = { ...commonEntry, id: '90000000-0000-4000-8000-000000000000', kind: 'prompt', identity: 'summarize', name: 'summarize', description: 'Review a report\nDo not execute it.',
  definition: { name: 'summarize', description: 'Review a report\nDo not execute it.', arguments: [{ name: 'report', required: true }] }, definitionSha256: hash('4') };
export const entries = [tool, resource, template, prompt];
export const requested = {
  id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', bindingId, agentId, revision: 1, bindingSha256: hash('5'), status: 'requested', requestedBindingSha256: binding.bindingSha256,
  installationSha256: binding.installationSha256, configurationSha256: binding.configurationSha256, snapshotId: null, snapshotSha256: null, changes: null, reasonCode: null,
  actions: ['cancel'], createdAt: '2026-11-18T12:01:00+00:00', updatedAt: '2026-11-18T12:01:00+00:00',
};
export const running = { ...requested, revision: 2, bindingSha256: hash('6'), status: 'running' };
export const succeeded = { ...requested, revision: 3, bindingSha256: hash('7'), status: 'succeeded', snapshotId, snapshotSha256: binding.snapshotSha256,
  changes: { added: 4, removed: 0, changed: 0, unchanged: 0 }, actions: [] };
export const disabled = { ...binding, revision: 4, state: 'disabled', bindingSha256: hash('8'), stale: true, actions: ['enable', 'revoke'] };
export const enabled = { ...binding, revision: 5, bindingSha256: hash('9') };
export const revoked = { ...disabled, revision: 5, state: 'revoked', bindingSha256: hash('0'), actions: [] };
export const cancelled = { ...requested, revision: 2, bindingSha256: hash('6'), status: 'cancelled', reasonCode: 'reconciliation_cancelled', actions: [] };
export const reference = { schema: 'potbot-mcp-reference/v1', workspaceId, principalId, bindingId, bindingRevision: binding.revision, bindingSha256: binding.bindingSha256,
  installationSha256: binding.installationSha256, snapshotId, snapshotSha256: binding.snapshotSha256, entryId: tool.id, kind: tool.kind, identity: tool.identity, definitionSha256: tool.definitionSha256, expiresAt: binding.expiresAt };
export const page = items => ({ ...scope, items, nextCursor: null });
export const decision = { revision: binding.revision, digest: binding.bindingSha256, confirm: true };
export const wire = { bindings: page([binding]), binding: { ...scope, binding }, entries: page(entries), attempts: page([requested]), requestedAttempt: { ...scope, attempt: requested },
  runningAttempt: { ...scope, attempt: running }, succeededAttempt: { ...scope, attempt: succeeded }, disabledBinding: { ...scope, binding: disabled }, enabledBinding: { ...scope, binding: enabled },
  revokedBinding: { ...scope, binding: revoked }, cancelledAttempt: { ...scope, attempt: cancelled }, reference: { ...scope, reference } };
