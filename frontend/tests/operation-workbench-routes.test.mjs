import test from 'node:test';
import assert from 'node:assert/strict';
import { sessionCookie } from '../src/lib/server/security.ts';
import { workbenchJSON, operationWorkbenchRoute } from '../src/lib/server/operation-workbench.ts';
import { GET as describe } from '../src/app/api/ui/operations/[id]/contract/route.ts';
import { POST as submit } from '../src/app/api/ui/operations/[id]/[action]/route.ts';
import { GET as inspect } from '../src/app/api/ui/operations/[id]/jobs/[jobId]/route.ts';
import { GET as readJob, POST as cancel } from '../src/app/api/ui/operations/[id]/jobs/[jobId]/[action]/route.ts';

const workspaceId='c7687924-49a1-4a65-b48e-f1d9d29ed409', principalId='1050b1bb-21c2-4188-a053-01498ce7087c';
const operationId='a'.repeat(64), jobId='9a71e07c-150f-41ed-b248-4f55c42e83f0', checkId='f2062423-cb5a-4cc2-bcf8-c5201545ac6a';
const auth={password:'operator-password-very-long',signingKey:'a'.repeat(64),publicOrigin:'http://localhost:3100'};
const cookie=sessionCookie(auth).split(';')[0];
const env={POTBOT_UI_PASSWORD:auth.password,POTBOT_UI_COOKIE_SECRET:auth.signingKey,POTBOT_UI_PUBLIC_ORIGIN:auth.publicOrigin,
  POTBOT_UI_BACKEND_TOKEN:'PRIVATE-backend-token',POTBOT_UI_BACKEND_URL:'http://127.0.0.1:8765/',POTBOT_UI_WORKSPACE_ID:workspaceId,
  POTBOT_UI_SETUP_TOKEN:'PRIVATE-setup-token-'.repeat(4)};
const scope={schema:'potbot-operation-workbench/v1',workspaceId,principalId,operationId};
const contract={...scope,kind:'cog',reference:'syn://cog/example',executionReference:'syn://thing/example',name:'Example',supported:true,jobType:'cog.execute',
  inputFields:['episode_id','agent_id','repository_state_id','profile_sha256','type_ref','value'],reasonCodes:[],maxInputBytes:16384};
const job={id:jobId,episode_id:workspaceId,contract_revision_id:workspaceId,job_type:'operation.check',state:'queued',
  owner_principal_id:principalId,operation_check_id:null,update_token:0,selected_models:[],progress:{stage:'queued'},
  cancellation_requested:false,result:null,error_code:null,output_bytes:0,created_at:'2026-11-17T21:00:00+00:00',
  updated_at:'2026-11-17T21:00:00+00:00',started_at:null,finished_at:null,dispatched_at:null,heartbeat_at:null,lease_until:null};
const spec={job_type:'cog.execute',episode_id:workspaceId,selected_models:[],input:{agent_id:principalId,repository_state_id:workspaceId,
  operation_ref:'syn://thing/example',profile_sha256:'e'.repeat(64),type_ref:'syn://type/example-input',value:{query:'bounded example'}}};
const native={...job,job_type:'cog.execute',operation_check_id:checkId};
const params=(action,id=operationId,selectedJob)=>({params:Promise.resolve({id,action,
  ...((selectedJob!==undefined||['inspect','cancel','events','result','artifact'].includes(action))?{jobId:selectedJob??jobId}:{})})});
function request(method='GET',body,options={}) {
  return new Request(`${auth.publicOrigin}/api/ui/operations/${operationId}${options.query??''}`,{
    method,headers:{Cookie:cookie,'X-Potbot-Workspace':workspaceId,...(method==='POST'?{
      Origin:auth.publicOrigin,'Sec-Fetch-Site':'same-origin','Content-Type':'application/json','Idempotency-Key':'workbench:test-key'}:{}),...options.headers},
    ...(body===undefined?{}:{body:JSON.stringify(body)}),...(options.signal?{signal:options.signal}:{}),
  });
}

