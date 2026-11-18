import test from 'node:test';
import assert from 'node:assert/strict';
import { projectToolRecipe, projectAcquisition, projectAcquisitionList, projectRecipeList, projectAcquisitionEvents, projectToolMutation, toolActionBody, validateToolMutation } from '../src/lib/tool-acquisition.ts';
import { toolAvailability, toolNeedsMonitoring } from '../src/lib/tool-acquisition-client.ts';
import { workspaceId as w, principalId as p, operationId, recipeId, agentId, scope, recipe, queued, reviewed, tested, released, withdrawn, events } from './tool-acquisition-fixtures.mjs';
const clone=value=>structuredClone(value),envelope=operation=>({...scope,operation});

test('all acquisition phases retain exact public source, review, build and release evidence',()=>{
  for(const operation of [queued,reviewed,tested,released,withdrawn])assert.deepEqual(projectAcquisition(envelope(operation),w,operationId,p),envelope(operation));
  assert.equal(toolAvailability(queued),'Installed state not yet verified');assert.equal(toolAvailability(reviewed),'Not installed');
  assert.match(toolAvailability(released),/normal execution policy/);assert.equal(toolAvailability(withdrawn),'Not installed');
  assert.equal(toolNeedsMonitoring(queued),true);assert.equal(toolNeedsMonitoring(reviewed),false);assert.equal(toolNeedsMonitoring({...released,hostAction:{action:'rollback',status:'queued'}}),true);
  const upgrade={...reviewed,kind:'upgrade',oldCommit:'c'.repeat(40),installedState:'unknown',review:{...reviewed.review,kind:'upgrade',oldCommit:'c'.repeat(40)}};
  assert.ok(projectAcquisition(envelope(upgrade),w,operationId,p));
});
test('recipe review preserves Unicode codepoints and refuses unqualified commands, network claims and malformed inventories',()=>{
  assert.deepEqual(projectToolRecipe(recipe,p),recipe);
  assert.ok(projectToolRecipe({...recipe,name:'🧭'.repeat(120),description:'🧭'.repeat(4096)},p));
  assert.equal(projectToolRecipe({...recipe,name:'🧭'.repeat(121)},p),null);
  for(const patch of [{ownerPrincipalId:agentId},{repository:'https://evil.example/tool.git'},{build:{...recipe.build,network:'allowed'}},{cleanupPaths:[]},{artifactPaths:['checkout/tool']},
    {lockfilePaths:['../secret']},{tests:[recipe.tests[0],recipe.tests[0]]},{tests:recipe.tests.map(test=>({...test,kind:'smoke'}))},{approvalValid:'yes'}])assert.equal(projectToolRecipe({...recipe,...patch},p),null);
  assert.deepEqual(projectToolRecipe({...recipe,build_argv:['PRIVATE'],setupToken:'PRIVATE'},p),recipe);
});
test('private acquisition projections enforce principal, operation and source identities without leaking worker authority',()=>{
  assert.equal(projectAcquisition(envelope(reviewed),agentId),null);assert.equal(projectAcquisition(envelope(reviewed),w,operationId,agentId),null);
  for(const patch of [{ownerPrincipalId:agentId},{owned:false},{agentId:w},{repository:'https://github.com/other/tool.git'},{oldCommit:'b'.repeat(40)},{kind:'upgrade'},
    {source:{...reviewed.source,operation_id:recipeId}},{source:{...reviewed.source,commit:'b'.repeat(40)}},{review:{...reviewed.review,sourceSha256:'0'.repeat(64)}},
    {review:{...reviewed.review,buildRoot:'/private/host'}},{review:{...reviewed.review,buildArgv:['/usr/local/tools/script']}},{review:{...reviewed.review,buildArgv:['/bin/sh','line\nnext']}},
    {review:{...reviewed.review,requiresPostflight:false}},{review:{...reviewed.review,automaticActivation:true}},{review:{...reviewed.review,lockfiles:[]}},
    {phase:'ready_to_execute'},{allowedActions:['execute']},{errorCode:'/private/path'}])assert.equal(projectAcquisition(envelope({...reviewed,...patch}),w),null);
  const injected={...envelope(tested),workerToken:'PRIVATE',operation:{...tested,source_request_mac:'PRIVATE',execution_contract:{privatePath:'/secret'},build:{...tested.build,stdout:'PRIVATE'}}};
  assert.deepEqual(projectAcquisition(injected,w),envelope(tested));
});
test('held and withdrawn records cannot impersonate independently released installations',()=>{
  for(const patch of [{releaseState:'none'},{releaseBindingSha256:null},{releaseEvidenceSha256:null},{releaseGeneration:0}])assert.equal(projectAcquisition(envelope({...released,...patch}),w),null);
  for(const patch of [{installedState:'released'},{kind:'upgrade',oldCommit:'c'.repeat(40)},{phase:'active'}])assert.equal(projectAcquisition(envelope({...withdrawn,...patch}),w),null);
  assert.ok(projectAcquisition(envelope({...tested,phase:'awaiting_repin',installedState:'held',allowedActions:['repin','rollback']}),w));
});
test('acquisition pages and audit cursors reject mixed identities, duplicates and skipped cursor bindings',()=>{
  assert.deepEqual(projectAcquisitionList({...scope,items:[queued,reviewed],nextCursor:null},w),null);
  assert.ok(projectAcquisitionList({...scope,items:[queued],nextCursor:null},w,p));assert.ok(projectRecipeList({...scope,items:[recipe],nextCursor:recipeId},w,p));
  assert.equal(projectRecipeList({...scope,items:[recipe,recipe],nextCursor:null},w),null);
  assert.equal(projectAcquisitionList({...scope,items:[],nextCursor:'../next'},w),null);
  assert.deepEqual(projectAcquisitionEvents(events,w,0,p,operationId),events);
  for(const patch of [{operationId:recipeId},{items:[events.items[1],events.items[0]]},{nextCursor:3},{items:[],hasMore:true}])assert.equal(projectAcquisitionEvents({...events,...patch},w,0,p,operationId),null);
  assert.equal(projectAcquisitionEvents(events,w,1,p,operationId),null);
});
test('forms build only exact source and permitted review or lifecycle bodies, never caller commands or pins',()=>{
  assert.deepEqual(validateToolMutation([],{recipeId,commit:queued.newCommit,confirm:true}),{recipeId,commit:queued.newCommit,confirm:true});
  for(const patch of [{repository:recipe.repository},{argv:['/bin/sh']},{commit:'main'},{confirm:false}])assert.equal(validateToolMutation([],{recipeId,commit:queued.newCommit,confirm:true,...patch}),null);
  assert.deepEqual(toolActionBody(reviewed,'approve-build'),{revision:3,reviewSha256:reviewed.review.sha256,confirm:true});
  assert.deepEqual(toolActionBody(tested,'approve'),{revision:6,evidenceSha256:tested.evidenceSha256,confirm:true});
  assert.deepEqual(toolActionBody(released,'rollback'),{revision:12,bindingSha256:released.bindingSha256,confirm:true});
  assert.equal(toolActionBody(queued,'approve-build'),null);assert.equal(toolActionBody(withdrawn,'rollback'),null);
  const recovery={...reviewed,phase:'indeterminate',recipe:{...recipe,approvalValid:false},hostAction:{action:'reconcile',status:'running'},allowedActions:['reconcile']};
  assert.ok(projectAcquisition(envelope(recovery),w,operationId,p));
  assert.deepEqual(toolActionBody(recovery,'reconcile'),{revision:reviewed.revision,bindingSha256:reviewed.bindingSha256,confirm:true});
  for(const patch of [{revision:'12'},{revision:-1},{confirm:false},{bindingSha256:null},{executablePins:{}}])assert.equal(validateToolMutation([operationId,'rollback'],{revision:12,bindingSha256:released.bindingSha256,confirm:true,...patch}),null);
});
test('committed mutation receipts bind their original revision and exact action even after later worker progress',()=>{
  assert.ok(projectToolMutation(envelope(queued),w,[],{recipeId,commit:queued.newCommit,confirm:true},p));
  assert.equal(projectToolMutation(envelope(reviewed),w,[],{recipeId,commit:queued.newCommit,confirm:true},p),null);
  const buildApproved={...reviewed,revision:4,phase:'build_queued',allowedActions:['cancel']},body=toolActionBody(reviewed,'approve-build');
  assert.ok(projectToolMutation(envelope(buildApproved),w,[operationId,'approve-build'],body,p));
  assert.equal(projectToolMutation(envelope(tested),w,[operationId,'approve-build'],body,p),null);
  const rollbackQueued={...released,revision:13,hostAction:{action:'rollback',status:'queued'},allowedActions:[]};
  assert.ok(projectToolMutation(envelope(rollbackQueued),w,[operationId,'rollback'],toolActionBody(released,'rollback'),p));
  assert.equal(projectToolMutation(envelope({...rollbackQueued,hostAction:{action:'repin',status:'queued'}}),w,[operationId,'rollback'],toolActionBody(released,'rollback'),p),null);
  const cancelBody={revision:queued.revision,bindingSha256:queued.bindingSha256,confirm:true};
  assert.ok(projectToolMutation(envelope({...queued,revision:2,phase:'cancelled',allowedActions:[]}),w,[operationId,'cancel'],cancelBody,p));
  const approved={...tested,revision:7,phase:'approved',allowedActions:['cancel']};
  assert.ok(projectToolMutation(envelope({...approved,revision:8,phase:'rejected',allowedActions:[]}),w,[operationId,'cancel'],toolActionBody(approved,'cancel'),p));
  assert.equal(projectToolMutation(envelope({...approved,revision:8}),w,[operationId,'cancel'],toolActionBody(approved,'cancel'),p),null);
});
