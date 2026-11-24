/** Fixed, server-only settings bridge; never a general URL or privileged RPC proxy. */
import { projectCatalogue, projectEffective, projectInventory, projectSwarm, projectRevocationReceipt, revocationReview } from "../settings.ts";
import type { SettingsResource } from "../settings.ts";
import { potbotConfigForRequest, upstream } from "./potbot.ts";
import { setupCapability } from "./api-setup.ts";
import { boundedJSON, requireOperator, safeError, validOrigin } from "./security.ts";

const PATHS:Record<SettingsResource,string> = {catalogue:"/v1/ui/settings/catalogue",effective:"/v1/ui/settings/effective",models:"/v1/ui/inventory/models",tools:"/v1/ui/inventory/tools",swarm:"/v1/ui/swarm/nodes",requests:"/v1/ui/swarm/revocations"};
async function responseJSON(response:Response):Promise<unknown>{
  if(!response.body)throw Error("body");const reader=response.body.getReader(),chunks:Uint8Array[]=[];let total=0;
  try{while(true){const part=await reader.read();if(part.done)break;total+=part.value.byteLength;if(total>524288){await reader.cancel();throw Error("bound");}chunks.push(part.value);}}
  finally{reader.releaseLock();}
  const bytes=new Uint8Array(total);let offset=0;for(const part of chunks){bytes.set(part,offset);offset+=part.length;}
  return JSON.parse(new TextDecoder("utf-8",{fatal:true}).decode(bytes));
}
export async function settingsRoute(request:Request,path:string[]):Promise<Response>{
  const operator=requireOperator(request);if(operator instanceof Response)return operator;
  if(path.length!==1||!Object.hasOwn(PATHS,path[0])||new URL(request.url).search)return safeError("Unknown settings resource or query.",400);
  const resource=path[0] as SettingsResource,write=resource==="requests";
  if(request.method!==(write?"POST":"GET"))return safeError("Unsupported settings method.",405);
  if(write&&!validOrigin(request,operator)||!write&&request.headers.has("origin")&&!validOrigin(request,operator))return safeError("Untrusted settings origin.",403);
  const config=potbotConfigForRequest(request);if(!config)return safeError("Workspace connection unavailable.",503);
  const setup=write?setupCapability():null;
  if(write&&!setup)return safeError("Native operator setup is required for management requests.",503);
  let body:string|undefined,requestId="";
  if(write){
    if(request.headers.get("content-type")?.split(";")[0]!=="application/json")return safeError("Expected a JSON review.",400);
    try{const review=revocationReview(await boundedJSON(request,4096));if(!review)return safeError("Invalid revocation review.",400);body=JSON.stringify(review);requestId=review.requestId;}
    catch{return safeError("Invalid revocation review.",400);}
  }
  try{
    const response=await upstream(config,PATHS[resource],{
      method:request.method,signal:AbortSignal.any([request.signal,AbortSignal.timeout(15000)]),
      headers:{...(write?{Origin:operator.publicOrigin,"Content-Type":"application/json","X-Potbot-Setup-Token":setup!}:{})},
      ...(body?{body}:{}),
    });
    if(!response.ok)return safeError(response.status===409?"The reviewed grant changed; reload and review.":"Settings resource unavailable.",response.status===409?409:response.status===404?404:response.status===403?403:503);
    const raw=await responseJSON(response);
    const result=resource==="catalogue"?projectCatalogue(raw,config.workspaceId):resource==="effective"?projectEffective(raw,config.workspaceId):resource==="models"||resource==="tools"?projectInventory(raw,config.workspaceId,resource):resource==="swarm"?projectSwarm(raw,config.workspaceId):projectRevocationReceipt(raw,config.workspaceId,requestId);
    if(!result)return safeError("Invalid scoped settings response.",502);
    if(resource==="swarm"&&"grants" in result&&!setupCapability())result.grants=result.grants.map(grant=>({...grant,canRequestRevocation:false}));
    return Response.json(result,{status:write?202:200,headers:{"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"}});
  }catch{return safeError(write?"Request outcome not confirmed. Check the original request before retrying.":"Settings resource unavailable.",503);}
}
