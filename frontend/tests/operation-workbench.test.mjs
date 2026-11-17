import test from 'node:test';
import assert from 'node:assert/strict';
import {
  WORKBENCH_SCHEMA, EMPTY_FORM, buildJobSpec, validateJobSpec, projectContract, projectCheck,
  projectJobEnvelope, projectEvents, projectResult, projectArtifact, artifactBytes, checkUsable, mutationAttempt,
} from '../src/lib/operation-workbench.ts';

export const workspaceId='c7687924-49a1-4a65-b48e-f1d9d29ed409';
export const principalId='1050b1bb-21c2-4188-a053-01498ce7087c';
export const operationId='a'.repeat(64), jobId='9a71e07c-150f-41ed-b248-4f55c42e83f0';
export const scope={schema:WORKBENCH_SCHEMA,workspaceId,principalId,operationId};
export const contract={...scope,kind:'cog',reference:'syn://cog/example',executionReference:'syn://thing/example',name:'Example',supported:true,jobType:'cog.execute',
  inputFields:['episode_id','agent_id','repository_state_id','profile_sha256','type_ref','value'],reasonCodes:[],maxInputBytes:16384};
export const report={schema:'potbot-operation-check/v1',operationId,eligible:true,checks:[{code:'installed_binding',status:'passed'}],
  bindings:{requestSha256:'b'.repeat(64),contractRevisionId:workspaceId,profileSha256:'c'.repeat(64),releaseSha256:'e'.repeat(64),grantGeneration:2},
  checkedAt:'2026-11-17T21:00:00+00:00',expiresAt:'2026-11-17T21:05:00+00:00',bindingSha256:'d'.repeat(64)};
export const job={id:jobId,episode_id:workspaceId,contract_revision_id:workspaceId,job_type:'operation.check',state:'succeeded',
  owner_principal_id:principalId,operation_check_id:null,update_token:3,selected_models:[],progress:{stage:'succeeded'},
  cancellation_requested:false,result:{operation_check:report},error_code:null,output_bytes:0,
  created_at:'2026-11-17T21:00:00+00:00',updated_at:'2026-11-17T21:00:01+00:00',started_at:null,finished_at:null,
  dispatched_at:null,heartbeat_at:null,lease_until:null};
export const nativeRequest={job_type:'cog.execute',episode_id:workspaceId,selected_models:[],input:{agent_id:principalId,repository_state_id:workspaceId,
  operation_ref:'syn://thing/example',profile_sha256:'e'.repeat(64),type_ref:'syn://type/example-input',value:{query:'bounded example'}}};

test('workbench forms bind operation identity and create exact native/model JobSpecs',()=>{
  assert.deepEqual(buildJobSpec(contract,{...EMPTY_FORM,taskId:workspaceId,agentId:principalId,repositoryStateId:workspaceId,
    profileSha256:'e'.repeat(64),typeRef:'syn://type/example-input',value:'{"query":"bounded example"}'}),nativeRequest);
  const model={...contract,kind:'model',reference:'model:provider/example',jobType:'model.generate'};
  const request=buildJobSpec(model,{...EMPTY_FORM,taskId:workspaceId,prompt:'Hello',provider:'openai',role:'coding'});
  assert.equal(request.selected_models.length,1);assert.deepEqual(request.selected_models[0],{provider:'openai',model:'provider/example',role:'coding'});
  assert.deepEqual(request.input,{prompt:'Hello',max_output_tokens:2048,timeout_seconds:60});
  assert.throws(()=>buildJobSpec({...contract,supported:false},EMPTY_FORM),/supported/);
  assert.throws(()=>buildJobSpec(contract,{...EMPTY_FORM,taskId:workspaceId,value:'{bad'}),/valid JSON/);
  assert.equal(validateJobSpec({...nativeRequest,input:{...nativeRequest.input,value:Infinity}}),null);
  assert.equal(validateJobSpec({...nativeRequest,input:{...nativeRequest.input,value:9007199254740992}}),null);
  assert.equal(validateJobSpec({...nativeRequest,input:{...nativeRequest.input,value:'x'.repeat(16384)}}),null);
  assert.equal(validateJobSpec({...request,selected_models:[request.selected_models[0],request.selected_models[0]]}),null);
  assert.equal(validateJobSpec({...request,input:{...request.input,credential:'must-not-enter'}}),null);
});

