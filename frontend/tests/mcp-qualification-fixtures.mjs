// Synthetic unit data. The separate HTTP fixture is exported by the PostgreSQL lifecycle test.
export const workspaceId = '11111111-1111-4111-8111-111111111111', principalId = '22222222-2222-4222-8222-222222222222';
export const agentId = '33333333-3333-4333-8333-333333333333', bindingId = '44444444-4444-4444-8444-444444444444';
export const suiteId = '55555555-5555-4555-8555-555555555555', reportId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', other = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';
export const scope = { schema: 'potbot-mcp-qualification/v1', workspaceId, principalId };
export const hash = digit => digit.repeat(64), clone = value => structuredClone(value);
const uuid = digit => `${digit.repeat(8)}-${digit.repeat(4)}-4${digit.repeat(3)}-8${digit.repeat(3)}-${digit.repeat(12)}`;
export const taskBinding = { episodeId: uuid('6'), contractRevisionId: uuid('7'), contractSha256: hash('a'), repositoryStateId: uuid('8'), repositorySha256: hash('b'), semanticReleaseId: uuid('9'), semanticReleaseSha256: hash('c') };
export const cases = [
  { id: 'structured_case', title: 'Inspect task shape', toolName: 'inspect', caseSha256: hash('1'), argumentsSha256: hash('2'), oracleSha256: hash('3'), assertionCount: 2 },
  { id: 'text_case', title: 'Inspect task summary', toolName: 'inspect', caseSha256: hash('4'), argumentsSha256: hash('5'), oracleSha256: hash('6'), assertionCount: 1 },
];
export const suite = { id: suiteId, bindingId, name: 'Admitted task cases', description: 'Check retained task results.\nPrivate inputs stay on the host.', agentId, toolName: 'inspect', transport: 'stdio', revision: 3, state: 'enabled',
  bindingSha256: hash('a'), approvalValid: true, suiteSha256: hash('b'), suiteFileSha256: hash('c'), configurationSha256: hash('d'), installationSha256: hash('e'), consumerSha256: hash('f'), taskBinding, cases,
  activeAttemptId: null, lastAttemptId: null, lastReportId: null, authorityCurrent: true, reasonCodes: [], actions: ['disable', 'qualify', 'revoke'], createdAt: '2026-11-18T12:00:00+00:00', updatedAt: '2026-11-18T12:01:00+00:00' };
const pendingCase = value => ({ id: value.id, title: value.title, caseSha256: value.caseSha256, status: 'pending', actionId: null, candidateId: null, reservationId: null, runId: null, resultArtifactId: null,
  resultSha256: null, receiptSha256: null, oracleSha256: value.oracleSha256, checks: null, elapsedMs: null, reasonCode: null });
export const pendingCases = cases.map(pendingCase);
export const preparedCase = { ...pendingCases[0], status: 'prepared', actionId: uuid('a'), candidateId: uuid('b'), reservationId: uuid('c') };
export const passedCases = pendingCases.map((value, index) => ({ ...value, status: 'passed', actionId: uuid(index ? '4' : 'a'), candidateId: uuid(index ? '5' : 'b'), reservationId: uuid(index ? '6' : 'c'),
  runId: uuid(index ? '7' : 'd'), resultArtifactId: uuid(index ? '8' : 'e'), resultSha256: hash(index ? '8' : '7'), receiptSha256: hash(index ? '9' : '8'), elapsedMs: 20 + index,
  checks: Array.from({ length: cases[index].assertionCount }, (_, index) => ({ index, passed: true, reasonCode: null })) }));
