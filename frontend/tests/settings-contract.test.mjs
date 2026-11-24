import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {resolve} from "node:path";
import {projectCatalogue,projectEffective,projectInventory,projectSwarm,projectRevocationReceipt,revocationReview} from "../src/lib/settings.ts";
import {preferenceChanges,rebasePreferences,filterInventory,revocationIntent} from "../src/lib/settings-client.ts";

const fixture=process.env.POTBOT_SETTINGS_HTTP_FIXTURE?resolve(process.env.POTBOT_SETTINGS_HTTP_FIXTURE):new URL("./fixtures/settings-http.json",import.meta.url);
const wire=JSON.parse(readFileSync(fixture,"utf8"));
const w=wire.catalogue.workspaceId,other="99999999-9999-4999-8999-999999999999";
const project={catalogue:value=>projectCatalogue(value,w),effective:value=>projectEffective(value,w),models:value=>projectInventory(value,w,"models"),tools:value=>projectInventory(value,w,"tools"),swarm:value=>projectSwarm(value,w)};

test("the complete scoped HTTP contract projects without invented execution or node readiness",()=>{
  for(const [kind,fn] of Object.entries(project)){const value=fn(wire[kind]);assert.ok(value,kind);assert.equal(value.workspaceId,w);assert.equal(fn({...wire[kind],workspaceId:other}),null,kind);}
  assert.equal(project.catalogue(wire.catalogue).descriptors.length,17);
  assert.equal(project.catalogue(wire.catalogue).features.length,29);
  assert.equal(project.effective(wire.effective).liveLayersAvailable,false);
  assert.equal(project.models(wire.models).runtimeProbed,false);
  assert.equal(project.swarm(wire.swarm).nativeNodeActionsAvailable,false);
});
test("unknown upstream fields are discarded instead of hydrating credentials or native paths",()=>{
  for(const [kind,fn] of Object.entries(project)){
    const value=structuredClone(wire[kind]);value.nativeToken="PRIVATE";
    if(value.items?.length)value.items[0].absolutePath="/PRIVATE";
    assert.ok(!JSON.stringify(fn(value)).includes("PRIVATE"),kind);
  }
});
test("model/tool discovery cannot pretend to have qualification, runtime identity or rights",()=>{
  for(const patch of [{qualification:"qualified"},{runtime:"healthy"},{permission:"granted"},{location:"../private"},{location:"C:/private"},{origin:"unknown"}]){
    const value=structuredClone(wire.models);value.items=[{...value.items[0],...patch}];assert.equal(project.models(value),null,JSON.stringify(patch));
  }
  const duplicate=structuredClone(wire.models);duplicate.items.push(duplicate.items[0]);assert.equal(project.models(duplicate),null);
  assert.equal(project.models({...wire.models,runtimeProbed:true}),null);
  assert.equal(project.swarm({...wire.swarm,liveCapacityAvailable:true}),null);
});
test("descriptor coverage cannot expand authority or accept a private external link",()=>{
  const value=structuredClone(wire.catalogue);value.descriptors[0].editable=true;assert.equal(project.catalogue(value),null);
  value.descriptors=wire.catalogue.descriptors;value.categories[0].path="//private.invalid";assert.equal(project.catalogue(value),null);
  assert.equal(project.catalogue({...wire.catalogue,layerEditing:true}),null);
});
test("preference conflict rebase preserves unrelated concurrent choices and identifies overlap",()=>{
  const base={theme:"system",graphDisplay:"3d",reducedMotion:false},draft={...base,theme:"dark"};
  assert.deepEqual(preferenceChanges(base,draft),["theme"]);
  assert.deepEqual(rebasePreferences(base,draft,{...base,graphDisplay:"list"}),{settings:{theme:"dark",graphDisplay:"list",reducedMotion:false},conflicts:[]});
  assert.deepEqual(rebasePreferences(base,draft,{...base,theme:"light"}).conflicts,["theme"]);
});
test("inventory filtering uses observed presence rather than treating registry records as installed",()=>{
  const rows=wire.models.items,record={...rows[0],id:other,origin:"deployment_record",presence:"not_observed",qualification:"qualified"};
  assert.equal(filterInventory([...rows,record],"","observed").length,rows.filter(item=>item.presence==="observed").length);
  assert.deepEqual(filterInventory([record],"","records"),[record]);
  assert.equal(filterInventory(rows,"no-such-model","all").length,0);
});
test("revocation resumes the original pending request without replaying a new effect",()=>{
  const grant={id:other,sourceWorkspaceId:w,revision:4,revocationGeneration:5,sha256:"a".repeat(64),permissions:["workspace.inspect"],expiresAt:"2026-12-31T00:00:00Z",state:"active",canRequestRevocation:true};
  const old="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",fresh="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const body=revocationIntent(grant,[{requestId:old,grantId:grant.id,expectedRevision:4,state:"requested",recordedAt:"2026-11-24T00:00:00Z"}],fresh);
  assert.equal(body.requestId,old);assert.ok(revocationReview(body));
  assert.equal(revocationReview({...body,argv:["sh"]}),null);assert.equal(revocationReview({...body,confirm:false}),null);
  const receipt={...wire.catalogue,requestId:old,state:"requested"};
  assert.ok(projectRevocationReceipt(receipt,w,old));assert.equal(projectRevocationReceipt(receipt,w,fresh),null);
});

test("private setting values and kind confusion fail the projection contract",()=>{
  for(const kind of ["secret_refs","source_access"]){
    const data=structuredClone(wire.effective);
    data.defaults.find(row=>row.id==="configuration."+kind).value="secret://private-record";
    assert.equal(project.effective(data),null);
    const catalogue=structuredClone(wire.catalogue);
    catalogue.descriptors.find(row=>row.id==="configuration."+kind).defaultValue="/private/path";
    assert.equal(project.catalogue(catalogue),null);
  }
  const models=structuredClone(wire.models);
  models.items[0].presence="not_observed";
  assert.equal(project.models(models),null);
  assert.equal(project.tools({...wire.models,kind:"tools"}),null);
  const tools=structuredClone(wire.tools);
  tools.items[0].qualification="qualified";
  assert.equal(project.tools(tools),null);
});