test('the full canonical JobSpec, including its selection and envelope, must fit the UTF-8 byte allowance',()=>{
  const model={job_type:'model.generate',episode_id:workspaceId,input:{prompt:'a'.repeat(16200),max_output_tokens:16,timeout_seconds:30},
    selected_models:[{provider:'openai',model:'fixture-model',role:'reasoning'}]};
  assert.equal(new TextEncoder().encode(JSON.stringify(model.input)).length,16257);
  assert.equal(new TextEncoder().encode(JSON.stringify(model)).length,16432);
  assert.equal(validateJobSpec(model),null);
  const empty={...model,input:{...model.input,prompt:''}};
  const allowance=16384-new TextEncoder().encode(JSON.stringify(empty)).length;
  for(const character of ['a','é','🌱']) {
    const width=new TextEncoder().encode(character).length;
    const prompt=character.repeat(Math.floor(allowance/width))+'a'.repeat(allowance%width);
    const exact={...empty,input:{...empty.input,prompt}};
    assert.equal(new TextEncoder().encode(JSON.stringify(exact)).length,16384);
    assert.ok(validateJobSpec(exact));
    assert.equal(validateJobSpec({...exact,input:{...exact.input,prompt:prompt+'a'}}),null);
  }
  const numeric={...nativeRequest,input:{...nativeRequest.input,value:{small:1e-7,text:''}}};
  // Python canonical_json writes 1e-07, one byte longer than JSON.stringify.
  const padding=16384-new TextEncoder().encode(JSON.stringify(numeric)).length-1;
  const exact={...numeric,input:{...numeric.input,value:{small:1e-7,text:'a'.repeat(padding)}}};
  assert.ok(validateJobSpec(exact));
  assert.equal(validateJobSpec({...exact,input:{...exact.input,value:{small:1e-7,text:'a'.repeat(padding+1)}}}),null);
  assert.equal(validateJobSpec({...model,input:{...model.input,prompt:'small'},selected_models:[{...model.selected_models[0],extra:'not-in-the-contract'}]}),null);
});

test('contract and jobs reject cross-scope identity and whitelist public fields',()=>{
  assert.deepEqual(projectContract({...contract,secret:'PRIVATE'},workspaceId,operationId,principalId),contract);
  assert.equal(projectContract({...contract,principalId:workspaceId},workspaceId,operationId,principalId),null);
  assert.equal(projectContract({...contract,jobType:'bash.execute'},workspaceId,operationId),null);
  const projected=projectJobEnvelope({...scope,job:{...job,input:{secret:'PRIVATE'},authorization:{token:'PRIVATE'}},secret:'PRIVATE'},workspaceId,operationId,jobId,principalId);
  assert.deepEqual(projected,{...scope,job});assert.ok(!JSON.stringify(projected).includes('PRIVATE'));
  assert.equal(projectJobEnvelope({...scope,job:{...job,owner_principal_id:workspaceId}},workspaceId,operationId),null);
  assert.equal(projectJobEnvelope({...scope,job},workspaceId,'f'.repeat(64)),null);
  assert.equal(projectJobEnvelope({...scope,job:{...job,id:workspaceId}},workspaceId,operationId,jobId),null);
});

test('eligible check requires passed observations, exact operation and an unexpired terminal check job',()=>{
  assert.deepEqual(projectCheck(report,operationId),report);
  assert.equal(checkUsable(job,operationId,Date.parse('2026-11-17T21:02:00Z')),true);
  assert.equal(checkUsable(job,operationId,Date.parse('2026-11-17T21:05:00Z')),false);
  assert.equal(checkUsable({...job,state:'running'},operationId,0),false);
  assert.equal(checkUsable({...job,job_type:'cog.execute'},operationId,0),false);
  assert.equal(projectCheck({...report,checks:[{code:'grant',status:'blocked'}]},operationId),null);
  assert.equal(projectCheck({...report,operationId:'f'.repeat(64)},operationId),null);
  assert.equal(projectCheck({...report,expiresAt:'2026-11-17T20:00:00Z'},operationId),null);
});

