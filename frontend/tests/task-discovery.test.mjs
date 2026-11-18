import test from 'node:test';
import assert from 'node:assert/strict';
import { projectTaskConstraints, projectTaskDiscoveryContext, projectTaskCandidate, projectTaskEvidence, projectTaskSearch, projectTaskResult, projectTaskSelection, projectTaskEnvelope, projectTaskSearches, projectTaskMutation, validateTaskMutation, parseMcpJSON, sameTaskConstraints, sameTaskValue } from '../src/lib/task-discovery.ts';
import { taskCanSelect, taskResultMatches, taskSelectionMatches, taskContextMatches, taskOwnerHref, taskHandoffHref, taskDiscoverySelection, exactOwnerSelection, taskDiscoveryNeedsMonitoring, taskDiscoveryScopeInvalidated } from '../src/lib/task-discovery-client.ts';
import { workspaceId as w, principalId as p, other, scope, hash, uuid, clone, taskBinding, constraints, context, embedding, create, candidate, completed, requested, cancelled, selected, result, selection, semantic, wire } from './task-discovery-fixtures.mjs';

test('task DTOs keep exact scoped evidence while discarding unrelated outer fields', () => {
  for (const [kind, value] of [['context', wire.context], ['search', wire.search], ['result', wire.result], ['selection', wire.selection], ['candidate', wire.candidate]]) {
    const identity = kind === 'context' ? undefined : value[kind].id;
    assert.deepEqual(projectTaskEnvelope({ ...value, credentials: 'PRIVATE' }, w, kind, identity, p), value);
    assert.equal(projectTaskEnvelope(value, other, kind, identity, p), null);
    assert.equal(projectTaskEnvelope(value, w, kind, identity, other), null);
    if (kind !== 'context') assert.equal(projectTaskEnvelope(value, w, kind, 'wrong-id', p), null);
  }
});

test('context binds exact task, state and bounded registered profiles', () => {
  assert.deepEqual(projectTaskDiscoveryContext(context), context); assert.equal(taskContextMatches(context, taskBinding.episodeId, taskBinding.repositoryStateId), true); assert.equal(taskContextMatches(context, taskBinding.episodeId, other), false);
  for (const patch of [{ taskStatus: 'completed' }, { taskCapabilities: ['x'.repeat(257)] }, { counts: { registry: 256, operations: 1, mcp: 0 } }, { embeddingSpaces: [...context.embeddingSpaces, ...context.embeddingSpaces] }, { taskBinding: { ...taskBinding, privatePath: 'PRIVATE' } }]) assert.equal(projectTaskDiscoveryContext({ ...context, ...patch }), null);
  for (const patch of [{ dimensions: 0 }, { dimensions: 768 }, { revision: hash('A') }, { embeddingName: 'x'.repeat(65) }, { maxBatchSize: 65 }, { maxInputTokens: 32769 }, { runtimePath: 'PRIVATE' }]) assert.equal(projectTaskDiscoveryContext({ ...context, embeddingSpaces: [{ ...context.embeddingSpaces[0], ...patch }] }), null);
});

test('hard constraints preserve explicit empty permissions and canonical aliases', () => {
  assert.deepEqual(projectTaskConstraints({ ...constraints, permittedEffects: [], permittedScopes: [] }), { ...constraints, permittedEffects: [], permittedScopes: [] });
  assert.equal(sameTaskConstraints(constraints, { ...constraints, permittedScopes: [] }), false);
  const a = { ...constraints, classes: ['code_search', 'file_inspection'], inputTypes: ['synny://type/object'], outputTypes: ['syn://type/potbot-data-text'], permittedScopes: ['b', 'a'] };
  const b = { ...constraints, classes: ['file_inspection', 'code_search'], inputTypes: ['syn://type/object'], outputTypes: ['synny://type/potbot-data-text'], permittedScopes: ['a', 'b'] };
  assert.equal(sameTaskConstraints(a, b), true);
  assert.equal(projectTaskConstraints({ ...constraints, permittedScopes: Array.from({ length: 64 }, (_, i) => `scope${i}`) })?.permittedScopes.length, 64);
  for (const patch of [{ classes: ['invented'] }, { inputTypes: ['https://evil.example'] }, { inputTypes: ['syn://type/object#evil'] }, { permittedEffects: ['arbitrary'] }, { requireLocal: 1 }, { permittedScopes: Array.from({ length: 65 }, (_, i) => `scope${i}`) }, { inputTypes: ['syn://type/object', 'syn://type/object'] }]) assert.equal(projectTaskConstraints({ ...constraints, ...patch }), null);
});

