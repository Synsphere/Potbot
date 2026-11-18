import test from 'node:test';
import assert from 'node:assert/strict';
import { projectQualificationTaskBinding, projectQualificationCase, projectQualificationCaseResult, projectQualificationCheck, projectQualificationSuite, projectQualificationAttempt, projectQualificationEvidence, projectQualificationReport,
  projectQualificationSuites, projectQualificationAttempts, projectQualificationSuiteEnvelope, projectQualificationAttemptEnvelope, projectQualificationReportEnvelope, validateQualificationMutation, projectQualificationMutation } from '../src/lib/mcp-qualification.ts';
import { workspaceId as w, principalId as p, suiteId, bindingId, reportId, other, hash, clone, taskBinding, suite, cases, preparedCase, passedCases, failedCase, pendingCases, requested, running, succeeded, failed, partial, disabled, enabled, revoked, cancelled, evidence, report, failedReport, partialReport, wire, page, decision } from './mcp-qualification-fixtures.mjs';

test('qualification projections retain only safe public identities and exact scoped envelopes', () => {
  assert.deepEqual(projectQualificationSuite({ ...suite, argv: ['PRIVATE'], endpoint: 'PRIVATE', nativePath: 'PRIVATE', signature: 'PRIVATE' }), suite);
  assert.deepEqual(projectQualificationSuites({ ...wire.suites, setupToken: 'PRIVATE' }, w, p), wire.suites);
  assert.deepEqual(projectQualificationSuiteEnvelope(wire.suite, w, suiteId, p), wire.suite);
  assert.deepEqual(projectQualificationAttempt({ ...requested, workerToken: 'PRIVATE', claim: {} }), requested);
  assert.deepEqual(projectQualificationReport({ ...report, signature: 'PRIVATE' }), report);
  assert.deepEqual(projectQualificationReportEnvelope(wire.report, w, reportId, p), wire.report);
  for (const patch of [{ workspaceId: other }, { principalId: other }, { schema: 'next-version' }]) {
    assert.equal(projectQualificationSuites({ ...wire.suites, ...patch }, w, p), null);
    assert.equal(projectQualificationReportEnvelope({ ...wire.report, ...patch }, w, reportId, p), null);
  }
  assert.equal(projectQualificationSuiteEnvelope(wire.suite, w, other, p), null);
  assert.equal(projectQualificationAttemptEnvelope(wire.requestedAttempt, w, other, p), null);
  assert.equal(projectQualificationReportEnvelope(wire.report, w, other, p), null);
});

test('task and case metadata use exact bounded schemas without private arguments or oracle values', () => {
  assert.deepEqual(projectQualificationTaskBinding(taskBinding), taskBinding);
  for (const key of Object.keys(taskBinding)) assert.equal(projectQualificationTaskBinding({ ...taskBinding, [key]: 'not-an-identity' }), null, key);
  for (const patch of [{ nativePath: '/PRIVATE' }, { contractSha256: hash('A') }, { episodeId: 1 }]) assert.equal(projectQualificationTaskBinding({ ...taskBinding, ...patch }), null);
  for (const value of cases) assert.deepEqual(projectQualificationCase(value), value);
  for (const patch of [{ id: 'Upper' }, { id: 'x'.repeat(65) }, { title: 'x'.repeat(129) }, { toolName: 'a\nb' }, { assertionCount: 0 }, { assertionCount: 17 }, { assertionCount: true }, { arguments: { secret: 1 } }, { oracle: { expected: 'PRIVATE' } }]) assert.equal(projectQualificationCase({ ...cases[0], ...patch }), null);
  for (const patch of [{ cases: [] }, { cases: Array(9).fill(cases[0]) }, { cases: [cases[0], cases[0]] }, { cases: [cases[0], { ...cases[1], toolName: 'different' }] }, { name: 'x'.repeat(97) }, { description: 'x'.repeat(2049) }]) assert.equal(projectQualificationSuite({ ...suite, ...patch }), null);
  assert.ok(projectQualificationSuite({ ...suite, description: '<script>untrusted provider text</script>\nhttps://untrusted.example' }));
});

test('suite actions, signature, current authority and lifecycle state must agree', () => {
  for (const value of [suite, disabled, enabled, revoked]) assert.deepEqual(projectQualificationSuite(value), value);
  for (const patch of [{ id: 'not-uuid' }, { revision: 0 }, { revision: Number.MAX_SAFE_INTEGER + 1 }, { transport: 'websocket' }, { state: 'revoked' }, { state: 'disabled' }, { approvalValid: false },
    { authorityCurrent: false }, { reasonCodes: ['discovery_not_current'] }, { activeAttemptId: other }, { actions: ['qualify', 'qualify'] }, { actions: ['execute'] }, { reasonCodes: ['PRIVATE /path'] },
    { updatedAt: '2026-11-17T12:00:00Z' }, { createdAt: 'today' }, { suiteSha256: hash('A') }]) assert.equal(projectQualificationSuite({ ...suite, ...patch }), null, JSON.stringify(patch));
  assert.ok(projectQualificationSuite({ ...suite, approvalValid: false, authorityCurrent: false, reasonCodes: ['admission_invalid'], actions: ['revoke'] }));
  assert.ok(projectQualificationSuite({ ...suite, authorityCurrent: false, reasonCodes: ['task_binding_changed'], actions: ['disable', 'revoke'] }));
});

