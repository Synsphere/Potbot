import test from 'node:test';
import assert from 'node:assert/strict';
import { mcpSuiteSelection, mcpQualificationBindingSelection, qualificationScopeInvalidated, qualificationCommandRoute, newQualificationCommand, validateQualificationCommand, qualificationJournalScope,
  qualificationAttemptMatchesSuite, qualificationReportMatchesSuite, qualificationReportMatchesAttempt, qualificationAttemptNeedsMonitoring, qualificationReportCurrent, MCP_QUALIFICATION_ENDPOINT, QUALIFICATION_MAX_POLLS } from '../src/lib/mcp-qualification-client.ts';
import { WorkshopRequests, retainAttempt, recoverAttempt, clearAttempt, clearRejectedAttempt, workshopJournalScope } from '../src/lib/skill-workshop-client.ts';
import { workspaceId as w, principalId as p, suiteId, bindingId, other, scope, suite, requested, succeeded, report, evidence, decision, hash } from './mcp-qualification-fixtures.mjs';
const path = `${MCP_QUALIFICATION_ENDPOINT}/suites/${suiteId}/qualify`;
function storage() { const map = new Map(); return { read: async key => map.get(key) ?? null, write: async (key, value) => map.set(key, value), remove: async key => map.delete(key) }; }

test('qualification deep links require one exact matching workspace and canonical suite or binding UUID', () => {
  assert.equal(mcpSuiteSelection(`?workspace=${w}&suite=${suiteId}&binding=${bindingId}`, w), suiteId);
  assert.equal(mcpQualificationBindingSelection(`?workspace=${w}&suite=${suiteId}&binding=${bindingId}`, w), bindingId);
  for (const [key, value, select] of [['suite', suiteId, mcpSuiteSelection], ['binding', bindingId, mcpQualificationBindingSelection]]) {
    for (const search of [`?${key}=${value}`, `?workspace=${other}&${key}=${value}`, `?workspace=${w}&workspace=${w}&${key}=${value}`, `?workspace=${w}&${key}=${value}&${key}=${other}`, `?workspace=${w}&${key}=latest`, `?workspace=${w}&${key}=${other.toUpperCase()}`]) assert.equal(select(search, w), null, search);
  }
});

test('changed read or mutation scope hides retained private qualification evidence', () => {
  for (const status of [401, 403]) assert.equal(qualificationScopeInvalidated(status, null, w, p), true);
  for (const status of [200, 202]) {
    assert.equal(qualificationScopeInvalidated(status, { ...scope, principalId: other }, w, p), true);
    assert.equal(qualificationScopeInvalidated(status, { ...scope, workspaceId: other }, w, p), true);
    assert.equal(qualificationScopeInvalidated(status, scope, w, p), false);
  }
  assert.equal(qualificationScopeInvalidated(409, null, w, p), false);
  assert.equal(qualificationScopeInvalidated(200, { ...scope, schema: 'untrusted' }, w, p), false);
});

test('journal validation retains exact reviewed bytes and rejects execution, admission or report upload routes', () => {
  const command = newQualificationCommand(path, decision); assert.deepEqual(validateQualificationCommand(command), command);
  assert.deepEqual(qualificationCommandRoute(path), ['suites', suiteId, 'qualify']);
  const padded = { ...command, body: ` { "revision": ${decision.revision}, "digest":"${decision.digest}", "confirm": true } ` }; assert.deepEqual(validateQualificationCommand(padded), padded);
  for (const candidate of [path + '?url=private', path + '/extra', `https://evil.example${path}`, `${MCP_QUALIFICATION_ENDPOINT}/attempts/${requested.id}/recover`, `${MCP_QUALIFICATION_ENDPOINT}/attempts/${requested.id}/run`, `${MCP_QUALIFICATION_ENDPOINT}/suites/${suiteId}/admit`, `${MCP_QUALIFICATION_ENDPOINT}/reports/${report.id}`]) assert.equal(validateQualificationCommand({ ...command, path: candidate }), null);
  for (const patch of [{ key: 'unsafe key' }, { key: ':invalid' }, { key: '.invalid' }, { body: 'x'.repeat(8193) }, { body: '{"revision":1,"revision":2}' }, { body: JSON.stringify({ ...decision, arguments: {} }) }, { receipt: {} }]) assert.equal(validateQualificationCommand({ ...command, ...patch }), null);
  assert.notEqual(command.key, newQualificationCommand(path, decision).key);
});

test('qualification journal separates workspace, principal, tab and discovery owners', () => {
  const prior = globalThis.window, values = new Map(); globalThis.window = { sessionStorage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) } };
  try {
    const journal = qualificationJournalScope(w, p); assert.equal(journal, 'mcp-qualification:' + workshopJournalScope(w, p));
    assert.equal(journal, qualificationJournalScope(w, p)); assert.notEqual(journal, qualificationJournalScope(other, p)); assert.notEqual(journal, qualificationJournalScope(w, other));
    assert.notEqual(journal, 'mcp-discovery:' + workshopJournalScope(w, p)); values.clear(); assert.notEqual(journal, qualificationJournalScope(w, p));
  } finally { if (prior === undefined) delete globalThis.window; else globalThis.window = prior; }
});

