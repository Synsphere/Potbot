import test from 'node:test';
import assert from 'node:assert/strict';
import { upgradeRecord, upgradeListing, releaseStatus } from '../src/lib/tool-upgrades.ts';
const ws = '4ef1cf0d-aa07-48b4-a58c-786110ed1c43';
const record = {
  id:'5af022e6-64cc-45ae-b77b-26dd557c3a0f', workspaceId: ws,
  slug:'stemlab', oldCommit:'a'.repeat(40), newCommit:'b'.repeat(40),
  suiteSha256:'c'.repeat(64), evidenceSha256:'d'.repeat(64),
  passedTests:3, tests:[{name:'smoke-test',kind:'smoke',elapsedMs:12,passed:true},{name:'unit-test',kind:'unit',elapsedMs:20,passed:true},{name:'regression-test',kind:'regression',elapsedMs:18,passed:true}], revision:0, state:'awaiting_approval', updatedAt:'2026-10-25T10:17:00Z'
};
test('returns only public upgrade projection, discarding injected internals', () => {
  const injected = {...record, privatePath:'/etc/shadow', token:'secret', workerToken:'xyz'};
  assert.deepEqual(upgradeRecord(injected, ws), record);
  assert.deepEqual(upgradeListing({schema:'potbot-tool-upgrades-s11/v1', items:[injected]}, ws),
                   {schema:'potbot-tool-upgrades-s11/v1', items:[record]});
});
test('rejects wrong workspace, untrusted state, invalid hashes and identities', () => {
  for (const modified of [
    {...record, workspaceId:'00000000-0000-4000-8000-000000000000'},
    {...record, evidenceSha256:'e'.repeat(63)},
    {...record, slug:'../unsafe'}, {...record, state:'shell_write'},
    {...record, passedTests:0}, {...record, revision:1.5},
    {...record, newCommit:record.oldCommit},
  ]) assert.equal(upgradeRecord(modified, ws), null);
});
test('rejects overlong and malformed upgrade feeds', () => {
  assert.equal(upgradeListing({schema:'bad',items:[record]}, ws), null);
  assert.equal(upgradeListing({schema:'potbot-tool-upgrades-s11/v1',items:Array(101).fill(record)}, ws), null);
  assert.equal(upgradeListing({schema:'potbot-tool-upgrades-s11/v1',items:[{...record,state:'root'}]}, ws), null);
});
test('preserves the bounded S12 release projection and drops private postflight data', () => {
  const released = {...record, state:'active', releaseState:'released', releaseGeneration:2,
    releaseBindingSha256:'e'.repeat(64), releaseEvidenceSha256:'f'.repeat(64)};
  assert.deepEqual(upgradeRecord({...released, releaseMac:'secret', postflightOutput:'private'}, ws), released);
  assert.equal(releaseStatus(released).label, 'Released');
});
test('rejects incomplete release claims and invalid release state or bindings', () => {
  const released = {...record, releaseState:'released', releaseGeneration:2,
    releaseBindingSha256:'e'.repeat(64), releaseEvidenceSha256:'f'.repeat(64)};
  for (const modified of [
    {...record, releaseState:'released'},
    {...released, releaseState:'force_release'},
    {...released, releaseGeneration:-1},
    {...released, releaseGeneration:1.5},
    {...released, releaseBindingSha256:null},
    {...released, releaseEvidenceSha256:'f'.repeat(63)},
  ]) assert.equal(upgradeRecord(modified, ws), null);
});
test('failed, unresolved and legacy records never display a successful release', () => {
  assert.equal(releaseStatus({...record, state:'active'}).label, 'Release not established');
  assert.equal(releaseStatus({...record, releaseState:'repinned'}).label, 'Awaiting postflight');
  assert.equal(releaseStatus({...record, releaseState:'failed'}).attention, true);
  assert.equal(releaseStatus({...record, releaseState:'indeterminate'}).attention, true);
});