test('case states require real finished identities, safe assertion outcomes and ordered bounded checks', () => {
  for (const value of [...pendingCases, preparedCase, ...passedCases, failedCase]) assert.deepEqual(projectQualificationCaseResult(value), value);
  assert.ok(projectQualificationCaseResult({ ...failedCase, checks: null, reasonCode: 'tool_result_error' }));
  for (const field of ['actionId', 'candidateId', 'reservationId', 'runId', 'resultArtifactId', 'resultSha256', 'receiptSha256', 'elapsedMs']) assert.equal(projectQualificationCaseResult({ ...passedCases[0], [field]: null }), null, field);
  for (const patch of [{ checks: null }, { checks: [] }, { checks: Array(17).fill(passedCases[0].checks[0]) }, { checks: [{ index: 1, passed: true, reasonCode: null }] },
    { checks: [{ index: 0, passed: false, reasonCode: 'value_mismatch' }] }, { elapsedMs: -1 }, { elapsedMs: 0.5 }, { reasonCode: 'PRIVATE /path' }, { actualValue: 'PRIVATE' }]) assert.equal(projectQualificationCaseResult({ ...passedCases[0], ...patch }), null, JSON.stringify(patch));
  assert.equal(projectQualificationCaseResult({ ...preparedCase, reservationId: null }), null);
  for (const status of ['pending', 'prepared', 'not_run', 'indeterminate']) assert.equal(projectQualificationCaseResult({ ...passedCases[0], status }), null, status);
  assert.equal(projectQualificationCaseResult({ ...pendingCases[0], actionId: other }), null);
  assert.equal(projectQualificationCaseResult({ ...failedCase, reasonCode: null }), null);
  assert.equal(projectQualificationCaseResult({ ...failedCase, checks: passedCases[0].checks }), null);
  for (const reasonCode of ['pointer_missing', 'value_mismatch', 'value_type_mismatch', 'text_not_found', 'length_mismatch']) assert.deepEqual(projectQualificationCheck({ index: 0, passed: false, reasonCode }), { index: 0, passed: false, reasonCode });
  for (const value of [{ index: 0, passed: true, reasonCode: 'value_mismatch' }, { index: 0, passed: false, reasonCode: null }, { index: 0, passed: false, reasonCode: 'PRIVATE' }, { index: 0, passed: true, reasonCode: null, pointer: '/PRIVATE' }]) assert.equal(projectQualificationCheck(value), null);
});

test('attempt counters and terminal evidence cannot invent completion or browser recovery authority', () => {
  for (const value of [requested, running, succeeded, failed, partial, cancelled]) assert.deepEqual(projectQualificationAttempt(value), value);
  for (const patch of [{ status: 'queued' }, { status: 'succeeded' }, { caseCount: 0 }, { caseCount: 3 }, { caseCount: 9 }, { completedCases: 1 }, { passedCases: 1 }, { failedCases: 1 }, { reportId }, { actions: ['execute'] }, { actions: ['recover'] }, { recoveryAvailable: 'yes' }]) assert.equal(projectQualificationAttempt({ ...requested, ...patch }), null, JSON.stringify(patch));
  assert.equal(projectQualificationAttempt({ ...succeeded, actions: ['cancel'] }), null);
  assert.equal(projectQualificationAttempt({ ...succeeded, reportId: null, reportSha256: null }), null);
  assert.equal(projectQualificationAttempt({ ...succeeded, completedCases: 1 }), null);
  assert.ok(projectQualificationAttempt({ ...running, status: 'recovering' }));
  assert.ok(projectQualificationAttempt({ ...running, status: 'indeterminate', actions: [], cases: [{ ...preparedCase, status: 'indeterminate', reasonCode: 'case_outcome_unknown' }, { ...pendingCases[1], status: 'not_run', reasonCode: 'case_not_run' }] }));
});

