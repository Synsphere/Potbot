"use client";

import { useEffect, useRef, useState } from "react";
import type { Catalogue, Effective, Grant, Inventory, RevocationReview, SettingsSection, Swarm } from "../../lib/settings.ts";
import { UUID, projectCatalogue, projectEffective, projectInventory, projectRevocationReceipt, projectSwarm } from "../../lib/settings.ts";
import { bytesLabel, projectPreferences, validateSettings } from "../../lib/ui-resources.ts";
import type { UiPreferences, UiSettings } from "../../lib/ui-resources.ts";
import { filterInventory, preferenceChanges, rebasePreferences, revocationIntent } from "../../lib/settings-client.ts";
import styles from "./settings.module.css";

type Workspace={id:string;name:string};
type Review={grant:Grant;body:RevocationReview};
const SECTIONS:Record<SettingsSection,string>={
  workspace:"Workspace and profiles",appearance:"Appearance and accessibility",models:"Models and providers",
  tools:"Tools, MCP and skills",swarm:"Workspace swarm",execution:"Tasks and execution",context:"Context and memory",
  research:"Research and sources",applications:"Applications and browser",storage:"Storage and transfers",
  security:"Security and credentials",services:"Services and network",diagnostics:"Monitoring and audit",updates:"Updates and recovery",
};
const BOOLEAN_LABELS:Partial<Record<keyof UiSettings,string>>={reducedMotion:"Reduce motion",showGraphDiagnostics:"Graph diagnostics",orbAutoHide:"Automatically hide the orb",downloadNotifications:"Download notifications"};
async function responseBody(response:Response):Promise<unknown>{if(!response.ok)throw Error(String(response.status));return response.json();}