test('uncertain qualification requests remain recoverable without resubmission or request replacement', async () => {
  const saved = storage(), journal = 'mcp-qualification:recovery:' + crypto.randomUUID(), command = newQualificationCommand(path, decision);
  const prior = globalThis.fetch; let calls = 0; globalThis.fetch = async () => { calls++; throw Error('Unexpected automatic dispatch'); };
  try {
    assert.equal(await retainAttempt(journal, command, saved), true); assert.deepEqual(await recoverAttempt(journal, validateQualificationCommand, saved), command);
    for (const status of [400, 401, 403, 404, 409, 413, 422, 502, 503]) { assert.equal(await clearRejectedAttempt(journal, command.key, status, true, saved), false); assert.deepEqual(await recoverAttempt(journal, validateQualificationCommand, saved), command); }
    await assert.rejects(retainAttempt(journal, newQualificationCommand(path, decision), saved), /unresolved/);
    await assert.rejects(retainAttempt(journal, { ...command, body: JSON.stringify({ ...decision, revision: 4 }) }, saved), /unresolved/);
    await clearAttempt(journal, 'wrong-key', saved); assert.deepEqual(await recoverAttempt(journal, validateQualificationCommand, saved), command);
    await clearAttempt(journal, command.key, saved); assert.equal(await recoverAttempt(journal, validateQualificationCommand, saved), null); assert.equal(calls, 0);
  } finally { globalThis.fetch = prior; }
});

test('unavailable durable storage keeps an honest temporary receipt and definite first rejection clears it', async () => {
  const journal = 'mcp-qualification:memory:' + crypto.randomUUID(), command = newQualificationCommand(path, decision);
  const unavailable = { read: async () => { throw Error('unavailable'); }, write: async () => { throw Error('unavailable'); }, remove: async () => { throw Error('unavailable'); } };
  assert.equal(await retainAttempt(journal, command, unavailable), false); assert.deepEqual(await recoverAttempt(journal, validateQualificationCommand, unavailable), command);
  assert.equal(await clearRejectedAttempt(journal, command.key, 503, false, unavailable), false); assert.equal(await clearRejectedAttempt(journal, command.key, 409, false, unavailable), true);
  assert.equal(await recoverAttempt(journal, validateQualificationCommand, unavailable), null);
});

test('scope, filter and selection generations abort late reads before they can replace current evidence', async () => {
  const requests = new WorkshopRequests(), old = requests.open(), page = requests.open(); let release;
  const response = new Promise(resolve => { release = resolve; }); let applied = false;
  const flight = (async () => { await response; if (old.current()) applied = true; old.finish(); })();
  requests.reset(); const current = requests.open(); assert.equal(old.signal.aborted, true); assert.equal(page.signal.aborted, true); release(); await flight;
  assert.equal(applied, false); assert.equal(current.current(), true); requests.reset(); assert.equal(current.current(), false);
});

test('attempt and report selection bind exact ordered cases, assertions, task owners and native source hashes', () => {
  assert.equal(qualificationAttemptMatchesSuite(succeeded, suite), true); assert.equal(qualificationReportMatchesSuite(report, suite), true); assert.equal(qualificationReportMatchesAttempt(report, succeeded, suite), true);
  for (const patch of [{ suiteId: other }, { bindingId: other }, { agentId: other }, { suiteSha256: hash('0') }, { configurationSha256: hash('0') }, { cases: [...succeeded.cases].reverse() }, { cases: [{ ...succeeded.cases[0], oracleSha256: hash('0') }, succeeded.cases[1]] }, { cases: [{ ...succeeded.cases[0], checks: succeeded.cases[0].checks.slice(0, 1) }, succeeded.cases[1]] }]) assert.equal(qualificationAttemptMatchesSuite({ ...succeeded, ...patch }, suite), false, JSON.stringify(patch));
  for (const field of ['agentId', 'suiteSha256', 'suiteFileSha256', 'consumerSha256', 'configurationSha256', 'installationSha256']) assert.equal(qualificationReportMatchesSuite({ ...report, evidence: { ...evidence, [field]: field === 'agentId' ? other : hash('0') } }, suite), false, field);
  for (const field of Object.keys(evidence.taskBinding)) assert.equal(qualificationReportMatchesSuite({ ...report, evidence: { ...evidence, taskBinding: { ...evidence.taskBinding, [field]: field.endsWith('Id') ? other : hash('0') } } }, suite), false, field);
  for (const patch of [{ attemptId: other }, { id: other }, { sha256: hash('0') }]) assert.equal(qualificationReportMatchesAttempt({ ...report, ...patch }, succeeded, suite), false);
  assert.equal(qualificationReportMatchesAttempt({ ...report, cases: [{ ...report.cases[0], receiptSha256: hash('0') }, report.cases[1]] }, succeeded, suite), false);
});

test('historical reports stay inspectable while current authority and bounded monitoring remain separate', () => {
  const freshSuite = { ...suite, lastReportId: report.id, lastAttemptId: report.attemptId };
  assert.equal(qualificationReportCurrent(report, freshSuite), true);
  for (const patch of [{ state: 'disabled' }, { state: 'revoked' }, { approvalValid: false }, { authorityCurrent: false }, { lastReportId: other }, { lastAttemptId: other }, { activeAttemptId: other }]) assert.equal(qualificationReportCurrent(report, { ...freshSuite, ...patch }), false);
  for (const patch of [{ approvalValid: false }, { authorityCurrent: false }]) { assert.equal(qualificationReportCurrent({ ...report, ...patch }, freshSuite), false); assert.equal(qualificationReportMatchesSuite({ ...report, ...patch }, freshSuite), true); }
  for (const status of ['requested', 'running', 'recovering']) assert.equal(qualificationAttemptNeedsMonitoring({ ...requested, status }), true);
  for (const status of ['succeeded', 'failed', 'cancelled', 'indeterminate']) assert.equal(qualificationAttemptNeedsMonitoring({ ...requested, status }), false);
  assert.equal(QUALIFICATION_MAX_POLLS, 30);
});