test('native report identities, time order and completeness are exact while historical evidence stays inspectable', () => {
  assert.deepEqual(projectQualificationEvidence(evidence), evidence);
  for (const patch of [{ workspaceId: 'private' }, { schema: 'next-version' }, { agentHomeRevision: 0 }, { completedAt: '2026-11-18T12:02:00Z' }, { referenceSha256: hash('A') }, { runtimePath: '/PRIVATE' }, { caseValues: [] }]) assert.equal(projectQualificationEvidence({ ...evidence, ...patch }), null, JSON.stringify(patch));
  for (const value of [report, failedReport, partialReport]) assert.deepEqual(projectQualificationReport(value), value);
  assert.ok(projectQualificationReport({ ...report, authorityCurrent: false, reasonCodes: ['task_binding_changed', 'report_superseded'] }));
  assert.ok(projectQualificationReport({ ...report, approvalValid: false, authorityCurrent: false, reasonCodes: ['report_admission_invalid'] }));
  for (const patch of [{ passed: false }, { complete: false }, { attemptId: other }, { authorityCurrent: false }, { approvalValid: false }, { cases: pendingCases }]) assert.equal(projectQualificationReport({ ...report, ...patch }), null);
  assert.equal(projectQualificationReport({ ...partialReport, complete: true }), null);
  assert.equal(projectQualificationReport({ ...failedReport, passed: true }), null);
  for (const key of ['workspaceId', 'principalId']) assert.equal(projectQualificationReportEnvelope({ ...wire.report, report: { ...report, evidence: { ...evidence, [key]: other } } }, w, reportId, p), null);
});

test('invalid attempt admission preserves containment while withdrawing all execution and success evidence', () => {
  const redacted = { ...requested, reasonCode: 'admission_invalid', cases: pendingCases.map(value => ({ ...value, status: 'indeterminate', reasonCode: 'admission_invalid' })) };
  for (const status of ['requested', 'running', 'recovering']) assert.deepEqual(projectQualificationAttempt({ ...redacted, status }), { ...redacted, status });
  const terminal = { ...redacted, status: 'indeterminate', actions: [] };
  assert.deepEqual(projectQualificationAttempt(terminal), terminal);
  assert.equal(projectQualificationAttempt({ ...terminal, passedCases: 2, completedCases: 2 }), null);
  assert.equal(projectQualificationAttempt({ ...terminal, status: 'succeeded' }), null);
});

test('qualification pages reject mixed owners, unsorted records and ambiguous nonadvancing cursors', () => {
  assert.deepEqual(projectQualificationAttempts(wire.attempts, w, p, suiteId), wire.attempts);
  assert.deepEqual(projectQualificationSuites(wire.suites, w, p, bindingId), wire.suites);
  assert.equal(projectQualificationAttempts(wire.attempts, w, p, other), null);
  assert.equal(projectQualificationSuites(wire.suites, w, p, other), null);
  for (const items of [[suite, suite], [{ ...suite, id: other }, suite], Array(51).fill(suite)]) assert.equal(projectQualificationSuites(page(items), w, p), null);
  for (const nextCursor of ['bad', other, other.toUpperCase()]) assert.equal(projectQualificationSuites({ ...wire.suites, nextCursor }, w, p), null);
  assert.equal(projectQualificationSuites({ ...page([]), nextCursor: suiteId }, w, p), null);
  assert.deepEqual(projectQualificationSuites({ ...wire.suites, nextCursor: suiteId }, w, p), { ...wire.suites, nextCursor: suiteId });
});

test('exact reviewed decisions accept only original requested and revision-plus-one lifecycle receipts', () => {
  for (const action of ['enable', 'disable', 'revoke', 'qualify']) assert.deepEqual(validateQualificationMutation(['suites', suiteId, action], decision), decision);
  assert.deepEqual(validateQualificationMutation(['attempts', requested.id, 'cancel'], decision), decision);
  for (const patch of [{ revision: 0 }, { revision: Number.MAX_SAFE_INTEGER }, { confirm: false }, { digest: 'latest' }, { arguments: {} }, { report }, { endpoint: 'PRIVATE' }]) assert.equal(validateQualificationMutation(['suites', suiteId, 'qualify'], { ...decision, ...patch }), null);
  for (const path of [['suites', suiteId, 'admit'], ['suites', suiteId, 'execute'], ['attempts', requested.id, 'run'], ['attempts', requested.id, 'recover'], ['reports', reportId], ['suites', suiteId, 'qualify', 'extra']]) assert.equal(validateQualificationMutation(path, decision), null);
  assert.deepEqual(projectQualificationMutation(wire.requestedAttempt, w, ['suites', suiteId, 'qualify'], decision, p), wire.requestedAttempt);
  for (const value of [wire.runningAttempt, wire.succeededAttempt]) assert.equal(projectQualificationMutation(value, w, ['suites', suiteId, 'qualify'], decision, p), null);
  assert.equal(projectQualificationMutation(wire.requestedAttempt, w, ['suites', suiteId, 'qualify'], { ...decision, digest: hash('0') }, p), null);
  for (const [key, group, action, recordKey] of [['disabledSuite', 'suites', 'disable', 'suite'], ['enabledSuite', 'suites', 'enable', 'suite'], ['revokedSuite', 'suites', 'revoke', 'suite'], ['cancelledAttempt', 'attempts', 'cancel', 'attempt']]) {
    const value = wire[key][recordKey], body = { ...decision, revision: value.revision - 1 }, path = [group, value.id, action];
    assert.deepEqual(projectQualificationMutation(wire[key], w, path, body, p), wire[key]);
    assert.equal(projectQualificationMutation({ ...wire[key], [recordKey]: { ...value, revision: value.revision + 1 } }, w, path, body, p), null);
  }
});