export default function SettingsWorkbench({section}:{section:SettingsSection|"overview"}){
  const [workspaces,setWorkspaces]=useState<Workspace[]>([]),[workspaceId,setWorkspaceId]=useState(""),[homeId,setHomeId]=useState("");
  const [catalogue,setCatalogue]=useState<Catalogue|null>(null),[effective,setEffective]=useState<Effective|null>(null);
  const [inventory,setInventory]=useState<Inventory|null>(null),[swarm,setSwarm]=useState<Swarm|null>(null);
  const [preferences,setPreferences]=useState<UiPreferences|null>(null),[draft,setDraft]=useState<UiSettings>({}),[latest,setLatest]=useState<UiPreferences|null>(null);
  const [notice,setNotice]=useState("Loading authorised workspace connections…"),[operation,setOperation]=useState(""),[loading,setLoading]=useState(false),[busy,setBusy]=useState(false);
  const [search,setSearch]=useState(""),[filter,setFilter]=useState("all"),[epoch,setEpoch]=useState(0),[review,setReview]=useState<Review|null>(null);
  const dialog=useRef<HTMLDialogElement>(null),back=useRef<HTMLButtonElement>(null),requestAbort=useRef<AbortController|null>(null);
  const currentScope=useRef(workspaceId);currentScope.current=workspaceId;
  const retainedRequests=useRef(new Map<string,RevocationReview>());
  useEffect(()=>{
    const controller=new AbortController();
    void fetch("/api/ui/bootstrap",{cache:"no-store",credentials:"same-origin",signal:controller.signal}).then(responseBody).then(raw=>{
      if(controller.signal.aborted)return;
      const value=raw as {workspaces?:unknown};
      if(!value||!Array.isArray(value.workspaces)||value.workspaces.length>12)throw Error();
      const known=value.workspaces.map((item:unknown)=>{const row=item as Workspace;if(!row||typeof row.id!=="string"||!UUID.test(row.id)||typeof row.name!=="string"||!row.name.trim()||row.name.length>80)throw Error();return {id:row.id,name:row.name};});
      if(new Set(known.map(item=>item.id)).size!==known.length)throw Error();
      const query=new URLSearchParams(window.location.search),original=query.getAll("workspace").length<=1?query.get("workspace"):null;
      const home=known.find(item=>item.id===original)??(!original?known[0]:undefined);
      const requested=query.getAll("target").length<=1?query.get("target"):null;
      const selected=known.find(item=>item.id===requested)??home;
      setWorkspaces(known);setHomeId(home?.id??"");
      if(selected){setWorkspaceId(selected.id);setNotice("");}else setNotice("Select an authorised workspace connection.");
    }).catch(()=>{if(!controller.signal.aborted)setNotice("Potbot could not verify workspace connections. Check operator setup.");});
    return ()=>{controller.abort();requestAbort.current?.abort();};
  },[]);
  useEffect(()=>{
    if(!workspaceId)return;
    const controller=new AbortController(),headers={"X-Potbot-Workspace":workspaceId};
    setCatalogue(null);setEffective(null);setInventory(null);setSwarm(null);setPreferences(null);setLatest(null);setReview(null);setNotice("");setLoading(true);
    const read=async(resource:string)=>responseBody(await fetch("/api/ui/settings/"+resource,{cache:"no-store",credentials:"same-origin",headers,signal:controller.signal}));
    async function load(){
      const catalog=projectCatalogue(await read("catalogue"),workspaceId);if(!catalog)throw Error();
      if(controller.signal.aborted)return;setCatalogue(catalog);
      if(section==="models"||section==="tools"){
        const value=projectInventory(await read(section),workspaceId,section);if(!value||value.principalId!==catalog.principalId)throw Error();
        if(!controller.signal.aborted)setInventory(value);
      }else if(section==="swarm"){
        const value=projectSwarm(await read("swarm"),workspaceId);if(!value||value.principalId!==catalog.principalId)throw Error();
        if(!controller.signal.aborted)setSwarm(value);
      }else if(section==="appearance"){
        const raw=await responseBody(await fetch("/api/ui/preferences",{cache:"no-store",credentials:"same-origin",headers,signal:controller.signal}));
        const value=projectPreferences(raw,workspaceId);if(!value)throw Error();
        if(!controller.signal.aborted){setPreferences(value);setDraft(value.settings);}
      }else{
        const value=projectEffective(await read("effective"),workspaceId);if(!value||value.principalId!==catalog.principalId)throw Error();
        if(!controller.signal.aborted)setEffective(value);
      }
    }
    void load().catch(()=>{if(!controller.signal.aborted){setCatalogue(null);setEffective(null);setInventory(null);setSwarm(null);setPreferences(null);setNotice("This settings resource is unavailable. No inventory or runtime state has been inferred.");}})
      .finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return ()=>controller.abort();
  },[workspaceId,section,epoch]);
  useEffect(()=>{
    if(review&&dialog.current&&!dialog.current.open){dialog.current.showModal();back.current?.focus();}
    if(!review&&dialog.current?.open)dialog.current.close();
  },[review]);
  const active=workspaces.find(item=>item.id===homeId),target=workspaces.find(item=>item.id===workspaceId);
  const query=workspaceId?`?workspace=${encodeURIComponent(homeId||workspaceId)}&target=${encodeURIComponent(workspaceId)}`:"";
  const ownerQuery=workspaceId?`?workspace=${encodeURIComponent(workspaceId)}`:"";
  const link=(name:string)=>"/settings"+(name==="overview"?"":"/"+name)+query;
  function selectTarget(value:string){
    requestAbort.current?.abort();setWorkspaceId(value);setBusy(false);setOperation("");
    const url=new URL(window.location.href);url.searchParams.set("target",value);window.history.replaceState(null,"",url);
  }
  async function savePreferences(){
    if(!preferences||busy)return;
    const selected=workspaceId,submitted={...draft},base=preferences;
    setBusy(true);setOperation("");
    try{
      const response=await fetch("/api/ui/preferences",{method:"PUT",cache:"no-store",credentials:"same-origin",headers:{"Content-Type":"application/json","X-Potbot-Workspace":selected},body:JSON.stringify({revision:base.revision,settings:submitted})});
      if(currentScope.current!==selected)return;
      if(response.status===409){
        const raw=await responseBody(await fetch("/api/ui/preferences",{cache:"no-store",credentials:"same-origin",headers:{"X-Potbot-Workspace":selected}}));
        const next=projectPreferences(raw,selected);if(!next)throw Error();
        if(currentScope.current!==selected)return;
        setLatest(next);setOperation("Preferences changed in another session. Your edits are retained. Review and rebase before saving.");return;
      }
      const value=projectPreferences(await responseBody(response),selected);if(!value)throw Error();
      if(currentScope.current===selected){setPreferences(value);setDraft(value.settings);setLatest(null);setOperation("Saved preferences read back at revision "+value.revision+". They apply when the workspace reloads its saved preferences.");}
    }catch{if(currentScope.current===selected)setOperation("Save outcome not confirmed. Refresh saved values before submitting another change.");}
    finally{if(currentScope.current===selected)setBusy(false);}
  }
  function prepareRebase(){
    if(!preferences||!latest)return;
    const result=rebasePreferences(preferences.settings,draft,latest.settings);
    setPreferences(latest);setDraft(result.settings);setLatest(null);
    setOperation(result.conflicts.length?`Rebased for review. Your choices replace concurrent changes to: ${result.conflicts.join(", ")}. Save only after checking them.`:"Rebased your edits while preserving unrelated saved changes. Review before saving.");
  }
  async function submitRevocation(){
    if(!review||busy||!swarm)return;
    const selected=workspaceId,retained=review,controller=new AbortController();requestAbort.current=controller;setBusy(true);setOperation("");
    try{
      const response=await fetch("/api/ui/settings/requests",{method:"POST",credentials:"same-origin",cache:"no-store",signal:controller.signal,headers:{"Content-Type":"application/json","X-Potbot-Workspace":selected},body:JSON.stringify(retained.body)});
      const value=projectRevocationReceipt(await responseBody(response),selected,retained.body.requestId);if(!value)throw Error();
      if(currentScope.current!==selected)return;setReview(null);setEpoch(value=>value+1);
      setOperation("Revocation request saved. A native operator must apply it; request acceptance is not grant revocation.");
    }catch{if(currentScope.current===selected)setOperation("Request outcome not confirmed. Keep request "+retained.body.requestId+" and check the swarm list before resubmitting this same review.");}
    finally{if(currentScope.current===selected)setBusy(false);}
  }
  const descriptors=catalogue?.descriptors.filter(item=>(section==="overview"||item.category===section)&&(!search||[item.label,item.id,item.reason].join(" ").toLowerCase().includes(search.toLowerCase())))??[];
  const features=catalogue?.features.filter(item=>(search||section==="overview"||item.category===section)&&(!search||[item.label,item.id,item.description].join(" ").toLowerCase().includes(search.toLowerCase())))??[];
  const items=inventory?filterInventory(inventory.items,search,filter):[];
  const dirty=preferences?preferenceChanges(preferences.settings,draft):[];
  const effectiveValues=new Map(effective?.defaults.map(item=>[item.id,item.value])??[]);
  const recordedValues=new Map(effective?.latestRecordedSnapshot?.values.map(item=>[item.id,item.value])??[]);
  return <main className={styles.shell} style={{colorScheme:preferences&&draft.theme&&draft.theme!=="system"?draft.theme:"light dark"}}>
    <header className={styles.header}><a href={homeId?`/?workspace=${encodeURIComponent(homeId)}`:"/"}>← Return to {active?.name??"Potbot"}</a>
      <p className={styles.kicker}>POTBOT / CONFIGURATION</p><h1>{section==="overview"?"Settings":SECTIONS[section]}</h1>
      <p>Inspect configuration and installed resources. Active workspace: <strong>{active?.name??"Not selected"}</strong>.</p>
      <div className={styles.toolbar}><label>Workspace to inspect<select value={workspaceId} disabled={busy} onChange={event=>selectTarget(event.target.value)}><option value="" disabled>Select workspace</option>{workspaces.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
        <label>Search settings and inventory<input type="search" value={search} onChange={event=>setSearch(event.target.value)} placeholder="Model, tool, source or policy"/></label>
        <button type="button" disabled={!workspaceId||loading||busy} onClick={()=>setEpoch(value=>value+1)}>Refresh</button></div>
      <p className={styles.muted}>Viewing {target?.name??"no target"}. Selecting another target keeps the return context above.</p>
    </header>
    <div className={styles.layout}>
      <nav className={styles.nav} aria-label="Settings categories"><a href={link("overview")} aria-current={section==="overview"?"page":undefined}>All settings</a>{Object.entries(SECTIONS).map(([id,label])=><a key={id} href={link(id)} aria-current={section===id?"page":undefined}>{label}</a>)}</nav>
      <section className={styles.content} aria-label="Selected settings">
        <label className={styles.mobileNav}>Settings category<select value={section} onChange={event=>{window.location.href=link(event.target.value);}}><option value="overview">All settings</option>{Object.entries(SECTIONS).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
        {notice&&<p role="status" className={styles.notice}>{notice}</p>}
        {loading&&<p role="status">Loading workspace-scoped records…</p>}
        {operation&&<p role="status" className={styles.notice}>{operation}</p>}
        {catalogue&&section==="appearance"&&preferences&&<section className={styles.card}>
          <h2>Saved preferences</h2><p>For your principal in {target?.name}. Revision {preferences.revision}.</p>
          <form onSubmit={event=>{event.preventDefault();void savePreferences();}}>
            <label className={styles.field}>Theme<select value={draft.theme??"system"} disabled={busy} onChange={event=>setDraft({...draft,theme:event.target.value as UiSettings["theme"]})}><option value="system">System</option><option value="dark">Dark</option><option value="light">Light</option></select></label>
            <label className={styles.field}>Graph display<select value={draft.graphDisplay??"3d"} disabled={busy} onChange={event=>setDraft({...draft,graphDisplay:event.target.value as UiSettings["graphDisplay"]})}><option value="3d">3D graph</option><option value="list">Accessible list</option></select></label>
            {Object.entries(BOOLEAN_LABELS).map(([key,label])=><label className={styles.check} key={key}><input type="checkbox" disabled={busy} checked={draft[key as keyof UiSettings]===true} onChange={event=>setDraft({...draft,[key]:event.target.checked})}/>{label}</label>)}
            <label className={styles.field}>Xpra client frame limit<select value={draft.xpraMaxFps??30} disabled={busy} onChange={event=>setDraft({...draft,xpraMaxFps:Number(event.target.value) as UiSettings["xpraMaxFps"]})}>{[10,15,24,30,60].map(value=><option key={value} value={value}>{value} FPS</option>)}</select></label>
            <button type="submit" disabled={busy||!dirty.length||!!latest||!validateSettings(draft)}>{busy?"Saving…":"Save preferences"}</button>
            <p className={styles.muted}>{dirty.length?`Pending: ${dirty.join(", ")}`:"No unsaved preference changes."} Layout coordinates and compact density remain browser-local.</p>
          </form>
          {latest&&<div className={styles.notice}><h3>Concurrent saved revision {latest.revision}</h3><pre>{JSON.stringify(latest.settings,null,2)}</pre><button type="button" onClick={prepareRebase}>Rebase my edits for review</button><button type="button" onClick={()=>{setPreferences(latest);setDraft(latest.settings);setLatest(null);setOperation("Loaded the current saved values.");}}>Use latest saved values</button></div>}
        </section>}
        {catalogue&&(section==="models"||section==="tools")&&inventory&&<section>
          <h2>{section==="models"?"Model files and registered deployments":"Package metadata and managed tool records"}</h2>
          <p>Execution backend: <strong>{inventory.local.nodeLabel}</strong>. Inventory read {inventory.observedAt}.</p>
          {inventory.local.status==="not_configured"&&<p className={styles.notice}>Native file inventory is not configured. The operator must select this workspace&apos;s project roots. Registry records below do not establish local installation.</p>}
          {(inventory.local.status==="partial"||inventory.recordsTruncated)&&<p className={styles.notice}>This inventory is partial or bounded. {inventory.local.unavailableRoots} selected locations could not be inspected; some entries may be omitted.</p>}
          <label className={styles.field}>Inventory filter<select value={filter} onChange={event=>setFilter(event.target.value)}><option value="all">All observations and records</option><option value="observed">Observed local metadata/files</option><option value="records">Registry/lifecycle records</option><option value="attention">Needs further checks</option></select></label>
          <p className={styles.muted}>Metadata refresh loads no models and runs no tools. Runtime loading and task permissions have not been evaluated.</p>
          <div className={styles.items}>{items.map(item=><article key={item.id} className={styles.card}><header><h3>{item.name}</h3><span className={styles.badge}>{item.origin.replaceAll("_"," ")}</span></header><p>{item.version||"Version/quantization not established"}</p><dl><dt>Presence</dt><dd>{item.presence.replaceAll("_"," ")}</dd><dt>Qualification</dt><dd>{item.qualification}</dd><dt>Recorded runtime health</dt><dd>{item.runtime}</dd><dt>Task permission</dt><dd>Not evaluated</dd><dt>Size</dt><dd>{item.bytes===null?"Not observed":bytesLabel(item.bytes)}</dd></dl><p>{item.detail}</p><details><summary>Observation details</summary><p>{item.location}</p><code>{item.id}</code></details></article>)}</div>
          {!items.length&&<p>No matching authorised observations. An unavailable or unconfigured root is not an empty machine.</p>}
          <div className={styles.links}><a href={"/setup/tools"+ownerQuery}>Tool acquisition and upgrades</a><a href={"/tools"+ownerQuery}>Operation readiness</a><a href="/setup/apis">Provider credentials</a></div>
        </section>}
        {catalogue&&section==="swarm"&&swarm&&<section>
          <h2>Registered nodes</h2><p>Authority workspace: {target?.name}. Last ledger read {swarm.observedAt}.</p>
          <p className={styles.muted}>Registration does not establish authenticated contact, live capacity or execution qualification. Worker telemetry and native lifecycle controls are not available in this increment.</p>
          {swarm.recordsTruncated&&<p role="status">Showing a bounded subset of ledger records.</p>}
          <div className={styles.items}>{swarm.nodes.map(node=><article className={styles.card} key={node.id}><header><h3>{node.label}</h3><span className={styles.badge}>{node.state}</span></header><p>Generation {node.generation}. Remote contact and runtime qualification have not been established.</p><details><summary>Public identity</summary><p>{node.id}</p><code>{node.sshFingerprint}</code><p>Registered {node.registeredAt}</p></details></article>)}</div>
          {!swarm.nodes.length&&<p>No registered nodes are visible in this authority workspace.</p>}
          <h2>Workspace deployments</h2>{swarm.deployments.map(item=><article className={styles.card} key={item.workspaceId}><h3>Workspace {item.workspaceId}</h3><p>Node {item.nodeId} · revision {item.revision} · {item.executionMode}</p><p>{item.platform} · {item.qualification}</p></article>)}{!swarm.deployments.length&&<p>No native deployment is recorded for this workspace.</p>}
          <h2>Direct control grants</h2><p>These target-owner records grant access from the displayed source workspace. Revocation is applied through the protected native operator.</p>
          {swarm.grants.map(grant=><article className={styles.card} key={grant.id}><header><h3>From {grant.sourceWorkspaceId}</h3><span className={styles.badge}>{grant.state}</span></header><p>{grant.permissions.join(" · ")}</p><p>Expires {grant.expiresAt} · revision {grant.revision}</p><button type="button" disabled={!grant.canRequestRevocation||busy} onClick={()=>{setOperation("");const key=workspaceId+":"+swarm.principalId+":"+grant.id+":"+grant.revision;const body=retainedRequests.current.get(key)??revocationIntent(grant,swarm.requests,crypto.randomUUID());if(retainedRequests.current.size>200)retainedRequests.current.clear();retainedRequests.current.set(key,body);setReview({grant,body});}}>Request revocation</button>{!grant.canRequestRevocation&&grant.state!=="revoked"&&<p className={styles.muted}>Target ownership and configured native operator setup are required.</p>}</article>)}
          {!swarm.grants.length&&<p>No direct control grants are visible.</p>}
          <h2>Management requests</h2>{swarm.requests.map(item=><article className={styles.card} key={item.requestId}><h3>{item.state==="applied"?"Revocation observed":item.state==="requested"?"Awaiting native operator":"Needs review"}</h3><p>Request {item.requestId}</p><p>Grant {item.grantId??"unavailable"} · reviewed revision {item.expectedRevision??"unknown"}</p>{item.state==="requested"&&<details><summary>Native operator handoff</summary><p>Run on the protected operator host with its native administrative credential. The command checks ownership, the original request and the current grant.</p><pre><code>{`potbot remote-workspace apply-revocation --workspace-id ${workspaceId} --owner-principal-id ${swarm.principalId} --request-id ${item.requestId} --confirm`}</code></pre><p>Then refresh this view. A saved request alone does not confirm revocation or process shutdown.</p></details>}</article>)}{!swarm.requests.length&&<p>No management requests recorded for your principal.</p>}
          <div className={styles.links}><a href={"/setup/operator"}>Native operator health</a><a href={"/tools"+ownerQuery}>Jobs and operation readiness</a></div>
        </section>}
        {catalogue&&effective&&descriptors.length>0&&<section>
          <h2>Configuration defaults and recorded values</h2><p className={styles.muted}>These are installation defaults and the latest recorded run snapshot. Live layer editing and live effective-configuration inspection are not available yet.</p>
          {effective.latestRecordedSnapshot&&<p>Recorded snapshot {effective.latestRecordedSnapshot.id} · {effective.latestRecordedSnapshot.recordedAt}</p>}
          {descriptors.filter(item=>item.id.startsWith("configuration.")).map(item=><article className={styles.card} key={item.id}><h3>{item.label}</h3><dl><dt>Installation default</dt><dd>{effectiveValues.get(item.id)??item.defaultValue}</dd><dt>Recorded run snapshot</dt><dd>{recordedValues.get(item.id)??"Not recorded"}</dd><dt>Apply behaviour</dt><dd>Subsequent admissions</dd></dl><p>{item.reason}</p><details><summary>Ownership and scopes</summary><p>{item.owner} · {item.scopes.join(" → ")}</p><code>{item.id}</code></details></article>)}
        </section>}
        {catalogue&&features.length>0&&<section className={styles.coverage}><h2>Feature configuration owners</h2><p>Existing workbenches remain authoritative. Some configuration editors and remote capabilities are still planned.</p><div className={styles.items}>{features.map(feature=>{const owner=catalogue.categories.find(item=>item.id===feature.category);return <article className={styles.card} key={feature.id}><h3>{feature.label}</h3><p>{feature.description}</p><div className={styles.links}><a href={link(feature.category)}>Inspect this settings area</a>{owner&&owner.path!=="/"&&!owner.path.startsWith("/settings")&&<a href={owner.path+ownerQuery}>Open existing controls</a>}</div><small>{feature.id}</small></article>;})}</div></section>}
      </section>
    </div>
    <dialog ref={dialog} className={styles.dialog} aria-labelledby="revocation-title" onCancel={event=>{if(busy)event.preventDefault();else setReview(null);}} onClose={()=>{if(!busy)setReview(null);}}>
      {review&&<><h2 id="revocation-title">Request revocation of this grant?</h2><p>Source workspace {review.grant.sourceWorkspaceId} → target {target?.name}.</p><p>{review.grant.permissions.join(" · ")}</p><p>This queues an exact review for the native operator. Acceptance does not revoke the grant. Refresh after native application to verify the protected ledger.</p><details><summary>Exact review</summary><p>Grant {review.body.grantId}; revision {review.body.expectedRevision}; request {review.body.requestId}.</p><code>{review.body.grantSha256}</code></details>{operation&&<p role="status">{operation}</p>}<div className={styles.links}><button ref={back} type="button" disabled={busy} onClick={()=>setReview(null)}>Back</button><button type="button" disabled={busy} onClick={()=>void submitRevocation()}>{busy?"Submitting…":"Submit this request"}</button></div></>}
    </dialog>
  </main>;
}
