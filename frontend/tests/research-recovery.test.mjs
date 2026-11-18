import test from 'node:test';
import assert from 'node:assert/strict';
import { WorkshopRequests, retainAttempt, recoverAttempt, clearRejectedAttempt, clearAttempt } from '../src/lib/skill-workshop-client.ts';
import { RESEARCH_ENDPOINT, newResearchAttempt, validateResearchAttempt, researchScopeInvalidated, researchSelection, researchJournalScope, researchRequestHeaders } from '../src/lib/research-client.ts';
import * as f from './research-fixtures.mjs';
function storage() { const slots = new Map(); return { slots, read: async key => slots.get(key), write: async (key, value) => slots.set(key, structuredClone(value)), remove: async (key, expected) => { if (slots.get(key)?.key === expected) slots.delete(key); } }; }

test('uncertain research commands preserve literal body and key across reload; recovery never sends', async () => {
  const disk = storage(), scope = 'research:test:' + crypto.randomUUID(), attempt = newResearchAttempt(RESEARCH_ENDPOINT + '/runs', f.runBody);
  assert.equal(await retainAttempt(scope, attempt, disk), true);
  const recovered = await recoverAttempt(scope, validateResearchAttempt, disk); assert.deepEqual(recovered, attempt);
  await assert.rejects(retainAttempt(scope, newResearchAttempt(RESEARCH_ENDPOINT + '/runs', f.runBody), disk), /unresolved/);
  assert.equal(await clearRejectedAttempt(scope, attempt.key, 503, false, disk), false);
  assert.equal(await clearRejectedAttempt(scope, attempt.key, 409, true, disk), false);
  assert.deepEqual(await recoverAttempt(scope, validateResearchAttempt, disk), attempt);
  await clearAttempt(scope, 'wrong-key', disk); assert.deepEqual(await recoverAttempt(scope, validateResearchAttempt, disk), attempt);
  await clearAttempt(scope, attempt.key, disk); assert.equal(await recoverAttempt(scope, validateResearchAttempt, disk), null);
});

test('only definitive first rejection releases the journal; unsupported routes and citation reads cannot be journaled', async () => {
  const disk = storage(), scope = 'research:test:' + crypto.randomUUID(), attempt = newResearchAttempt(RESEARCH_ENDPOINT + '/actions', f.actionBody);
  await retainAttempt(scope, attempt, disk); assert.equal(await clearRejectedAttempt(scope, attempt.key, 400, false, disk), true); assert.equal(await recoverAttempt(scope, validateResearchAttempt, disk), null);
  for (const path of ['/api/ui/research/citations', '/api/ui/research/runs/latest/cancel', '/api/ui/research/provider/search', '/api/ui/research/runs?provider=brave', '/api/ui/task-execution/runs']) assert.equal(validateResearchAttempt(newResearchAttempt(path, f.citationBody)), null);
  for (const raw of ['{"confirm":true,"confirm":false}', ' '.repeat(131073)]) assert.equal(validateResearchAttempt({ ...attempt, body: raw }), null);
  assert.equal(validateResearchAttempt({ ...attempt, key: ':invalid' }), null);
});

test('workspace/principal and request generations fence delayed history, citation and mutation replies', () => {
  assert.equal(researchScopeInvalidated(401, null, f.scope), true); assert.equal(researchScopeInvalidated(403, null, f.scope), true);
  assert.equal(researchScopeInvalidated(200, { ...f.envelope, scope: { ...f.scope, principalId: f.other } }, f.scope), true);
  assert.equal(researchScopeInvalidated(200, { ...f.envelope, scope: { ...f.scope, workspaceId: f.other } }, f.scope), true);
  assert.equal(researchScopeInvalidated(409, null, f.scope), false);
  const requests = new WorkshopRequests(), old = requests.open(); requests.reset(); const current = requests.open(); assert.equal(old.current(), false); assert.equal(old.signal.aborted, true); assert.equal(current.current(), true); old.finish(); assert.equal(current.current(), true); current.finish();
  assert.notEqual(researchJournalScope(f.w, f.p), researchJournalScope(f.w, f.other));
  assert.notEqual(researchJournalScope(f.w, f.p), researchJournalScope(f.other, f.p));
});

test('recovered commands retain their reviewed principal and cannot be adopted by a new principal', async () => {
  const disk = storage(), scopeA = researchJournalScope(f.w, f.p), scopeB = researchJournalScope(f.w, f.other), attempt = newResearchAttempt(RESEARCH_ENDPOINT + '/runs', f.runBody);
  await retainAttempt(scopeA, attempt, disk);
  assert.deepEqual(researchRequestHeaders(f.scope), { 'X-Potbot-Workspace': f.w, 'X-Potbot-Expected-Principal': f.p });
  assert.deepEqual(researchRequestHeaders({ ...f.scope, principalId: f.other }), { 'X-Potbot-Workspace': f.w, 'X-Potbot-Expected-Principal': f.other });
  assert.equal(await clearRejectedAttempt(scopeA, attempt.key, 403, true, disk), false);
  assert.deepEqual(await recoverAttempt(scopeA, validateResearchAttempt, disk), attempt);
  assert.equal(await recoverAttempt(scopeB, validateResearchAttempt, disk), null);
  assert.throws(() => researchRequestHeaders({ ...f.scope, principalId: f.p + ',' + f.other }), /exact reviewed/);
  assert.throws(() => researchRequestHeaders({ ...f.scope, principalId: 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA' }), /exact reviewed/);
});

test('read-only exact run URLs cannot smuggle duplicate or foreign task selectors', () => {
  assert.deepEqual(researchSelection(`?workspace=${f.w}&run=${f.runId}`, f.w), { episode: null, state: null, need: null, run: f.runId, invalid: false });
  for (const query of [`?run=${f.runId}`, `?workspace=${f.other}&run=${f.runId}`, `?workspace=${f.w}&run=${f.runId}&run=${f.runId}`, `?workspace=${f.w}&episode=${f.episode}&state=latest`, `?workspace=${f.w}&need=${f.needId}&need=${f.other}`]) assert.equal(researchSelection(query, f.w).invalid, true);
});
