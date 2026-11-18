// Explicit public wire fixtures for boundary tests. Real HTTP export is tested separately.
export const workspaceId='11111111-1111-4111-8111-111111111111',principalId='22222222-2222-4222-8222-222222222222';
export const operationId='33333333-3333-4333-8333-333333333333',recipeId='44444444-4444-4444-8444-444444444444',agentId='55555555-5555-4555-8555-555555555555';
export const scope={schema:'potbot-tool-acquisitions/v1',workspaceId,principalId};
export const recipe={id:recipeId,name:'Pinned inspection tool',description:'An operator-approved offline build recipe',slug:'scope-tool',repository:'https://github.com/example/scope-tool.git',
  recipeSha256:'1'.repeat(64),agentId,ownerPrincipalId:principalId,createdAt:'2026-11-17T22:45:00+00:00',
  build:{commandSha256:'2'.repeat(64),timeoutMs:600000,network:'denied'},
  tests:[{name:'smoke',kind:'smoke',timeoutMs:30000},{name:'unit',kind:'unit',timeoutMs:30000}],cleanupPaths:['build-cache'],lockfilePaths:['requirements.lock'],artifactPaths:['static/tool'],approvalValid:true};
export const queued={id:operationId,ownerPrincipalId:principalId,owned:true,agentId,slug:recipe.slug,kind:null,repository:recipe.repository,oldCommit:null,newCommit:'a'.repeat(40),revision:1,phase:'source_queued',recipe,
  source:null,review:null,build:null,tests:[],evidenceSha256:null,suiteSha256:'3'.repeat(64),releaseState:'none',releaseGeneration:0,releaseBindingSha256:null,releaseEvidenceSha256:null,
  installedState:'unknown',hostAction:null,allowedActions:['cancel'],bindingSha256:'4'.repeat(64),errorCode:null,createdAt:'2026-11-17T22:46:00+00:00',updatedAt:'2026-11-17T22:46:00+00:00'};
export const source={schema:'potbot-tool-source/v1',operation_id:operationId,repository:recipe.repository,commit:queued.newCommit,git_tree:'b'.repeat(40),tree_sha256:'5'.repeat(64),files:5,bytes:4096,source_sha256:'6'.repeat(64),storage_sha256:'7'.repeat(64)};
export const review={sha256:'8'.repeat(64),kind:'install',oldCommit:null,newCommit:queued.newCommit,sourceSha256:source.source_sha256,recipeSha256:recipe.recipeSha256,commandSha256:'9'.repeat(64),
  imageDigest:'a'.repeat(64),qualificationSha256:'b'.repeat(64),buildRoot:'/home/agent-alpha/builds/'+operationId.replaceAll('-',''),
  buildArgv:['/usr/bin/python3','-I','-B','/home/agent-alpha/builds/'+operationId.replaceAll('-','')+'/checkout/build.py'],lockfiles:[['requirements.lock','c'.repeat(64)]],network:'denied',automaticActivation:false,requiresPostflight:true};
export const reviewed={...queued,revision:3,phase:'review',kind:'install',source,review,installedState:'absent',allowedActions:['approve-build','cancel']};
export const tested={...reviewed,revision:6,phase:'awaiting_approval',build:{commandSha256:review.commandSha256,isolationIdentity:'d'.repeat(64),treeSha256:'e'.repeat(64),files:9,bytes:8192},
  tests:[{name:'smoke',kind:'smoke',elapsedMs:12,passed:true},{name:'unit',kind:'unit',elapsedMs:20,passed:true}],evidenceSha256:'f'.repeat(64),allowedActions:['approve','reject']};
export const released={...tested,revision:12,phase:'active',installedState:'released',releaseState:'released',releaseGeneration:1,releaseBindingSha256:'1'.repeat(64),releaseEvidenceSha256:'2'.repeat(64),allowedActions:['rollback']};
export const withdrawn={...released,revision:18,phase:'withdrawn',installedState:'absent',releaseState:'withdrawn',releaseGeneration:2,allowedActions:[]};
export const events={...scope,operationId,items:[{revision:1,action:'source_requested',priorState:'none',nextState:'acquiring',evidenceSha256:'4'.repeat(64),recordedAt:queued.createdAt},
  {revision:2,action:'source_claimed',priorState:'acquiring',nextState:'acquiring',evidenceSha256:'4'.repeat(64),recordedAt:queued.createdAt}],nextCursor:2,hasMore:false};