test('candidate admission never upgrades permission, evidence or execution', () => {
  assert.deepEqual(projectTaskCandidate(candidate, true), candidate);
  for (const patch of [{ executable: true }, { executable: null }, { ownerId: other }, { bindingId: other }, { score: -1 }, { score: Number.MAX_SAFE_INTEGER + 1 }, { taskPermission: 'approved' }, { privateArguments: {} }, { capabilities: { ...candidate.capabilities, scopes: Array.from({ length: 33 }, (_, i) => `scope${i}`) } }]) assert.equal(projectTaskCandidate({ ...candidate, ...patch }, true), null);
  for (const patch of [{ suiteId: other }, { reportSha256: hash('a') }, { caseCount: 1 }, { passedCases: 1 }]) assert.equal(projectTaskEvidence({ ...candidate.taskEvidence, ...patch }), null);
  const evidence = { status: 'matching_pass', suiteId: uuid('a'), reportId: uuid('b'), reportSha256: hash('c'), caseCount: 2, passedCases: 2 };
  assert.deepEqual(projectTaskEvidence(evidence), evidence); assert.equal(projectTaskEvidence({ ...evidence, passedCases: 1 }), null); assert.equal(projectTaskEvidence({ ...evidence, caseCount: 9, passedCases: 9 }), null);
  assert.equal(projectTaskCandidate({ ...candidate, kind: 'mcp', ownerId: uuid('b'), bindingId: uuid('c'), taskEvidence: evidence }, true)?.kind, 'mcp');
});

test('search, result and selection joins reject mismatched snapshots and stale authority', () => {
  assert.equal(taskResultMatches(completed, result), true); assert.equal(taskSelectionMatches(selected, result, selection), true); assert.equal(taskCanSelect(completed, result, candidate), true);
  assert.equal(taskResultMatches(completed, { ...result, consumerSha256: hash('f') }), false); assert.equal(taskResultMatches(completed, { ...result, taskBinding: { ...taskBinding, repositoryStateId: other } }), false);
  assert.equal(taskSelectionMatches(selected, result, { ...selection, owner: { ...selection.owner, ownerSha256: hash('f') } }), false);
  for (const patch of [{ approvalValid: false }, { authorityCurrent: false, reasonCodes: ['task_changed'] }, { actions: [] }, { status: 'selected' }]) assert.equal(taskCanSelect({ ...completed, ...patch }, result, candidate), false);
  assert.equal(taskCanSelect(completed, { ...result, authorityCurrent: false }, candidate), false); assert.equal(taskCanSelect(completed, result, { ...candidate, taskPermission: 'denied' }), false);
  assert.equal(taskDiscoveryNeedsMonitoring(requested), true); assert.equal(taskDiscoveryNeedsMonitoring(completed), false); assert.equal(taskDiscoveryNeedsMonitoring(cancelled), false);
  assert.equal(projectTaskSearch({ ...completed, status: 'failed', actions: ['select'] }), null); assert.equal(projectTaskSearch({ ...completed, resultId: null }), null);
  assert.equal(projectTaskSearch({ ...completed, authorityCurrent: true, reasonCodes: ['stale'] }), null);
  assert.equal(sameTaskValue(taskBinding, Object.fromEntries(Object.entries(taskBinding).reverse())), true);
  assert.equal(taskSelectionMatches({ ...selected, taskBinding: Object.fromEntries(Object.entries(taskBinding).reverse()) }, result, selection), true);
});

test('results validate exclusions, empty hybrid evidence and bounded native summaries', () => {
  assert.deepEqual(projectTaskResult(result), result); assert.equal(projectTaskResult({ ...result, noSuitableCandidate: true }), null);
  assert.equal(projectTaskResult({ ...result, excluded: [{ id: candidate.id, reasonCodes: ['denied'] }] }), null); assert.equal(projectTaskResult({ ...result, excluded: [{ id: hash('d'), reasonCodes: [] }] }), null);
  assert.equal(projectTaskResult({ ...result, items: [candidate, candidate] }), null); assert.equal(projectTaskResult({ ...result, mode: 'hybrid' }), null);
  const empty = { ...result, mode: 'hybrid', items: [], noSuitableCandidate: true }; assert.deepEqual(projectTaskResult(empty), empty);
  assert.equal(projectTaskResult({ ...result, mode: 'hybrid', semantic })?.semantic.usageOrigin, 'synthetic');
  for (const patch of [{ calls: 18 }, { elapsedMs: Number.MAX_SAFE_INTEGER + 1 }, { usageOrigin: 'real' }, { vectors: [[1]] }, { inputTokens: -1 }]) assert.equal(projectTaskResult({ ...result, mode: 'hybrid', semantic: { ...semantic, ...patch } }), null);
  assert.equal(projectTaskResult({ ...result, semantic }), null); assert.equal(projectTaskResult({ ...result, mode: 'hybrid', semantic: { ...semantic, elapsedMs: 120001 } })?.semantic.elapsedMs, 120001);
});