test('event replay is ordered and scoped, with untrusted nested fields removed',()=>{
  const event={sequence:4,id:workspaceId,job_id:jobId,event_type:'job.progress',recorded_at:job.updated_at,
    data:{job:{id:jobId,progress:{stage:'checking'},input:{secret:'PRIVATE'}},privatePath:'/private'}};
  const envelope={...scope,items:[event],next_cursor:4,has_more:false};
  const parsed=projectEvents(envelope,workspaceId,operationId,jobId,3,principalId);
  assert.deepEqual(parsed.items[0].data,{stage:'checking'});assert.ok(!JSON.stringify(parsed).includes('PRIVATE'));
  assert.equal(projectEvents({...envelope,items:[event,event]},workspaceId,operationId,jobId,3),null);
  assert.equal(projectEvents({...envelope,next_cursor:2},workspaceId,operationId,jobId,3),null);
  assert.equal(projectEvents({...envelope,items:[{...event,job_id:workspaceId}]},workspaceId,operationId,jobId,3),null);
  assert.equal(projectEvents({...envelope,items:[],has_more:true,next_cursor:3},workspaceId,operationId,jobId,3),null);
});

test('result projection exposes only owned text and bounded artifact identity',()=>{
  const value={...scope,job,output:'bounded output',truncated:false,artifactText:'{}',artifactTruncated:false,artifact:{id:workspaceId,sha256:'f'.repeat(64),bytes:18,mediaType:'application/json',path:'/PRIVATE'}};
  const result=projectResult(value,workspaceId,operationId,jobId,principalId);
  assert.equal(result.output,'bounded output');assert.ok(!JSON.stringify(result).includes('PRIVATE'));
  assert.equal(projectResult({...value,artifact:{...value.artifact,sha256:'invalid'}},workspaceId,operationId,jobId),null);
  assert.equal(projectResult({...value,job:{...job,owner_principal_id:workspaceId}},workspaceId,operationId,jobId),null);
});

test('artifact range projection enforces identity, cursor continuity and canonical bounded bytes',()=>{
  const artifact={id:workspaceId,sha256:'f'.repeat(64),bytes:4,mediaType:'application/json'};
  const first={...scope,artifact,offset:0,dataBase64:btoa('ab'),nextOffset:2};
  assert.deepEqual(projectArtifact(first,workspaceId,operationId,0,2,principalId),first);
  assert.deepEqual(artifactBytes(first.dataBase64),new Uint8Array([97,98]));
  assert.equal(projectArtifact({...first,nextOffset:null},workspaceId,operationId,0,2),null);
  assert.equal(projectArtifact({...first,nextOffset:3},workspaceId,operationId,0,2),null);
  assert.equal(projectArtifact({...first,offset:1},workspaceId,operationId,0,2),null);
  assert.equal(projectArtifact({...first,dataBase64:'YQ==='},workspaceId,operationId,0,2),null);
  const last={...first,offset:2,dataBase64:btoa('cd'),nextOffset:null};
  assert.deepEqual(projectArtifact(last,workspaceId,operationId,2,2),last);
  assert.throws(()=>artifactBytes('x'.repeat(349529)),/encoding/);
  assert.equal(artifactBytes(btoa('x'.repeat(262144))).length,262144);
});

test('uncertain mutation retries retain the exact key and payload; changed requests cannot reuse it',()=>{
  const attempt=mutationAttempt('/fixed/checks',nativeRequest);
  assert.match(attempt.key,/^workbench:[a-f0-9-]{36}$/);
  assert.equal(mutationAttempt('/fixed/checks',nativeRequest,attempt),attempt);
  assert.throws(()=>mutationAttempt('/fixed/runs',nativeRequest,attempt),/pending/);
  assert.throws(()=>mutationAttempt('/fixed/checks',{...nativeRequest,episode_id:principalId},attempt),/pending/);
  assert.notEqual(mutationAttempt('/fixed/checks',nativeRequest).key,attempt.key);
});
