import test from 'node:test';
import assert from 'node:assert/strict';
import { projectTaskOracle, boundedTaskLiteral, validTaskPointer, projectTaskExecutionContext, projectTaskExecutionBinding, projectTaskExecutionJob, projectTaskExecutionJobs, projectTaskExecutionEvents, projectTaskExecutionResult, projectTaskExecutionMutation, validateTaskExecutionMutation, projectTaskExecutionArtifact } from '../src/lib/task-execution.ts';
import { taskExecutionCanLaunch, taskExecutionScopeMatches, taskExecutionBindingMatchesContext, taskExecutionScopeInvalidated, selectedExecutionQuery, taskExecutionNeedsMonitoring } from '../src/lib/task-execution-client.ts';
import { taskHandoffHref } from '../src/lib/task-discovery-client.ts';
import { workspaceId as w, principalId as p, operationId as o, selectionId as s, checkId, runId, other, uuid, hash, clone, scope, context, binding, check, checkedJob, run, runJob, checkBody, runBody, acceptance, selection, result, events, jobs } from './task-execution-fixtures.mjs';

test('selected context joins exact selection, task and existing operation contract without granting execution', () => {
  assert.deepEqual(projectTaskExecutionContext(context, w, s, p, o), context);
  for (const patch of [{ workspaceId: other }, { principalId: other }, { selectionId: other }, { selectionSha256: hash('f') }, { operationId: hash('f') }, { taskBinding: { ...scope.taskBinding, repositoryStateId: other } }, { selection: { ...selection, authorityCurrent: false } }, { supported: true, operation: null }]) assert.equal(projectTaskExecutionContext({ ...context, ...patch }, w, s, p, o), null);
  const registry = { ...selection, owner: { kind: 'registry', ownerId: other, bindingId: null, ownerSha256: hash('a') }, handoff: { kind: 'registry', identity: other, bindingId: null } };
  const unsupported = { ...context, operationId: null, selection: registry, operation: null, supported: false, reasonCodes: ['execution_adapter_unavailable'] };
  assert.deepEqual(projectTaskExecutionContext(unsupported, w, s, p), unsupported);
  assert.equal(projectTaskExecutionContext({ ...unsupported, supported: true }, w, s, p), null);
  const stale = { ...context, current: false, selection: { ...selection, authorityCurrent: false, reasonCodes: ['task_context_changed'] }, reasonCodes: ['selection_not_current'] };
  assert.deepEqual(projectTaskExecutionContext(stale, w, s, p, o), stale);
});

test('fixed result assertions accept only finite bounded literal JSON and RFC 6901 pointers', () => {
  assert.deepEqual(projectTaskOracle(acceptance), acceptance);
  for (const pointer of ['', '/text', '/value/a~1b/~0', '/value/0']) assert.equal(validTaskPointer(pointer), true);
  for (const pointer of ['text', '/value/~2', '/x\u0000', '/x'.repeat(17), '/' + 'é'.repeat(512), '/\ud800']) assert.equal(validTaskPointer(pointer), false);
  for (const patch of [{ assertions: [] }, { assertions: Array(17).fill(acceptance.assertions[0]) }, { expression: 'eval' }, { assertions: [{ op: 'regex', pointer: '/text', value: '.*' }] }, { assertions: [{ op: 'contains', pointer: '/text', value: '' }] }, { assertions: [{ op: 'contains', pointer: '/text', value: 1 }] }, { assertions: [{ op: 'length_equals', pointer: '/text', value: -1 }] }, { assertions: [{ op: 'length_equals', pointer: '/text', value: 1.5 }] }, { assertions: [{ op: 'equals', pointer: '/value', value: Infinity }] }, { assertions: [{ op: 'equals', pointer: '/value', value: 'x'.repeat(16385) }] }]) assert.equal(projectTaskOracle({ ...acceptance, ...patch }), null);
  let deep = null; for (let i = 0; i < 34; i++) deep = [deep]; assert.equal(boundedTaskLiteral(deep, 65536), false);
  assert.equal(boundedTaskLiteral(Array(8193).fill(null), 65536), false);
  assert.equal(boundedTaskLiteral({ ['x'.repeat(513)]: true }, 65536), false);
  assert.equal(boundedTaskLiteral({ value: undefined }, 65536), false);
});