test('operation BFF preserves authority, exact request identity and bounded scoped reads',async t=>{
  const old={...process.env}, priorFetch=globalThis.fetch;
  Object.assign(process.env,env);delete process.env.POTBOT_UI_WORKSPACES_JSON;
  t.after(()=>{globalThis.fetch=priorFetch;for(const key of [...Object.keys(env),'POTBOT_UI_WORKSPACES_JSON']) {
    if(old[key]===undefined)delete process.env[key];else process.env[key]=old[key];
  }});

  await t.test('rejects unauthorized origins, workspaces, methods, identifiers and queries before network access',async()=>{
    let calls=0;globalThis.fetch=async()=>{calls++;throw new Error('unexpected network');};
    assert.equal((await describe(new Request(auth.publicOrigin+'/api/ui/operations'),params())).status,401);
    assert.equal((await submit(request('POST',spec,{headers:{Origin:'https://untrusted.example'}}),params('checks'))).status,403);
    assert.equal((await submit(request('POST',spec,{headers:{'Sec-Fetch-Site':'cross-site'}}),params('checks'))).status,403);
    assert.equal((await describe(request('GET',undefined,{headers:{'X-Potbot-Workspace':principalId}}),params())).status,503);
    assert.equal((await describe(request(),params('', '../execute'))).status,400);
    assert.equal((await inspect(request(),params('',operationId,'not-a-job'))).status,400);
    assert.equal((await submit(request('POST',spec),params('execute'))).status,400);
    assert.equal((await readJob(request(),params('cancel'))).status,400);
    assert.equal((await cancel(request('POST',{confirm:true}),params('result'))).status,400);
    assert.equal((await describe(request('GET',undefined,{query:'?path=/execute'}),params())).status,400);
    assert.equal((await readJob(request('GET',undefined,{query:'?after=1&after=2'}),params('events'))).status,400);
    assert.equal((await readJob(request('GET',undefined,{query:'?limit=101'}),params('events'))).status,400);
    assert.equal((await readJob(request('GET',undefined,{query:'?after=9007199254740992'}),params('events'))).status,400);
    assert.equal((await operationWorkbenchRoute(request('PUT'),{id:operationId,action:'contract'})).status,400);
    assert.equal(calls,0);
  });

  await t.test('contract read is passive and strips everything outside the fixed public projection',async()=>{
    globalThis.fetch=async(url,options)=>{
      assert.equal(url,`http://127.0.0.1:8765/v1/ui/operations/${operationId}/contract`);
      assert.equal(options.method,'GET');assert.equal(options.headers.Authorization,'Bearer PRIVATE-backend-token');
      assert.equal(options.headers['X-Potbot-Setup-Token'],undefined);assert.equal(options.cache,'no-store');
      return Response.json({...contract,input:{secret:'PRIVATE'},token:'PRIVATE'});
    };
    const response=await describe(request(),params());
    assert.equal(response.status,200);assert.equal(response.headers.get('Cache-Control'),'private, no-store');
    assert.deepEqual(await response.json(),contract);
    globalThis.fetch=async()=>Response.json({...contract,workspaceId:principalId});
    assert.equal((await describe(request(),params())).status,502);
  });

  await t.test('native check queues the exact scoped body with server-only setup permission and stable key',async()=>{
    globalThis.fetch=async(url,options)=>{
      assert.equal(url,`http://127.0.0.1:8765/v1/ui/operations/${operationId}/checks`);
      assert.equal(options.method,'POST');assert.equal(options.headers['Idempotency-Key'],'workbench:test-key');
      assert.equal(options.headers['X-Potbot-Setup-Token'],env.POTBOT_UI_SETUP_TOKEN);
      assert.equal(options.headers.Origin,auth.publicOrigin);assert.deepEqual(JSON.parse(options.body),spec);
      return Response.json({...scope,job:{...job,input:{secret:'PRIVATE'},operation_authorization:{token:'PRIVATE'}}},{status:202});
    };
    const response=await submit(request('POST',spec),params('checks'));
    assert.equal(response.status,202);assert.deepEqual(await response.json(),{...scope,job});
  });

  await t.test('launch requires explicit confirmation and the returned job must match task, type and consumed check',async()=>{
    const body={checkJobId:checkId,request:spec,confirm:true};
    globalThis.fetch=async(url,options)=>{
      assert.equal(url,`http://127.0.0.1:8765/v1/ui/operations/${operationId}/runs`);
      assert.deepEqual(JSON.parse(options.body),body);return Response.json({...scope,job:native},{status:202});
    };
    const response=await submit(request('POST',body),params('runs'));assert.equal(response.status,202);
    assert.deepEqual(await response.json(),{...scope,job:native});
    for(const wrong of [{...native,episode_id:principalId},{...native,operation_check_id:jobId},job]) {
      globalThis.fetch=async()=>Response.json({...scope,job:wrong});
      assert.equal((await submit(request('POST',body),params('runs'))).status,502);
    }
    globalThis.fetch=async()=>Response.json({...scope,job:native});
    assert.equal((await submit(request('POST',spec),params('checks'))).status,502);
  });

  await t.test('hosted checks preserve the selected single model and reject hidden extra inputs',async()=>{
    const hosted={job_type:'model.generate',episode_id:workspaceId,selected_models:[{provider:'openai',model:'example/model',role:'coding'}],
      input:{prompt:'An exact prompt',max_output_tokens:1024,timeout_seconds:60}};
    let calls=0;globalThis.fetch=async(_url,options)=>{calls++;assert.deepEqual(JSON.parse(options.body),hosted);return Response.json({...scope,job});};
    assert.equal((await submit(request('POST',hosted),params('checks'))).status,202);
    assert.equal((await submit(request('POST',{...hosted,selected_models:[...hosted.selected_models,...hosted.selected_models]}),params('checks'))).status,400);
    assert.equal((await submit(request('POST',{...hosted,input:{...hosted.input,api_key:'PRIVATE'}}),params('checks'))).status,400);
    assert.equal((await submit(request('POST',{...hosted,input:{...hosted.input,prompt:'a'.repeat(16200)}}),params('checks'))).status,400);
    assert.equal(calls,1);
  });

  await t.test('mutation validation rejects malformed and oversized input before contacting the queue',async()=>{
    let calls=0;globalThis.fetch=async()=>{calls++;throw new Error('unexpected network');};
    for(const options of [{headers:{'Idempotency-Key':'bad/key'}},{headers:{'Idempotency-Key':''}},{headers:{'Content-Type':'text/plain'}}])
      assert.equal((await submit(request('POST',spec,options),params('checks'))).status,400);
    assert.equal((await submit(request('POST',{...spec,input:{...spec.input,value:'x'.repeat(65_537)}}),params('checks'))).status,400);
    assert.equal((await submit(request('POST',{checkJobId:checkId,request:spec,confirm:false}),params('runs'))).status,400);
    assert.equal((await submit(request('POST',{checkJobId:checkId,request:spec,confirm:true,secret:'PRIVATE'}),params('runs'))).status,400);
    assert.equal((await cancel(request('POST',{confirm:true,revision:2}),params('cancel'))).status,400);
    delete process.env.POTBOT_UI_SETUP_TOKEN;
    assert.equal((await submit(request('POST',spec),params('checks'))).status,503);
    process.env.POTBOT_UI_SETUP_TOKEN=env.POTBOT_UI_SETUP_TOKEN;
    assert.equal(calls,0);
  });

  await t.test('cancellation remains available without launch permission and confirms the exact owned job',async()=>{
    delete process.env.POTBOT_UI_SETUP_TOKEN;
    const cancelled={...native,state:'cancelled',cancellation_requested:true,update_token:1};
    globalThis.fetch=async(url,options)=>{
      assert.equal(url,`http://127.0.0.1:8765/v1/ui/operations/${operationId}/jobs/${jobId}/cancel`);
      assert.equal(options.headers['X-Potbot-Setup-Token'],undefined);assert.equal(options.headers.Origin,auth.publicOrigin);
      assert.deepEqual(JSON.parse(options.body),{confirm:true});return Response.json({...scope,job:cancelled});
    };
    const response=await cancel(request('POST',{confirm:true}),params('cancel'));
    assert.equal(response.status,200);assert.deepEqual(await response.json(),{...scope,job:cancelled});
    process.env.POTBOT_UI_SETUP_TOKEN=env.POTBOT_UI_SETUP_TOKEN;
  });

  await t.test('owned job and event reads reject foreign identity and do not expose nested authority',async()=>{
    globalThis.fetch=async(url)=>{assert.equal(url,`http://127.0.0.1:8765/v1/ui/operations/${operationId}/jobs/${jobId}`);return Response.json({...scope,job:native});};
    assert.equal((await inspect(request(),params('inspect'))).status,200);
    for(const wrong of [{...native,id:checkId},{...native,owner_principal_id:workspaceId}]) {
      globalThis.fetch=async()=>Response.json({...scope,job:wrong});assert.equal((await inspect(request(),params('inspect'))).status,502);
    }
    const event={sequence:5,id:checkId,job_id:jobId,event_type:'job.output',recorded_at:job.updated_at,
      data:{text:'Retained text',provider:'openai',model:'example/model',role:'coding',input:'PRIVATE',authorization:'PRIVATE'}};
    globalThis.fetch=async(url)=>{
      assert.equal(url,`http://127.0.0.1:8765/v1/ui/operations/${operationId}/jobs/${jobId}/events?after=4&limit=25`);
      return Response.json({...scope,items:[event],next_cursor:5,has_more:false});
    };
    const response=await readJob(request('GET',undefined,{query:'?after=4&limit=25'}),params('events'));
    assert.equal(response.status,200);assert.doesNotMatch(await response.text(),/PRIVATE/);
    globalThis.fetch=async()=>Response.json({...scope,items:[{...event,job_id:checkId}],next_cursor:5,has_more:false});
    assert.equal((await readJob(request(),params('events'))).status,502);
  });

  await t.test('result text and verified artifact ranges retain bounded data without paths or credentials',async()=>{
    const artifact={id:principalId,sha256:'d'.repeat(64),bytes:4,mediaType:'application/json'};
    globalThis.fetch=async()=>Response.json({...scope,job:native,output:'Text',truncated:false,artifact:{...artifact,path:'/PRIVATE'},artifactText:'{}',artifactTruncated:false});
    const result=await readJob(request(),params('result'));assert.equal(result.status,200);assert.doesNotMatch(await result.text(),/PRIVATE/);
    globalThis.fetch=async(url)=>{
      assert.equal(url,`http://127.0.0.1:8765/v1/ui/operations/${operationId}/jobs/${jobId}/artifact?offset=2&length=2`);
      return Response.json({...scope,artifact:{...artifact,path:'/PRIVATE'},offset:2,dataBase64:btoa('cd'),nextOffset:null});
    };
    const response=await readJob(request('GET',undefined,{query:'?offset=2&length=2'}),params('artifact'));
    assert.equal(response.status,200);assert.deepEqual(await response.json(),{...scope,artifact,offset:2,dataBase64:btoa('cd'),nextOffset:null});
    for(const query of ['?length=0','?length=262145','?offset=-1','?offset=9007199254740992','?offset=0&offset=1','?path=/private'])
      assert.equal((await readJob(request('GET',undefined,{query}),params('artifact'))).status,400);
    globalThis.fetch=async()=>Response.json({...scope,artifact,offset:0,dataBase64:btoa('ab'),nextOffset:3});
    assert.equal((await readJob(request(),params('artifact'))).status,502);
  });

  await t.test('known conflicts retain409 and unavailable/invalid responses never reflect backend secrets',async()=>{
    for(const status of [400,401,403,404,409,500,503]) {
      globalThis.fetch=async()=>Response.json({error:'PRIVATE backend diagnostic'},{status});
      const response=await submit(request('POST',spec),params('checks'));
      assert.equal(response.status,[400,404,409,503].includes(status)?status:502);
      assert.doesNotMatch(await response.text(),/PRIVATE/);
    }
    globalThis.fetch=async()=>{throw new Error('PRIVATE connection detail');};
    const failed=await submit(request('POST',spec),params('checks'));assert.equal(failed.status,503);assert.doesNotMatch(await failed.text(),/PRIVATE/);
    globalThis.fetch=async()=>Response.json({...scope,job:{...job,owner_principal_id:workspaceId}});
    assert.equal((await submit(request('POST',spec),params('checks'))).status,502);
  });

  await t.test('aborting a changed browser scope cancels the upstream request',async()=>{
    const controller=new AbortController();let ready;
    const started=new Promise(resolve=>{ready=resolve;});
    globalThis.fetch=async(_url,options)=>new Promise((_resolve,reject)=>{
      options.signal.addEventListener('abort',()=>reject(options.signal.reason),{once:true});ready();
    });
    const pending=describe(request('GET',undefined,{signal:controller.signal}),params());
    await started;controller.abort();assert.equal((await pending).status,503);
  });
});

test('bounded workbench streaming stops at the limit and rejects malformed UTF-8/JSON',async()=>{
  let cancelled=false,reads=0;
  const response=new Response(new ReadableStream({pull(controller){reads++;controller.enqueue(new Uint8Array(600_000));},cancel(){cancelled=true;}}));
  await assert.rejects(workbenchJSON(response),/exceeds bound/);assert.equal(cancelled,true);assert.ok(reads<=3);
  await assert.rejects(workbenchJSON(new Response(new Uint8Array([0xff,0xfe]))));
  await assert.rejects(workbenchJSON(new Response('{invalid')));
});
