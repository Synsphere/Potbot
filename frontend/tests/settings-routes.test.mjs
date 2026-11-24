import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {settingsRoute} from "../src/lib/server/settings.ts";
import {GET,POST} from "../src/app/api/ui/settings/[...path]/route.ts";

const wire=JSON.parse(readFileSync(new URL("./fixtures/settings-http.json",import.meta.url),"utf8")),w=wire.catalogue.workspaceId;
const environment={POTBOT_UI_COOKIE_SECRET:"s".repeat(64),POTBOT_UI_PUBLIC_ORIGIN:"http://localhost:3100",POTBOT_UI_BACKEND_TOKEN:"PRIVATE-backend-token",POTBOT_UI_BACKEND_URL:"http://127.0.0.1:8765/",POTBOT_UI_WORKSPACE_ID:w,POTBOT_UI_SETUP_TOKEN:"PRIVATE-setup-".repeat(5)};
const params=path=>({params:Promise.resolve({path:path.split("/")})});
const request=(method="GET",body,patch={})=>new Request("http://localhost:3100/api/ui/settings/catalogue"+(patch.query??""),{method,headers:{"X-Potbot-Workspace":w,...(method==="POST"?{Origin:environment.POTBOT_UI_PUBLIC_ORIGIN,"Content-Type":"application/json"}:{}),...patch.headers},...(body?{body:JSON.stringify(body)}:{}),...(patch.signal?{signal:patch.signal}:{})});

test("settings bridge confines reads and owner requests to the allowlisted workspace and exact resource",async t=>{
  const saved={...process.env},fetch=globalThis.fetch;Object.assign(process.env,environment);delete process.env.POTBOT_UI_WORKSPACES_JSON;
  t.after(()=>{globalThis.fetch=fetch;for(const key of [...Object.keys(environment),"POTBOT_UI_WORKSPACES_JSON"])if(saved[key]===undefined)delete process.env[key];else process.env[key]=saved[key];});
  await t.test("bad scope, origin, paths, methods and caller URLs reject before transport",async()=>{
    let calls=0;globalThis.fetch=async()=>{calls++;throw Error();};
    for(const path of ["../catalogue","catalogue/extra","execute","https:"])assert.equal((await GET(request(),params(path))).status,400);
    assert.equal((await GET(request("GET",null,{query:"?url=http://private.invalid"}),params("catalogue"))).status,400);
    assert.equal((await GET(request("GET",null,{headers:{"X-Potbot-Workspace":"99999999-9999-4999-8999-999999999999"}}),params("catalogue"))).status,503);
    assert.equal((await GET(request("GET",null,{headers:{Origin:"https://foreign.invalid"}}),params("catalogue"))).status,403);
    assert.equal((await settingsRoute(request("POST",{}),["catalogue"])).status,405);assert.equal(calls,0);
  });
  await t.test("reads strip private fields and never send setup authority",async()=>{
    for(const resource of ["catalogue","effective","models","tools","swarm"]){
      globalThis.fetch=async(url,options)=>{assert.ok(url.startsWith("http://127.0.0.1:8765/v1/ui/"));assert.equal(options.headers.Authorization,"Bearer PRIVATE-backend-token");assert.equal(options.headers["X-Potbot-Setup-Token"],undefined);assert.equal(options.redirect,"error");return Response.json({...wire[resource],privateToken:"PRIVATE"});};
      const response=await GET(request(),params(resource));assert.equal(response.status,200,resource);assert.equal(response.headers.get("Cache-Control"),"private, no-store");assert.ok(!(await response.text()).includes("PRIVATE"));
    }
  });
  await t.test("exact requests require explicit confirmation and separate setup authority",async()=>{
    const body={requestId:"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",grantId:wire.swarm.grants[0].id,expectedRevision:1,expectedGeneration:1,grantSha256:"d".repeat(64),confirm:true};
    globalThis.fetch=async(url,options)=>{assert.equal(url,"http://127.0.0.1:8765/v1/ui/swarm/revocations");assert.equal(options.headers["X-Potbot-Setup-Token"],environment.POTBOT_UI_SETUP_TOKEN);assert.equal(options.headers.Origin,environment.POTBOT_UI_PUBLIC_ORIGIN);assert.deepEqual(JSON.parse(options.body),body);return Response.json({schema:"potbot-settings/v1",workspaceId:w,principalId:wire.catalogue.principalId,requestId:body.requestId,state:"requested"});};
    assert.equal((await POST(request("POST",body),params("requests"))).status,202);
    for(const patch of [{confirm:false},{argv:["sh"]},{requestId:"bad"},{expectedRevision:0}])assert.equal((await POST(request("POST",{...body,...patch}),params("requests"))).status,400);
    assert.equal((await POST(request("POST",body,{headers:{Origin:"https://foreign.invalid"}}),params("requests"))).status,403);
    delete process.env.POTBOT_UI_SETUP_TOKEN;assert.equal((await POST(request("POST",body),params("requests"))).status,503);process.env.POTBOT_UI_SETUP_TOKEN=environment.POTBOT_UI_SETUP_TOKEN;
  });
  await t.test("foreign or oversized responses, leaked errors and aborts never hydrate as evidence",async()=>{
    globalThis.fetch=async()=>Response.json({...wire.models,workspaceId:"99999999-9999-4999-8999-999999999999"});assert.equal((await GET(request(),params("models"))).status,502);
    globalThis.fetch=async()=>new Response(JSON.stringify({...wire.catalogue,padding:"x".repeat(524288)}));assert.equal((await GET(request(),params("catalogue"))).status,503);
    globalThis.fetch=async()=>new Response("PRIVATE connection details",{status:500});const response=await GET(request(),params("swarm"));assert.ok(!(await response.text()).includes("PRIVATE"));
    const controller=new AbortController();controller.abort();globalThis.fetch=async(_url,options)=>{assert.equal(options.signal.aborted,true);throw Error("aborted");};assert.equal((await GET(request("GET",null,{signal:controller.signal}),params("catalogue"))).status,503);
  });
});
