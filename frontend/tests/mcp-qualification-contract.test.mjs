import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { parseMcpJSON, projectQualificationSuites, projectQualificationSuiteEnvelope, projectQualificationAttempts, projectQualificationAttemptEnvelope, projectQualificationReportEnvelope, projectQualificationMutation } from '../src/lib/mcp-qualification.ts';
import { qualificationAttemptMatchesSuite, qualificationReportMatchesSuite, qualificationReportMatchesAttempt, qualificationReportCurrent } from '../src/lib/mcp-qualification-client.ts';

test('actual HTTP and PostgreSQL qualification lifecycle agrees with browser projections and exact receipts', async () => {
  // These are real service/API/database responses. Native runtime and provider
  // outcomes use the integration harness; this fixture does not qualify a vendor.
  const path = process.env.POTBOT_MCP_QUALIFICATION_CONTRACT_OUTPUT ?? new URL('./fixtures/mcp-qualification-http.json', import.meta.url);
  const wire = parseMcpJSON(await readFile(path, 'utf8')), { workspaceId: w, principalId: p } = wire.suites, suite = wire.suite.suite, suiteId = suite.id;
  assert.deepEqual(projectQualificationSuites(wire.suites, w, p), wire.suites);
  for (const key of ['suite', 'freshSuite', 'disabledSuite', 'enabledSuite', 'revokedSuite']) assert.deepEqual(projectQualificationSuiteEnvelope(wire[key], w, suiteId, p), wire[key], key);
  for (const key of ['emptyAttempts', 'attempts']) assert.deepEqual(projectQualificationAttempts(wire[key], w, p, suiteId), wire[key], key);
  for (const key of ['requestedAttempt', 'runningAttempt', 'succeededAttempt', 'failedAttempt', 'cancelledAttempt', 'untrustedAttempt']) {
    assert.deepEqual(projectQualificationAttemptEnvelope(wire[key], w, wire[key].attempt.id, p), wire[key], key);
    assert.equal(qualificationAttemptMatchesSuite(wire[key].attempt, suite), true, key);
  }
  for (const key of ['report', 'failedReport']) {
    assert.deepEqual(projectQualificationReportEnvelope(wire[key], w, wire[key].report.id, p), wire[key], key);
    assert.equal(qualificationReportMatchesSuite(wire[key].report, suite), true, key);
  }
  for (const [key, group, action, recordKey] of [['requestedAttempt', 'suites', 'qualify', 'attempt'], ['disabledSuite', 'suites', 'disable', 'suite'], ['enabledSuite', 'suites', 'enable', 'suite'], ['revokedSuite', 'suites', 'revoke', 'suite'], ['cancelledAttempt', 'attempts', 'cancel', 'attempt']]) {
    const identity = group === 'suites' ? suiteId : wire[key][recordKey].id;
    assert.deepEqual(projectQualificationMutation(wire[key], w, [group, identity, action], wire.requests[action], p), wire[key], key);
  }
  assert.equal(projectQualificationMutation(wire.runningAttempt, w, ['suites', suiteId, 'qualify'], wire.requests.qualify, p), null);
  assert.equal(projectQualificationMutation(wire.succeededAttempt, w, ['suites', suiteId, 'qualify'], wire.requests.qualify, p), null);
  assert.equal(wire.emptyAttempts.items.length, 0);
  assert.equal(wire.requestedAttempt.attempt.status, 'requested'); assert.equal(wire.runningAttempt.attempt.status, 'running');
  assert.equal(wire.succeededAttempt.attempt.status, 'succeeded'); assert.equal(wire.failedAttempt.attempt.status, 'failed'); assert.equal(wire.cancelledAttempt.attempt.status, 'cancelled');
  const untrusted = wire.untrustedAttempt.attempt;
  assert.equal(untrusted.status, 'indeterminate'); assert.equal(untrusted.reasonCode, 'admission_invalid');
  assert.equal(untrusted.completedCases, 0); assert.equal(untrusted.passedCases, 0); assert.equal(untrusted.failedCases, 0);
  assert.equal(untrusted.reportId, null); assert.equal(untrusted.reportSha256, null); assert.equal(untrusted.recoveryAvailable, false);
  assert.ok(untrusted.cases.every(value => value.status === 'indeterminate' && value.checks === null && value.runId === null && value.resultArtifactId === null));
  assert.equal(wire.report.report.complete, true); assert.equal(wire.report.report.passed, true);
  assert.equal(wire.failedReport.report.complete, true); assert.equal(wire.failedReport.report.passed, false);
  assert.ok(wire.failedReport.report.cases.some(value => value.checks?.some(check => !check.passed)));
  assert.equal(qualificationReportMatchesAttempt(wire.report.report, wire.succeededAttempt.attempt, suite), true);
  assert.equal(qualificationReportMatchesAttempt(wire.failedReport.report, wire.failedAttempt.attempt, suite), true);
  assert.equal(qualificationReportCurrent(wire.report.report, wire.freshSuite.suite), true);
  assert.equal(qualificationReportCurrent(wire.report.report, wire.disabledSuite.suite), false);
  assert.equal(qualificationReportCurrent(wire.report.report, wire.revokedSuite.suite), false);
  assert.deepEqual(wire.revokedSuite.suite.actions, []);
  if (wire.staleSuite) {
    assert.deepEqual(projectQualificationSuiteEnvelope(wire.staleSuite, w, suiteId, p), wire.staleSuite);
    assert.equal(wire.staleSuite.suite.authorityCurrent, false); assert.equal(qualificationReportCurrent(wire.report.report, wire.staleSuite.suite), false);
  }
  if (wire.staleReport) {
    assert.deepEqual(projectQualificationReportEnvelope(wire.staleReport, w, wire.staleReport.report.id, p), wire.staleReport);
    assert.equal(wire.staleReport.report.authorityCurrent, false); assert.equal(qualificationReportMatchesSuite(wire.staleReport.report, suite), true);
  }
});