test('immutable binding verifies all provenance and implementation identities plus task-bound request', () => {
  assert.deepEqual(projectTaskExecutionBinding(binding, scope), binding);
  for (const patch of [{ selectionId: other }, { selectionSha256: hash('e') }, { operationId: hash('e') }, { principalId: other }, { consumerSha256: 'missing' }, { taskBinding: { ...binding.taskBinding, contractRevisionId: other } }, { request: { ...binding.request, episode_id: other } }, { request: { ...binding.request, input: { ...binding.request.input, repository_state_id: other } } }, { capabilities: { ...binding.capabilities, effects: ['write_everywhere'] } }, { extra: 'PRIVATE' }]) assert.equal(projectTaskExecutionBinding({ ...binding, ...patch }, scope), null);
  const absent = clone(binding); delete absent.consumerSha256; assert.equal(projectTaskExecutionBinding(absent, scope), null);
  assert.equal(taskExecutionBindingMatchesContext(binding, context), true);
  for (const patch of [{ searchId: other }, { resultId: other }, { candidateId: hash('f') }, { ownerSha256: hash('f') }]) assert.equal(taskExecutionBindingMatchesContext({ ...binding, ...patch }, context), false);
});

test('launch needs a current supported selection and unexpired eligible worker check with exact provenance', () => {
  const now = Date.parse('2026-11-18T12:02:00Z');
  assert.deepEqual(projectTaskExecutionJob(check, w, s, checkId, p, o), check);
  assert.equal(taskExecutionCanLaunch(context, check, now), true);
  assert.equal(taskExecutionCanLaunch(context, check, Date.parse('2026-11-18T12:05:00Z')), false);
  for (const patch of [{ current: false }, { supported: false }, { selection: { ...selection, approvalValid: false } }, { selection: { ...selection, authorityCurrent: false } }]) assert.equal(taskExecutionCanLaunch({ ...context, ...patch }, check, now), false);
  for (const patch of [{ job: { ...checkedJob, state: 'running' } }, { binding: { ...binding, ownerSha256: hash('f') } }, { job: runJob }]) assert.equal(taskExecutionCanLaunch(context, { ...check, ...patch }, now), false);
  for (const patch of [{ episode_id: other }, { contract_revision_id: other }, { owner_principal_id: other }, { selected_models: [{ provider: 'openai', model: 'foreign', role: 'coding' }] }, { result: { operation_check: { ...checkedJob.result.operation_check, bindings: { ...checkedJob.result.operation_check.bindings, requestSha256: hash('f') } } } }]) assert.equal(projectTaskExecutionJob({ ...check, job: { ...checkedJob, ...patch } }, w, s, checkId, p, o), null);
  assert.equal(taskExecutionNeedsMonitoring({ ...run, job: { ...runJob, state: 'running' } }), true); assert.equal(taskExecutionNeedsMonitoring(run), false);
  const absent = clone(check); delete absent.job.result.operation_check.bindings.taskExecutionSha256;
  assert.equal(projectTaskExecutionJob(absent, w, s, checkId, p, o), null);
  absent.job.result.operation_check.eligible = false; absent.job.result.operation_check.checks = [{ code: 'task_selection', status: 'blocked' }];
  assert.ok(projectTaskExecutionJob(absent, w, s, checkId, p, o));
  absent.job.result.operation_check.bindings.taskExecutionSha256 = hash('f'); assert.equal(projectTaskExecutionJob(absent, w, s, checkId, p, o), null);
});

