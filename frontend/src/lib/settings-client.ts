import type { UiSettings } from "./ui-resources.ts";
import type { InventoryItem, Grant, SwarmRequest, RevocationReview } from "./settings.ts";

export function preferenceChanges(base:UiSettings,draft:UiSettings): (keyof UiSettings)[]{
  return [...new Set([...Object.keys(base),...Object.keys(draft)])].filter(key=>base[key as keyof UiSettings]!==draft[key as keyof UiSettings]) as (keyof UiSettings)[];
}
/** Rebase only the human's edits; preserve unrelated concurrent saved preferences. */
export function rebasePreferences(base:UiSettings,draft:UiSettings,latest:UiSettings):{settings:UiSettings;conflicts:(keyof UiSettings)[]}{
  const changed=preferenceChanges(base,draft),settings={...latest},conflicts:(keyof UiSettings)[]=[];
  for(const key of changed){
    if(base[key]!==latest[key]&&draft[key]!==latest[key])conflicts.push(key);
    if(draft[key]===undefined)delete settings[key];else Object.assign(settings,{[key]:draft[key]});
  }
  return {settings,conflicts};
}
export function filterInventory(items:InventoryItem[],query:string,filter:string):InventoryItem[]{
  const needle=query.trim().toLowerCase();
  return items.filter(item=>(!needle||[item.name,item.version,item.origin,item.location].join(" ").toLowerCase().includes(needle))&&
    (filter==="all"||filter==="observed"&&item.presence==="observed"||filter==="records"&&item.presence==="not_observed"||filter==="attention"&&(item.presence!=="observed"||item.qualification!=="qualified")));
}
/** Reuse an original pending intent after a lost reply or reload. Never issue execution. */
export function revocationIntent(grant:Grant,requests:SwarmRequest[],freshId:string):RevocationReview{
  const pending=requests.find(item=>item.grantId===grant.id&&item.expectedRevision===grant.revision&&item.state==="requested");
  return {requestId:pending?.requestId??freshId,grantId:grant.id,expectedRevision:grant.revision,expectedGeneration:grant.revocationGeneration,grantSha256:grant.sha256,confirm:true};
}
