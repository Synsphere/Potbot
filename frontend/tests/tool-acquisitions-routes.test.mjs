import test from 'node:test';
import assert from 'node:assert/strict';
import { sessionCookie } from '../src/lib/server/security.ts';
import { toolAcquisitionRoute } from '../src/lib/server/tool-acquisition.ts';
import { GET as list, POST as requestSource } from '../src/app/api/ui/tools/acquisitions/route.ts';
import { GET as read, POST as write } from '../src/app/api/ui/tools/acquisitions/[...path]/route.ts';
import { workspaceId,principalId,operationId,recipeId,scope,recipe,queued,reviewed,tested,released,events } from './tool-acquisition-fixtures.mjs';

const auth={password:'operator-password-very-long',signingKey:'a'.repeat(64),publicOrigin:'http://localhost:3100'},cookie=sessionCookie(auth).split(';')[0];
const env={POTBOT_UI_PASSWORD:auth.password,POTBOT_UI_COOKIE_SECRET:auth.signingKey,POTBOT_UI_PUBLIC_ORIGIN:auth.publicOrigin,
  POTBOT_UI_BACKEND_TOKEN:'PRIVATE-backend-token',POTBOT_UI_BACKEND_URL:'http://127.0.0.1:8765/',POTBOT_UI_WORKSPACE_ID:workspaceId,POTBOT_UI_SETUP_TOKEN:'PRIVATE-setup-token-'.repeat(4)};
const params=path=>({params:Promise.resolve({path:path.split('/')})}),envelope=operation=>({...scope,operation});
const sourceBody={recipeId,commit:queued.newCommit,confirm:true};
function request(method='GET',body,options={}) {
  return new Request(`${auth.publicOrigin}/api/ui/tools/acquisitions${options.query??''}`,{method,
    headers:{Cookie:cookie,'X-Potbot-Workspace':workspaceId,...(method==='POST'?{Origin:auth.publicOrigin,'Sec-Fetch-Site':'same-origin','Content-Type':'application/json','Idempotency-Key':'tool:test-key'}:{}),...options.headers},
    ...(body===undefined?{}:{body:JSON.stringify(body)}),...(options.signal?{signal:options.signal}:{})});
}