test('check and launch receipts join the exact reviewed request and launch cannot replace input or assertions', () => {
  const base = ['selections', s];
  assert.deepEqual(validateTaskExecutionMutation([...base, 'checks'], checkBody), checkBody);
  assert.deepEqual(projectTaskExecutionMutation(check, w, [...base, 'checks'], checkBody, p, o), check);
  assert.deepEqual(projectTaskExecutionMutation(run, w, [...base, 'runs'], runBody, p, o), run);
  assert.equal(validateTaskExecutionMutation([...base, 'runs'], { ...runBody, request: binding.request }), null);
  assert.equal(validateTaskExecutionMutation([...base, 'runs'], { ...runBody, acceptance }), null);
  assert.equal(validateTaskExecutionMutation([...base, 'runs'], { ...runBody, confirm: false }), null);
  assert.equal(projectTaskExecutionMutation(check, w, [...base, 'checks'], { ...checkBody, acceptance: { ...acceptance, assertions: [acceptance.assertions[0]] } }, p, o), null);
  assert.equal(projectTaskExecutionMutation(run, w, [...base, 'runs'], { ...runBody, checkJobId: other }, p, o), null);
  assert.equal(projectTaskExecutionMutation(run, w, [...base, 'runs'], { ...runBody, bindingSha256: hash('f') }, p, o), null);
});

test('lost positive binding permits only a scoped negative cancellation receipt', () => {
  const negative = { ...run, binding: null, job: { ...runJob, state: 'cancelling', cancellation_requested: true } }, path = ['selections', s, 'jobs', runId, 'cancel'];
  assert.deepEqual(projectTaskExecutionMutation(negative, w, path, { confirm: true }, p, o), negative);
  assert.equal(projectTaskExecutionJob(negative, w, s, runId, p, o), null);
  assert.equal(projectTaskExecutionMutation(negative, w, ['selections', s, 'runs'], runBody, p, o), null);
  assert.equal(projectTaskExecutionMutation({ ...negative, job: { ...negative.job, owner_principal_id: other } }, w, path, { confirm: true }, p, o), null);
  assert.equal(projectTaskExecutionMutation({ ...negative, job: { ...negative.job, episode_id: other } }, w, path, { confirm: true }, p, o), null);
  assert.equal(projectTaskExecutionMutation(negative, w, ['selections', s, 'jobs', other, 'cancel'], { confirm: true }, p, o), null);
});

test('job history preserves opaque cursors and rejects duplicated or cross-bound rows', () => {
  assert.deepEqual(projectTaskExecutionJobs(jobs, w, s, p, o), jobs);
  const paged = { ...jobs, nextCursor: 'eyJjcmVhdGVkX2F0IjoiZml4dHVyZSJ9', hasMore: true }; assert.deepEqual(projectTaskExecutionJobs(paged, w, s, p, o), paged);
  for (const patch of [{ items: [jobs.items[0], jobs.items[0]] }, { items: Array(26).fill(jobs.items[0]) }, { items: [{ ...jobs.items[0], binding: { ...binding, selectionId: other } }] }, { nextCursor: 'unsafe cursor', hasMore: true }, { nextCursor: 'x'.repeat(513), hasMore: true }, { nextCursor: 'opaque', hasMore: false }, { items: [], nextCursor: 'opaque', hasMore: true }]) assert.equal(projectTaskExecutionJobs({ ...jobs, ...patch }, w, s, p, o), null);
});

test('events and artifact chunks retain positive selection binding and exact job ownership', () => {
  assert.deepEqual(projectTaskExecutionEvents(events, w, s, runId, 0, p, o), events);
  assert.equal(projectTaskExecutionEvents({ ...events, binding: null }, w, s, runId, 0, p, o), null);
  assert.equal(projectTaskExecutionEvents(events, w, s, other, 0, p, o), null);
  assert.equal(projectTaskExecutionEvents({ ...events, items: [{ ...events.items[0], job_id: other }] }, w, s, runId, 0, p, o), null);
  const part = { ...run, artifact: { id: uuid('f'), sha256: hash('a'), bytes: 2, mediaType: 'application/json' }, offset: 0, dataBase64: btoa('{}'), nextOffset: null };
  assert.deepEqual(projectTaskExecutionArtifact(part, w, s, runId, 0, 2, p, o), part);
  assert.equal(projectTaskExecutionArtifact(part, w, s, other, 0, 2, p, o), null);
  assert.equal(projectTaskExecutionArtifact({ ...part, binding: null }, w, s, runId, 0, 2, p, o), null);
});

