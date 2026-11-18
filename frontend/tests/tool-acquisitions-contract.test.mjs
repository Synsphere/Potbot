import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { projectAcquisition, projectAcquisitionEvents, projectAcquisitionList, projectRecipeList, projectToolMutation } from '../src/lib/tool-acquisition.ts';

test('real HTTP and PostgreSQL acquisition lifecycle matches every browser projection and original mutation receipt',async()=>{
  // Captured from the first-install → source/build review → host release →
  // rollback-to-absence pipeline. Process outcomes in that test are explicit
  // fixtures; this is a cross-layer contract gate, not sandbox qualification.
  const path=process.env.POTBOT_TOOL_ACQUISITION_CONTRACT_OUTPUT??new URL('./fixtures/tool-acquisitions-http.json',import.meta.url);
  const {workspaceId,principalId,operationId,responses:wire}=JSON.parse(await readFile(path,'utf8'));
  assert.deepEqual(projectRecipeList(wire.recipes,workspaceId,principalId),wire.recipes);
  assert.deepEqual(projectAcquisitionList(wire.list,workspaceId,principalId),wire.list);
  assert.deepEqual(projectAcquisitionEvents(wire.events,workspaceId,0,principalId,operationId),wire.events);
  for(const key of ['requested','reviewed','approve-build','tested','approve','repin','repinned','postflight','released','rollback','withdrawn','detail']) {
    assert.deepEqual(projectAcquisition(wire[key],workspaceId,operationId,principalId),wire[key],key);
  }
  const requested=wire.requested.operation;
  assert.deepEqual(projectToolMutation(wire.requested,workspaceId,[],{recipeId:requested.recipe.id,commit:requested.newCommit,confirm:true},principalId),wire.requested);
  for(const action of ['approve-build','approve','repin','postflight','rollback']) {
    const result=wire[action].operation,binding=action==='approve-build'?'reviewSha256':action==='approve'?'evidenceSha256':'bindingSha256';
    const digest=action==='approve-build'?result.review.sha256:action==='approve'?result.evidenceSha256:'0'.repeat(64);
    assert.deepEqual(projectToolMutation(wire[action],workspaceId,[operationId,action],{revision:result.revision-1,[binding]:digest,confirm:true},principalId),wire[action],action);
  }
  assert.equal(wire.requested.operation.kind,null);assert.equal(wire.requested.operation.installedState,'unknown');
  assert.equal(wire.reviewed.operation.kind,'install');assert.equal(wire.reviewed.operation.oldCommit,null);
  assert.equal(wire.reviewed.operation.installedState,'absent');assert.equal(wire.tested.operation.installedState,'absent');
  assert.equal(wire.repinned.operation.installedState,'held');assert.equal(wire.released.operation.installedState,'released');
  assert.equal(wire.withdrawn.operation.phase,'withdrawn');assert.equal(wire.withdrawn.operation.installedState,'absent');
  assert.equal(wire.withdrawn.operation.releaseState,'withdrawn');assert.ok(wire.withdrawn.operation.releaseEvidenceSha256);
  assert.ok(wire.events.items.some(item=>item.action==='withdrawn'));
});
