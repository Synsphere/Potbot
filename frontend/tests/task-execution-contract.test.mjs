import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { parseMcpJSON, projectTaskExecutionContext, projectTaskExecutionJob, projectTaskExecutionJobs, projectTaskExecutionEvents, projectTaskExecutionResult, projectTaskExecutionMutation } from '../src/lib/task-execution.ts';
import { taskExecutionCanLaunch, taskExecutionScopeMatches } from '../src/lib/task-execution-client.ts';

test('actual PostgreSQL HTTP selected-task lifecycle preserves check binding, independent verification and recovery', async () => {
  // Exported by test_actual_http_lifecycle_exports_frontend_contract_fixture.
  // PostgreSQL, HTTP, queue, worker and receipt validation are real; the provider
  // transport is explicitly synthetic and does not qualify a live provider.
  const path = process.env.POTBOT_TASK_EXECUTION_HTTP_FIXTURE ?? new URL('./fixtures/task-execution-http.json', import.meta.url);
  const wire = parseMcpJSON(await readFile(path, 'utf8')), { workspaceId: w, principalId: p, selectionId: s, operationId: o } = wire;
  assert.match(wire.description, /synthetic provider/);
  const context = projectTaskExecutionContext(wire.context, w, s, p, o); assert.ok(context); assert.equal(context.supported, true); assert.equal(context.current, true);
  const check = projectTaskExecutionJob(wire.check, w, s, wire.check.job.id, p, o), checked = projectTaskExecutionJob(wire.checkedJob, w, s, wire.check.job.id, p, o), run = projectTaskExecutionJob(wire.launch, w, s, wire.launch.job.id, p, o);
  assert.ok(check); assert.ok(checked); assert.ok(run);
  assert.equal(check.job.state, 'queued'); assert.equal(checked.job.state, 'succeeded'); assert.equal(run.job.operation_check_id, checked.job.id);
  assert.deepEqual(check.binding, checked.binding); assert.deepEqual(run.binding, checked.binding); assert.deepEqual(check.binding, wire.check.binding);
  assert.equal(check.binding.selectionSha256, context.selectionSha256); assert.match(check.binding.consumerSha256, /^[a-f0-9]{64}$/);
  assert.equal(taskExecutionCanLaunch(context, checked, Date.parse(checked.job.result.operation_check.checkedAt) + 1), true);
  const base = ['selections', s];
  assert.deepEqual(projectTaskExecutionMutation(wire.check, w, [...base, 'checks'], { selectionSha256: context.selectionSha256, request: check.binding.request, acceptance: check.binding.acceptance, confirm: true }, p, o), check);
  assert.deepEqual(projectTaskExecutionMutation(wire.launch, w, [...base, 'runs'], { checkJobId: check.job.id, bindingSha256: check.binding.sha256, confirm: true }, p, o), run);
  const pending = projectTaskExecutionResult(wire.pending, w, s, run.job.id, p, o), result = projectTaskExecutionResult(wire.result, w, s, run.job.id, p, o);
  assert.ok(pending); assert.ok(result); assert.equal(pending.verification.status, 'pending'); assert.equal(pending.verification.checks.length, 0);
  assert.equal(result.result.job.state, 'succeeded'); assert.equal(result.verification.status, 'passed'); assert.equal(result.verification.resultKind, 'hosted_text');
  assert.equal(result.verification.checks.length, check.binding.acceptance.assertions.length); assert.ok(result.verification.checks.every(item => item.passed));
  assert.deepEqual(result.verification, wire.result.verification); assert.match(result.verification.evidenceSha256, /^[a-f0-9]{64}$/); assert.match(result.verification.outputSha256, /^[a-f0-9]{64}$/);
  const events = projectTaskExecutionEvents(wire.events, w, s, run.job.id, 0, p, o), jobs = projectTaskExecutionJobs(wire.jobs, w, s, p, o);
  assert.ok(events); assert.ok(jobs); assert.ok(events.items.some(item => item.event_type === 'job.output')); assert.ok(jobs.items.some(item => item.job.id === run.job.id));
  assert.deepEqual(events.binding, check.binding); assert.equal(events.job.id, run.job.id); assert.equal(taskExecutionScopeMatches(context, result), true);
  assert.doesNotMatch(JSON.stringify({ context, check, checked, run, pending, result, events, jobs }), /setup_token|operation_authorization|authorization_digest|claim_token/);
});