test('assertion verification stays separate from successful execution and preview content', () => {
  assert.deepEqual(projectTaskExecutionResult(result, w, s, runId, p, o), result);
  const failed = { ...result, verification: { ...result.verification, status: 'failed', checks: [{ index: 0, passed: false, reasonCode: 'equals_mismatch' }, result.verification.checks[1]], reasonCodes: ['assertions_failed'] } };
  assert.deepEqual(projectTaskExecutionResult(failed, w, s, runId, p, o), failed); assert.equal(failed.result.job.state, 'succeeded');
  const unavailable = { ...result, verification: { ...result.verification, status: 'unavailable', outputSha256: null, evidenceSha256: null, resultKind: null, checks: [], reasonCodes: ['complete_output_unavailable'] } };
  assert.deepEqual(projectTaskExecutionResult(unavailable, w, s, runId, p, o), unavailable);
  for (const patch of [{ bindingSha256: hash('1') }, { acceptanceSha256: hash('1') }, { outputSha256: null }, { evidenceSha256: null }, { resultKind: 'hosted_text' }, { checks: [] }, { checks: [{ index: 1, passed: true, reasonCode: null }] }, { checks: [{ index: 0, passed: false, reasonCode: 'equals_mismatch' }, result.verification.checks[1]] }]) assert.equal(projectTaskExecutionResult({ ...result, verification: { ...result.verification, ...patch } }, w, s, runId, p, o), null);
  assert.equal(projectTaskExecutionResult({ ...result, result: { ...result.result, job: { ...runJob, state: 'failed' } } }, w, s, runId, p, o), null);
  assert.equal(projectTaskExecutionResult({ ...result, binding: null }, w, s, runId, p, o), null);
  const previewChanged = { ...result, result: { ...result.result, output: 'untrusted alternate preview', truncated: true } }; assert.equal(projectTaskExecutionResult(previewChanged, w, s, runId, p, o).verification.status, 'passed');
});

test('discovery handoff and resume URLs bind one exact workspace, operation, selection and optional job', () => {
  const href = taskHandoffHref(w, selection); assert.equal(href, `/tools?workspace=${w}&operation=${o}&selection=${s}`);
  assert.deepEqual(selectedExecutionQuery(href.split('?')[1] + '&job=' + runId, w, o), { selection: s, job: runId, invalid: false });
  assert.deepEqual(selectedExecutionQuery(`?workspace=${w}&operation=${o}`, w, o), { selection: null, job: null, invalid: false });
  for (const query of [`?workspace=${w}&operation=${o}&selection=${s}&selection=${s}`, `?workspace=${w}&operation=${o}&job=${runId}`, `?workspace=${other}&operation=${o}&selection=${s}`, `?workspace=${w}&operation=${o}&selection=${s}&job=latest`, `?workspace=${w}&operation=${o}&operation=${o}&selection=${s}`]) assert.equal(selectedExecutionQuery(query, w, o).invalid, true);
  assert.equal(taskExecutionScopeMatches(scope, { ...scope, taskBinding: { ...scope.taskBinding, contractRevisionId: other } }), false);
  assert.equal(taskExecutionScopeInvalidated(403, null, w, p), true); assert.equal(taskExecutionScopeInvalidated(200, { ...scope, principalId: other }, w, p), true); assert.equal(taskExecutionScopeInvalidated(409, null, w, p), false);
});