test('exact mutations reject ambient authority and bind receipts to reviewed request', () => {
  assert.deepEqual(validateTaskMutation(['searches'], create), create); assert.ok(projectTaskMutation(wire.search, w, ['searches'], create, p));
  assert.ok(projectTaskMutation({ ...scope, search: { ...completed, constraints: Object.fromEntries(Object.entries(constraints).reverse()) } }, w, ['searches'], create, p));
  for (const patch of [{ confirm: false }, { query: '' }, { query: 'x'.repeat(501) }, { limit: 11 }, { mode: 'hybrid' }, { embedding }, { contextSha256: hash('A') }, { url: 'https://evil.example' }, { scores: {} }]) assert.equal(validateTaskMutation(['searches'], { ...create, ...patch }), null);
  const select = { revision: completed.revision, digest: completed.bindingSha256, candidateId: candidate.id, confirm: true };
  assert.ok(validateTaskMutation(['searches', completed.id, 'select'], select)); assert.ok(projectTaskMutation(wire.selection, w, ['searches', completed.id, 'select'], select, p));
  assert.equal(projectTaskMutation(wire.selection, w, ['searches', other, 'select'], select, p), null); assert.equal(projectTaskMutation(wire.search, w, ['searches'], { ...create, stateId: other }, p), null);
  assert.equal(validateTaskMutation(['searches', completed.id, 'execute'], select), null); assert.equal(validateTaskMutation(['searches', completed.id, 'select'], { ...select, candidateId: other }), null);
  assert.ok(projectTaskMutation(wire.cancelled, w, ['searches', requested.id, 'cancel'], { revision: requested.revision, digest: requested.bindingSha256, confirm: true }, p));
});

test('fixed handoffs and exact deep links cannot change owner or workspace', () => {
  assert.equal(taskHandoffHref(w, selection), `/tools?workspace=${w}&operation=${candidate.ownerId}`); assert.equal(taskHandoffHref(w, { ...selection, authorityCurrent: false }), null);
  assert.equal(taskOwnerHref(w, { kind: 'registry', ownerId: other, bindingId: null }), `/discover?workspace=${w}&candidate=${other}`);
  assert.equal(taskOwnerHref(w, { kind: 'mcp', ownerId: uuid('b'), bindingId: other }), `/discover/mcp?workspace=${w}&binding=${other}`);
  assert.equal(projectTaskSelection({ ...selection, handoff: { ...selection.handoff, url: 'https://evil.example' } }), null); assert.equal(projectTaskSelection({ ...selection, handoff: { ...selection.handoff, identity: hash('f') } }), null);
  const query = `?workspace=${w}&episode=${taskBinding.episodeId}&state=${taskBinding.repositoryStateId}&search=${completed.id}`;
  assert.deepEqual(taskDiscoverySelection(query, w), { episode: taskBinding.episodeId, state: taskBinding.repositoryStateId, search: completed.id });
  assert.deepEqual(taskDiscoverySelection(query, other), { episode: null, state: null, search: null }); assert.equal(taskDiscoverySelection(query + '&search=' + other, w).search, null);
  assert.equal(exactOwnerSelection(`?workspace=${w}&operation=${candidate.ownerId}`, w, 'operation'), candidate.ownerId);
  assert.equal(exactOwnerSelection(`?workspace=${w}&candidate=${other}`, w, 'candidate'), other);
  for (const query of [`?workspace=${other}&operation=${candidate.ownerId}`, `?workspace=${w}&operation=${candidate.ownerId}&operation=${hash('f')}`, `?workspace=${w}&workspace=${w}&operation=${candidate.ownerId}`, `?workspace=${w}&operation=https://evil.example`]) assert.equal(exactOwnerSelection(query, w, 'operation'), null);
});

test('scope changes and progressive JSON are bounded and fail closed', () => {
  assert.equal(taskDiscoveryScopeInvalidated(403, null, w, p), true); assert.equal(taskDiscoveryScopeInvalidated(200, { ...scope, principalId: other }, w, p), true); assert.equal(taskDiscoveryScopeInvalidated(200, { ...scope, workspaceId: other }, w, p), true);
  assert.equal(projectTaskSearches({ ...wire.searches, nextCursor: other }, w, p), null); assert.equal(projectTaskSearches({ ...wire.searches, items: [completed, completed] }, w, p), null);
  assert.equal(projectTaskSearches(wire.searches, w, p, other), null); assert.equal(projectTaskEnvelope({ ...wire.candidate, detail: [] }, w, 'candidate', candidate.id, p), null);
  assert.equal(projectTaskEnvelope({ ...wire.candidate, detail: { text: 'x'.repeat(131072) } }, w, 'candidate', candidate.id, p), null);
  assert.throws(() => parseMcpJSON('{"confirm":true,"confirm":false}')); assert.throws(() => parseMcpJSON('{"constraints":{"requireLocal":true,"requireLocal":false}}'));
});
