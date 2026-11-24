/** Bounded, path-free projections. Inventory and registration never imply execution rights. */
export const SETTINGS_SECTIONS = ["workspace","appearance","models","tools","swarm","execution","context","research","applications","storage","security","services","diagnostics","updates"] as const;
export type SettingsSection = typeof SETTINGS_SECTIONS[number];
export type SettingsResource = "catalogue" | "effective" | "models" | "tools" | "swarm" | "requests";
export type Scope = { schema: "potbot-settings/v1"; workspaceId: string; principalId: string };
export type Descriptor = { id: string; label: string; category: SettingsSection; kind: string; scopes: string[]; defaultValue: string; editable: boolean; apply: string; owner: string; reason: string; options: (string | number)[] };
export type Catalogue = Scope & { categories: { id: SettingsSection; label: string; path: string }[]; features: { id: string; label: string; category: SettingsSection; description: string }[]; descriptors: Descriptor[]; layers: string[]; layerEditing: false };
export type SettingValue = { id: string; value: string };
export type Effective = Scope & { liveLayersAvailable: false; defaults: SettingValue[]; latestRecordedSnapshot: { id: string; sha256: string; recordedAt: string; values: SettingValue[] } | null };
export type InventoryItem = { id: string; name: string; version: string; origin: string; location: string; bytes: number | null; presence: "observed" | "partial" | "not_observed"; verification: "not_checked"; qualification: "unknown" | "unqualified" | "qualified"; runtime: string; permission: "not_evaluated"; detail: string };
export type Inventory = Scope & { kind: "models" | "tools"; observedAt: string; local: { status: "not_configured" | "observed" | "partial"; nodeLabel: string; profileSha256: string | null; truncated: boolean; unavailableRoots: number }; items: InventoryItem[]; recordsTruncated: boolean; runtimeProbed: false; permissionEvaluated: false };
export type SwarmNode = { id: string; label: string; generation: number; state: "registered"; sshFingerprint: string; registeredAt: string; lastAuthenticatedContact: null; runtimeQualified: false };
export type Grant = { id: string; sourceWorkspaceId: string; revision: number; revocationGeneration: number; sha256: string; permissions: string[]; expiresAt: string; state: "active" | "expired" | "revoked"; canRequestRevocation: boolean };
export type SwarmRequest = { requestId: string; grantId: string | null; expectedRevision: number | null; state: "requested" | "applied" | "needs_review"; recordedAt: string };
export type Swarm = Scope & { observedAt: string; nodes: SwarmNode[]; deployments: { workspaceId: string; nodeId: string; revision: number; executionMode: "base_os"; qualification: "unqualified"; platform: string }[]; grants: Grant[]; requests: SwarmRequest[]; recordsTruncated: boolean; liveCapacityAvailable: false; workerTelemetryAvailable: false; nativeNodeActionsAvailable: false };
export type RevocationReview = { requestId: string; grantId: string; expectedRevision: number; expectedGeneration: number; grantSha256: string; confirm: true };
export type RevocationReceipt = Scope & { requestId: string; state: "requested" };
export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA = /^[0-9a-f]{64}$/;
const COMPUTATIONAL = new Set(["policy_versions","reasoning_models","coding_models","allowed_tools","source_access","physical_inference_concurrency","max_packet_tokens","verification_policy","embedding_profile","secret_refs"].map(key=>"configuration."+key));
const PREFERENCES = new Set(["theme","graphDisplay","reducedMotion","showGraphDiagnostics","orbAutoHide","downloadNotifications","xpraMaxFps"].map(key=>"preferences."+key));
const LINKS = new Set(["/","/settings/appearance","/settings/models","/settings/tools","/settings/swarm","/tools","/context","/research","/setup/apis","/setup/operator","/setup/tools"]);
const PERMISSIONS = new Set(["workspace.inspect","tool.execute","model.invoke","apps.launch","apps.view","apps.input","apps.terminate"]);
function obj(value: unknown): Record<string, unknown> { if (!value || typeof value!=="object" || Array.isArray(value)) throw Error("object"); return value as Record<string,unknown>; }
function text(value: unknown, max=240, empty=false): string { if(typeof value!=="string" || (!empty&&!value.length) || value.length>max || /[\x00-\x1f\x7f]/.test(value))throw Error("text");return value; }
function uuid(value: unknown): string { const result=text(value,36);if(!UUID.test(result)||result==="00000000-0000-0000-0000-000000000000")throw Error("uuid");return result; }
function hash(value: unknown): string { const result=text(value,64);if(!SHA.test(result))throw Error("hash");return result; }
function integer(value: unknown,min=0,max=Number.MAX_SAFE_INTEGER): number { if(typeof value!=="number"||!Number.isSafeInteger(value)||value<min||value>max)throw Error("integer");return value; }
function bool(value: unknown): boolean { if(typeof value!=="boolean")throw Error("bool");return value; }
function choice<T extends string>(value: unknown, choices: readonly T[]): T { if(typeof value!=="string"||!choices.includes(value as T))throw Error("choice");return value as T; }
function date(value: unknown): string { const result=text(value,48);if(!/^\d{4}-\d\d-\d\dT/.test(result)||!Number.isFinite(Date.parse(result)))throw Error("date");return result; }
function list<T>(value: unknown, max: number, project: (item: unknown)=>T): T[] { if(!Array.isArray(value)||value.length>max)throw Error("list");return value.map(project); }
function unique<T>(values: T[], key: (value:T)=>string): T[] { if(new Set(values.map(key)).size!==values.length)throw Error("duplicates");return values; }
function scope(value: Record<string,unknown>, workspaceId:string): Scope { if(value.schema!=="potbot-settings/v1"||uuid(value.workspaceId)!==workspaceId)throw Error("scope");return {schema:"potbot-settings/v1",workspaceId,principalId:uuid(value.principalId)}; }
function settingText(id:string,value:unknown):string {
  const result=text(value,1024,true);
  if(["configuration.secret_refs","configuration.source_access"].includes(id)&&!(/^[0-9]{1,10} configured$/.test(result)||result==="Not recorded"))throw Error("private setting");
  return result;
}
function values(value:unknown):SettingValue[] { const result=unique(list(value,10,item=>{const v=obj(item),id=text(v.id,100);if(!COMPUTATIONAL.has(id))throw Error("setting");return {id,value:settingText(id,v.value)};}),item=>item.id);if(result.length!==10)throw Error("coverage");return result; }
export function projectCatalogue(raw:unknown, workspaceId:string):Catalogue|null {
  try {
    const value=obj(raw),base=scope(value,workspaceId);if(value.layerEditing!==false)throw Error("layer editor");
    const categories=unique(list(value.categories,14,item=>{const row=obj(item),path=text(row.path,80);if(!LINKS.has(path))throw Error("link");return {id:choice(row.id,SETTINGS_SECTIONS),label:text(row.label,100),path};}),item=>item.id);
    if(categories.length!==14)throw Error("categories");
    const features=unique(list(value.features,29,item=>{const row=obj(item),id=text(row.id,5);if(!/^CFG(?:0[1-9]|1[0-9]|2[0-9])$/.test(id))throw Error("feature");return {id,label:text(row.label,100),category:choice(row.category,SETTINGS_SECTIONS),description:text(row.description,512)};}),item=>item.id);
    if(features.length!==29)throw Error("features");
    const descriptors=unique(list(value.descriptors,17,item=>{const row=obj(item),id=text(row.id,100),editable=bool(row.editable);
      if(!COMPUTATIONAL.has(id)&&!PREFERENCES.has(id)||editable!==PREFERENCES.has(id))throw Error("descriptor authority");
      return {id,label:text(row.label,100),category:choice(row.category,SETTINGS_SECTIONS),kind:choice(row.kind,["integer","snapshot","choice","boolean"]),scopes:list(row.scopes,5,item=>text(item,40)),defaultValue:COMPUTATIONAL.has(id)?settingText(id,row.defaultValue):text(row.defaultValue,1024,true),editable,apply:choice(row.apply,["client","next_admission"]),owner:text(row.owner,80),reason:text(row.reason,512),options:list(row.options,10,item=>typeof item==="number"?integer(item,0,60):text(item,80))};}),item=>item.id);
    if(descriptors.length!==17)throw Error("descriptors");
    const layers=list(value.layers,5,item=>text(item,20));if(layers.join(",")!=="installation,deployment,workspace,repository,task")throw Error("layers");
    return {...base,categories,features,descriptors,layers,layerEditing:false};
  }catch{return null;}
}
export function projectEffective(raw:unknown,workspaceId:string):Effective|null {
  try { const value=obj(raw),base=scope(value,workspaceId);if(value.liveLayersAvailable!==false)throw Error("layers");
    const snapshot=value.latestRecordedSnapshot===null?null:obj(value.latestRecordedSnapshot);
    return {...base,liveLayersAvailable:false,defaults:values(value.defaults),latestRecordedSnapshot:snapshot?{id:uuid(snapshot.id),sha256:hash(snapshot.sha256),recordedAt:date(snapshot.recordedAt),values:values(snapshot.values)}:null};
  }catch{return null;}
}
export function projectInventory(raw:unknown,workspaceId:string,kind:"models"|"tools"):Inventory|null {
  try {
    const value=obj(raw),base=scope(value,workspaceId),local=obj(value.local);
    if(value.kind!==kind||value.runtimeProbed!==false||value.permissionEvaluated!==false)throw Error("inventory");
    const items=unique(list(value.items,200,item=>{
      const row=obj(item),id=text(row.id,80),origin=choice(row.origin,["local_file","partial_file","package_metadata","deployment_record","tool_release_record"]);
      if(!UUID.test(id)&&!/^local:[0-9a-f]{64}$/.test(id))throw Error("identity");
      if(kind==="models"&&["package_metadata","tool_release_record"].includes(origin)||kind==="tools"&&origin!=="package_metadata"&&origin!=="tool_release_record")throw Error("kind");
      const presence=choice(row.presence,["observed","partial","not_observed"]);
      const localItem=["local_file","partial_file","package_metadata"].includes(origin);
      if(localItem&&(!id.startsWith("local:")||presence!==(origin==="partial_file"?"partial":"observed"))||!localItem&&(!UUID.test(id)||presence!=="not_observed"||row.bytes!==null))throw Error("observation identity");
      const location=text(row.location,512),qualification=choice(row.qualification,["unknown","unqualified","qualified"]),runtime=choice(row.runtime,["unknown","healthy","degraded","unavailable"]);
      if(row.verification!=="not_checked"||row.permission!=="not_evaluated")throw Error("unproved eligibility");
      if(origin==="deployment_record"&&location!=="Deployment registry"||origin==="tool_release_record"&&location!=="Tool acquisition ledger")throw Error("private path");
      if(["local_file","partial_file","package_metadata"].includes(origin)){
        if(location.startsWith("/")||/[\\:]/.test(location)||location.split("/").some(part=>!part||part==="."||part==="..")||qualification!=="unknown"||runtime!=="unknown")throw Error("unsafe local projection");
      }
      return {id,name:text(row.name),version:text(row.version,100,true),origin,location,bytes:row.bytes===null?null:integer(row.bytes),presence,verification:"not_checked" as const,qualification,runtime,permission:"not_evaluated" as const,detail:text(row.detail,512)};
    }),item=>item.id);
    return {...base,kind,observedAt:date(value.observedAt),local:{status:choice(local.status,["not_configured","observed","partial"]),nodeLabel:text(local.nodeLabel,120),profileSha256:local.profileSha256===null?null:hash(local.profileSha256),truncated:bool(local.truncated),unavailableRoots:integer(local.unavailableRoots)},items,recordsTruncated:bool(value.recordsTruncated),runtimeProbed:false,permissionEvaluated:false};
  }catch{return null;}
}
export function projectSwarm(raw:unknown,workspaceId:string):Swarm|null {
  try {
    const value=obj(raw),base=scope(value,workspaceId);
    if(value.liveCapacityAvailable!==false||value.workerTelemetryAvailable!==false||value.nativeNodeActionsAvailable!==false)throw Error("unimplemented capability");
    const nodes=unique(list(value.nodes,100,item=>{const row=obj(item);if(row.state!=="registered"||row.runtimeQualified!==false||row.lastAuthenticatedContact!==null)throw Error("unverified node");const sshFingerprint=text(row.sshFingerprint,60);if(!/^SHA256:[A-Za-z0-9+/]{43}$/.test(sshFingerprint))throw Error("pin");return {id:uuid(row.id),label:text(row.label,253),generation:integer(row.generation,1),state:"registered" as const,sshFingerprint,registeredAt:date(row.registeredAt),lastAuthenticatedContact:null,runtimeQualified:false as const};}),item=>item.id);
    const deployments=list(value.deployments,100,item=>{const row=obj(item);if(uuid(row.workspaceId)!==workspaceId||row.executionMode!=="base_os"||row.qualification!=="unqualified")throw Error("deployment");return {workspaceId,nodeId:uuid(row.nodeId),revision:integer(row.revision,1),executionMode:"base_os" as const,qualification:"unqualified" as const,platform:text(row.platform)};});
    const grants=unique(list(value.grants,100,item=>{const row=obj(item),permissions=unique(list(row.permissions,7,item=>text(item,40)),item=>item);if(!permissions.length||permissions.some(p=>!PERMISSIONS.has(p)))throw Error("permissions");return {id:uuid(row.id),sourceWorkspaceId:uuid(row.sourceWorkspaceId),revision:integer(row.revision,1),revocationGeneration:integer(row.revocationGeneration,1),sha256:hash(row.sha256),permissions,expiresAt:date(row.expiresAt),state:choice(row.state,["active","expired","revoked"]),canRequestRevocation:bool(row.canRequestRevocation)};}),item=>item.id);
    const requests=unique(list(value.requests,100,item=>{const row=obj(item),state=choice(row.state,["requested","applied","needs_review"]),grantId=row.grantId===null?null:uuid(row.grantId),expectedRevision=row.expectedRevision===null?null:integer(row.expectedRevision,1);if(state!=="needs_review"&&(!grantId||!expectedRevision))throw Error("request");return {requestId:uuid(row.requestId),grantId,expectedRevision,state,recordedAt:date(row.recordedAt)};}),item=>item.requestId);
    return {...base,observedAt:date(value.observedAt),nodes,deployments,grants,requests,recordsTruncated:bool(value.recordsTruncated),liveCapacityAvailable:false,workerTelemetryAvailable:false,nativeNodeActionsAvailable:false};
  }catch{return null;}
}
export function revocationReview(raw:unknown):RevocationReview|null {
  try {const value=obj(raw);if(Object.keys(value).sort().join(",")!=="confirm,expectedGeneration,expectedRevision,grantId,grantSha256,requestId"||value.confirm!==true)throw Error("review");return {requestId:uuid(value.requestId),grantId:uuid(value.grantId),expectedRevision:integer(value.expectedRevision,1),expectedGeneration:integer(value.expectedGeneration,1),grantSha256:hash(value.grantSha256),confirm:true};}catch{return null;}
}
export function projectRevocationReceipt(raw:unknown,workspaceId:string,requestId:string):RevocationReceipt|null {
  try {const value=obj(raw),base=scope(value,workspaceId);if(value.state!=="requested"||uuid(value.requestId)!==requestId)throw Error("receipt");return {...base,requestId,state:"requested"};}catch{return null;}
}