test('acquisition BFF confines source, build and host controls to authenticated fixed scoped routes',async t=>{
  const prior={...process.env},priorFetch=globalThis.fetch;Object.assign(process.env,env);delete process.env.POTBOT_UI_WORKSPACES_JSON;
  t.after(()=>{globalThis.fetch=priorFetch;for(const key of [...Object.keys(env),'POTBOT_UI_WORKSPACES_JSON'])if(prior[key]===undefined)delete process.env[key];else process.env[key]=prior[key];});
  await t.test('authentication, exact origin, workspace and method gates run before network access',async()=>{
    let calls=0;globalThis.fetch=async()=>{calls++;throw Error('unexpected');};
    assert.equal((await list(new Request('http://remote.invalid:3100'+'/api/ui/tools/acquisitions'))).status, 403);
    assert.equal((await list(request('GET',undefined,{headers:{Origin:'https://evil.example'}}))).status,403);
    assert.equal((await list(request('GET',undefined,{headers:{'X-Potbot-Workspace':principalId}}))).status,503);
    assert.equal((await toolAcquisitionRoute(request('PUT'),[])).status,400);
    for(const path of ['execute','recipes/publish',`${operationId}/run`,`${operationId}/approve/extra`,'../approve'])assert.equal((await write(request('POST',{}),params(path))).status,400,path);
    for(const extra of [{Origin:'https://evil.example'},{Origin:''},{'Sec-Fetch-Site':'cross-site'}])assert.equal((await requestSource(request('POST',sourceBody,{headers:extra}))).status,403);
    assert.equal((await read(request(),params(`${operationId}/approve`))).status,400);assert.equal(calls,0);
  });
  await t.test('bounded page and event query parsing refuses ambiguity and caller-selected upstreams',async()=>{
    let calls=0;globalThis.fetch=async()=>{calls++;throw Error('unexpected');};
    for(const query of ['?after=not-uuid','?limit=51','?limit=0','?limit=1&limit=2','?url=https://evil.example','?after=../escape'])assert.equal((await list(request('GET',undefined,{query}))).status,400,query);
    for(const query of ['?after=-1','?after=01','?after=9007199254740992','?after=1&after=2','?limit=51'])assert.equal((await read(request('GET',undefined,{query}),params(`${operationId}/events`))).status,400,query);
    assert.equal((await read(request('GET',undefined,{query:'?limit=1'}),params(operationId))).status,400);
    assert.equal((await requestSource(request('POST',sourceBody,{query:'?repository=https://evil.example'}))).status,400);assert.equal(calls,0);
  });
  await t.test('passive reads retain public scope while stripping worker authority and omitting setup credentials',async()=>{
    const result={...scope,items:[queued],nextCursor:null};
    globalThis.fetch=async(url,options)=>{
      assert.equal(url,'http://127.0.0.1:8765/v1/ui/tools/acquisitions?limit=25');assert.equal(options.method,'GET');
      assert.equal(options.headers.Authorization,'Bearer PRIVATE-backend-token');assert.equal(options.headers['X-Potbot-Setup-Token'],undefined);
      assert.equal(options.redirect,'error');assert.equal(options.cache,'no-store');return Response.json({...result,workerToken:'PRIVATE'});
    };
    const response=await list(request());assert.equal(response.status,200);assert.equal(response.headers.get('Cache-Control'),'private, no-store');assert.deepEqual(await response.json(),result);
    globalThis.fetch=async(url)=>{assert.equal(url,'http://127.0.0.1:8765/v1/ui/tools/acquisitions/recipes?limit=25');return Response.json({...scope,items:[{...recipe,approval_mac:'PRIVATE'}],nextCursor:null});};
    assert.deepEqual(await (await read(request(),params('recipes'))).json(),{...scope,items:[recipe],nextCursor:null});
    globalThis.fetch=async()=>Response.json({...envelope(tested),operation:{...tested,execution_contract:{privatePath:'/secret'},worker_token:'PRIVATE'}});
    assert.deepEqual(await (await read(request(),params(operationId))).json(),envelope(tested));
    for(const value of [{...envelope(tested),workspaceId:principalId},envelope({...tested,id:recipeId}),envelope({...tested,ownerPrincipalId:workspaceId})]) {
      globalThis.fetch=async()=>Response.json(value);assert.equal((await read(request(),params(operationId))).status,502);
    }
  });
  await t.test('source request forwards only the immutable recipe and commit with server-only capability and stable key',async()=>{
    globalThis.fetch=async(url,options)=>{
      assert.equal(url,'http://127.0.0.1:8765/v1/ui/tools/acquisitions');assert.deepEqual(JSON.parse(options.body),sourceBody);
      assert.equal(options.headers['Idempotency-Key'],'tool:test-key');assert.equal(options.headers['X-Potbot-Setup-Token'],env.POTBOT_UI_SETUP_TOKEN);
      assert.equal(options.headers.Origin,auth.publicOrigin);return Response.json({...envelope(queued),setup_token:'PRIVATE'},{status:202});
    };
    const response=await requestSource(request('POST',sourceBody));assert.equal(response.status,202);assert.deepEqual(await response.json(),envelope(queued));
    for(const patch of [{newCommit:'b'.repeat(40)},{revision:2},{phase:'sourcing'},{recipe:{...recipe,id:operationId}}]) {
      globalThis.fetch=async()=>Response.json(envelope({...queued,...patch}),{status:202});assert.equal((await requestSource(request('POST',sourceBody))).status,502);
    }
  });
  await t.test('mutations require exact bounded keys, capability and body fields without host commands or executable pins',async()=>{
    let calls=0;globalThis.fetch=async()=>{calls++;throw Error('unexpected');};
    for(const patch of [{commit:'main'},{confirm:false},{argv:['/bin/sh']},{repository:recipe.repository},{executablePins:{}}])assert.equal((await requestSource(request('POST',{...sourceBody,...patch}))).status,400);
    const body={revision:reviewed.revision,reviewSha256:reviewed.review.sha256,confirm:true};
    for(const patch of [{revision:'3'},{revision:-1},{confirm:false},{reviewSha256:null},{bindingSha256:reviewed.bindingSha256},{buildRoot:'/host/path'}])assert.equal((await write(request('POST',{...body,...patch}),params(`${operationId}/approve-build`))).status,400);
    for(const key of ['', 'with spaces','x'.repeat(129)])assert.equal((await requestSource(request('POST',sourceBody,{headers:{'Idempotency-Key':key}}))).status,400);
    assert.equal((await requestSource(request('POST',sourceBody,{headers:{'Content-Type':'text/plain'}}))).status,400);
    delete process.env.POTBOT_UI_SETUP_TOKEN;assert.equal((await requestSource(request('POST',sourceBody))).status,503);process.env.POTBOT_UI_SETUP_TOKEN=env.POTBOT_UI_SETUP_TOKEN;assert.equal(calls,0);
  });
  await t.test('build and tested approval receipts bind the exact action, prior revision and evidence',async()=>{
    const body={revision:reviewed.revision,reviewSha256:reviewed.review.sha256,confirm:true},approved={...reviewed,revision:4,phase:'build_queued',allowedActions:['cancel']};
    globalThis.fetch=async(url,options)=>{assert.equal(url,`http://127.0.0.1:8765/v1/ui/tools/acquisitions/${operationId}/approve-build`);assert.deepEqual(JSON.parse(options.body),body);return Response.json(envelope(approved));};
    assert.equal((await write(request('POST',body),params(`${operationId}/approve-build`))).status,200);
    globalThis.fetch=async()=>Response.json(envelope(tested));assert.equal((await write(request('POST',body),params(`${operationId}/approve-build`))).status,502);
    for(const [action,phase] of [['approve','approved'],['reject','rejected']]) {
      const result={...tested,revision:tested.revision+1,phase,allowedActions:[]};globalThis.fetch=async()=>Response.json(envelope(result));
      const decision={revision:tested.revision,evidenceSha256:tested.evidenceSha256,confirm:true};
      assert.equal((await write(request('POST',decision),params(`${operationId}/${action}`))).status,200);
      assert.equal((await write(request('POST',{...decision,evidenceSha256:'0'.repeat(64)}),params(`${operationId}/${action}`))).status,502);
    }
  });
  await t.test('host actions return queued readback requests and cancellation preserves pretest versus tested state',async()=>{
    const body={revision:released.revision,bindingSha256:released.bindingSha256,confirm:true};
    for(const action of ['rollback','reconcile','repin','postflight']) {
      const result={...released,revision:released.revision+1,hostAction:{action,status:'queued'},allowedActions:[]};
      globalThis.fetch=async()=>Response.json(envelope(result));assert.equal((await write(request('POST',body),params(`${operationId}/${action}`))).status,200);
      globalThis.fetch=async()=>Response.json(envelope({...result,hostAction:{action,status:'running'}}));assert.equal((await write(request('POST',body),params(`${operationId}/${action}`))).status,502);
    }
    for(const [operation,phase] of [[queued,'cancelled'],[{...tested,phase:'approved'},'rejected']]) {
      globalThis.fetch=async()=>Response.json(envelope({...operation,revision:operation.revision+1,phase,allowedActions:[]}));
      assert.equal((await write(request('POST',{revision:operation.revision,bindingSha256:operation.bindingSha256,confirm:true}),params(`${operationId}/cancel`))).status,200);
    }
  });
  await t.test('events remain bound to their operation and exact ordered cursor',async()=>{
    globalThis.fetch=async(url)=>{assert.equal(url,`http://127.0.0.1:8765/v1/ui/tools/acquisitions/${operationId}/events?limit=50&after=0`);return Response.json(events);};
    assert.deepEqual(await (await read(request(),params(`${operationId}/events`))).json(),events);
    for(const patch of [{operationId:recipeId},{nextCursor:99},{items:[events.items[1],events.items[0]]}]) {
      globalThis.fetch=async()=>Response.json({...events,...patch});assert.equal((await read(request(),params(`${operationId}/events`))).status,502);
    }
    globalThis.fetch=async()=>Response.json({...events,items:[],nextCursor:events.nextCursor});
    assert.equal((await read(request('GET',undefined,{query:'?after=2'}),params(`${operationId}/events`))).status,200);
  });
  await t.test('streamed body limits, request cancellation and redacted upstream errors preserve uncertain outcomes',async()=>{
    let calls=0;globalThis.fetch=async()=>{calls++;throw Error('unexpected');};
    const oversized={...sourceBody,padding:'a'.repeat(8192)};
    assert.equal((await requestSource(request('POST',oversized))).status,400);assert.equal(calls,0);
    const controller=new AbortController();let signal;
    globalThis.fetch=async(_url,options)=>{signal=options.signal;return Response.json(envelope(reviewed));};
    await read(request('GET',undefined,{signal:controller.signal}),params(operationId));controller.abort();assert.equal(signal.aborted,true);
    globalThis.fetch=async()=>new Response('SECRET'.repeat(400000));assert.equal((await list(request())).status,503);
    for(const [upstream,status] of [[400,400],[409,409],[404,404],[401,502],[500,502],[503,503]]) {
      globalThis.fetch=async()=>new Response('PRIVATE host path and token',{status:upstream});const response=await list(request());assert.equal(response.status,status);assert.doesNotMatch(await response.text(),/PRIVATE|token/);
    }
    globalThis.fetch=async()=>{throw Error('PRIVATE backend failure');};const response=await requestSource(request('POST',sourceBody));assert.equal(response.status,503);assert.doesNotMatch(await response.text(),/PRIVATE/);
  });
});