export const failedCase = { ...passedCases[0], status: 'failed', reasonCode: 'result_check_failed', checks: [{ index: 0, passed: true, reasonCode: null }, { index: 1, passed: false, reasonCode: 'value_mismatch' }] };
export const notRunCase = { ...pendingCases[1], status: 'not_run', reasonCode: 'case_not_run' };
export const requested = { id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', suiteId, bindingId, agentId, revision: 1, bindingSha256: hash('1'), status: 'requested', requestedSuiteSha256: suite.bindingSha256,
  suiteSha256: suite.suiteSha256, configurationSha256: suite.configurationSha256, caseCount: 2, completedCases: 0, passedCases: 0, failedCases: 0, cases: pendingCases, reportId: null, reportSha256: null,
  reasonCode: null, actions: ['cancel'], recoveryAvailable: false, createdAt: '2026-11-18T12:02:00+00:00', updatedAt: '2026-11-18T12:02:00+00:00' };
export const running = { ...requested, revision: 2, bindingSha256: hash('2'), status: 'running', cases: [preparedCase, pendingCases[1]], recoveryAvailable: true };
export const succeeded = { ...requested, revision: 3, bindingSha256: hash('3'), status: 'succeeded', completedCases: 2, passedCases: 2, cases: passedCases, reportId, reportSha256: hash('9'), actions: [] };
export const failed = { ...succeeded, status: 'failed', passedCases: 1, failedCases: 1, cases: [failedCase, passedCases[1]], reasonCode: 'qualification_failed' };
export const partial = { ...failed, completedCases: 1, passedCases: 1, failedCases: 0, cases: [passedCases[0], notRunCase] };
export const cancelled = { ...requested, revision: 2, bindingSha256: hash('2'), status: 'cancelled', reasonCode: 'qualification_cancelled', actions: [], cases: pendingCases.map(value => ({ ...value, status: 'not_run', reasonCode: 'case_not_run' })) };
export const disabled = { ...suite, revision: 4, state: 'disabled', bindingSha256: hash('8'), authorityCurrent: false, reasonCodes: ['suite_disabled'], actions: ['enable', 'revoke'] };
export const enabled = { ...suite, revision: 5, bindingSha256: hash('9') };
export const revoked = { ...disabled, revision: 6, state: 'revoked', bindingSha256: hash('0'), reasonCodes: ['suite_revoked'], actions: [] };
export const evidence = { schema: 'potbot-mcp-qualification-report/v1', workspaceId, principalId, attemptId: requested.id, agentId,
  suiteSha256: suite.suiteSha256, suiteFileSha256: suite.suiteFileSha256, consumerSha256: suite.consumerSha256, configurationSha256: suite.configurationSha256, installationSha256: suite.installationSha256,
  definitionSha256: hash('1'), runtimeSha256: hash('2'), imageSha256: hash('3'), qualificationSha256: hash('4'), agentHomeRevision: 2, storageSha256: hash('5'), referenceSha256: hash('6'), taskBinding,
  observedAt: '2026-11-18T12:02:01+00:00', completedAt: '2026-11-18T12:02:02+00:00', casesSha256: hash('7') };
export const report = { id: reportId, suiteId, attemptId: requested.id, bindingId, sha256: succeeded.reportSha256, approvalValid: true, complete: true, passed: true, authorityCurrent: true, reasonCodes: [], evidence, cases: passedCases, recordedAt: '2026-11-18T12:02:03+00:00' };
export const failedReport = { ...report, passed: false, cases: failed.cases }, partialReport = { ...report, complete: false, passed: false, cases: partial.cases };
export const page = items => ({ ...scope, items, nextCursor: null });
export const decision = { revision: suite.revision, digest: suite.bindingSha256, confirm: true };
export const wire = { suites: page([suite]), suite: { ...scope, suite }, emptyAttempts: page([]), attempts: page([succeeded]),
  requestedAttempt: { ...scope, attempt: requested }, runningAttempt: { ...scope, attempt: running }, succeededAttempt: { ...scope, attempt: succeeded }, failedAttempt: { ...scope, attempt: failed }, cancelledAttempt: { ...scope, attempt: cancelled },
  disabledSuite: { ...scope, suite: disabled }, enabledSuite: { ...scope, suite: enabled }, revokedSuite: { ...scope, suite: revoked }, report: { ...scope, report }, failedReport: { ...scope, report: failedReport }, partialReport: { ...scope, report: partialReport } };
