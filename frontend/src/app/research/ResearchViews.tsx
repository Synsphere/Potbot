"use client";
import type { ResearchContext, ResearchCitation, ResearchComparisonEnvelope } from "../../lib/research.ts";
import { researchReservationPreview, researchProviderInterval, researchProviderRequestAllowance } from "../../lib/research.ts";
import type { ResearchExecution, ResearchResult, ResearchSource, ResearchMergedResult } from "../../lib/research-execution.ts";
import { researchMetadataDateLabel, researchRetractionLabel } from "../../lib/research-specialist.ts";
import styles from "../skills/skills.module.css";

export function ResearchJSON({ title, value }: { title: string; value: unknown }) { return <details className={styles.details}><summary>{title}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>; }
export function ResearchReasons({ codes }: { codes: string[] }) { return codes.length ? <ul className={styles.help}>{codes.map(code => <li key={code}><code>{code}</code></li>)}</ul> : null; }
export function ResearchLink({ url }: { url: string }) { return <a className={styles.link} href={url} target="_blank" rel="noopener noreferrer">{url} ↗</a>; }

export function ResearchSpecialistResults({ results }: { results: ResearchMergedResult[] }) {
  const records = results.filter(result => result.lineage.some(source => source.specialist));
  if (!records.length) return null;
  return <section className={styles.section} aria-label="Scientific and archival discovery records">
    <h4>Scientific and archival records</h4>
    <p className={styles.help}>Publication dates, event dates and retrieval times are shown separately. Metadata remains a provider report; matching records from several indexes does not establish independent factual support. A record with no reported retraction has not been certified free of corrections or retractions.</p>
    <ul className={styles.history}>{records.map(record => <li key={record.id}>
      <h4>{record.title || "Untitled record"}</h4><ResearchLink url={record.canonicalUrl} />
      <p>{record.lineage.length} retained provider observation{record.lineage.length === 1 ? "" : "s"} for this grouped record.</p>
      {record.lineage.map(source => {
        const metadata = source.specialist;
        if (!metadata) return <p key={`${source.provider}:${source.rank}`}>{source.provider}, rank {source.rank}: web search observation · retrieved {source.retrievedAt}</p>;
        return <section className={styles.notice} key={`${source.provider}:${source.rank}`}>
          <h5>{source.provider} · rank {source.rank}</h5><ResearchLink url={source.canonicalUrl} />
          <dl className={styles.facts}>
            <dt>DOI</dt><dd>{metadata.doi ? <a className={styles.link} href={`https://doi.org/${encodeURIComponent(metadata.doi)}`} target="_blank" rel="noopener noreferrer">{metadata.doi}</a> : "Not reported"}</dd>
            <dt>Authors</dt><dd>{metadata.authors.join("; ") || "Not reported"}</dd>
            <dt>Source role</dt><dd>{metadata.sourceRole} · {metadata.recordKind.replaceAll("_", " ")}</dd>
            <dt>Publication date</dt><dd>{researchMetadataDateLabel(metadata.publicationDate)}</dd>
            <dt>Event date</dt><dd>{researchMetadataDateLabel(metadata.eventDate)}</dd>
            <dt>Metadata updated</dt><dd>{researchMetadataDateLabel(metadata.updatedDate)}</dd>
            <dt>Retrieved</dt><dd>{source.retrievedAt}</dd>
            <dt>Provider record</dt><dd><code>{metadata.recordId}</code></dd>
            <dt>Retraction status</dt><dd className={metadata.retraction.state === "reported_retracted" ? styles.warning : undefined}>{researchRetractionLabel(metadata.retraction.state)}</dd>
          </dl>
          {metadata.retraction.notices.length > 0 && <><h5>Reported corrections and notices</h5><ul>{metadata.retraction.notices.map((notice, index) => <li key={index}>
            <strong>{notice.kind.replaceAll("_", " ")}</strong> · {notice.relation === "updates" ? "this record updates" : notice.relation === "updated_by" ? "this record is updated by" : "this record is flagged"}{notice.targetDoi ? <> · <code>{notice.targetDoi}</code></> : ""} · {researchMetadataDateLabel(notice.date)}
          </li>)}</ul></>}
        </section>;
      })}
    </li>)}</ul>
  </section>;
}

export function ResearchContextView({ context }: { context: ResearchContext }) {
  const reservation = context.policy ? researchReservationPreview(context.policy.content) : null;
  return <section className={styles.notice}><h3>{context.need?.spec.question ?? "Task context: choose or create an information need"}</h3>
    <dl className={styles.facts}><dt>Task episode</dt><dd><code>{context.task.episodeId}</code></dd><dt>Contract revision</dt><dd><code>{context.task.contractRevisionId}</code></dd><dt>Repository state</dt><dd><code>{context.task.repositoryStateId}</code></dd><dt>Task status</dt><dd>{context.task.status} · {context.current ? "current snapshot" : "historical or unavailable for admission"}</dd><dt>Context digest</dt><dd><code>{context.contextSha256}</code></dd>{context.need && <><dt>Need identity</dt><dd><code>{context.need.id}</code></dd><dt>Need revision</dt><dd><code>{context.need.revisionId}</code> · {context.need.status}</dd><dt>Need content digest</dt><dd><code>{context.need.contentSha256}</code></dd></>}</dl>
    <ResearchReasons codes={context.reasonCodes} />
    {context.policy ? <><h4>Configured disclosure and resource limits</h4><p>{context.policy.content.allow_query_disclosure ? "The configured policy permits explicit query disclosure. A separate confirmation below is required for each admitted action." : "Query disclosure is disabled in the configured policy."}</p>
      <dl className={styles.facts}><dt>Policy digest</dt><dd><code>{context.policy.sha256}</code></dd><dt>Search providers</dt><dd>{context.policy.content.providers.map(item => item.provider).join(" → ") || "None configured"}</dd><dt>Result limits</dt><dd>{context.policy.content.max_results} results per provider; at most {context.policy.content.max_documents} fetched documents</dd><dt>Request allowance</dt><dd>{context.policy.content.max_requests} sends; {context.policy.content.max_source_bytes.toLocaleString()} bytes per response; {context.policy.content.timeout_seconds}s per request</dd><dt>Approved source hosts</dt><dd>{context.policy.content.fetch_hosts.filter(item => item.terms_approved).map(item => item.host).join(", ") || "None: no source fetching is permitted"}</dd><dt>Fallback statuses</dt><dd>{context.policy.content.fallback_statuses.join(", ") || "None"}</dd></dl>
      <div className={styles.tableScroll}><table className={styles.table}><thead><tr><th>Provider</th><th>Declared call ceiling</th><th>Minimum interval</th><th>Fixed endpoint</th></tr></thead><tbody>{context.policy.content.providers.map(item => <tr key={item.provider}><td>{item.provider}</td><td>{item.cost_microunits_per_call.toLocaleString()} money microunits</td><td>{researchProviderInterval(item).toLocaleString()} ms</td><td><code>{context.providerDescriptors.find(provider => provider.id === item.provider)?.endpoint ?? "Descriptor unavailable"}</code></td></tr>)}</tbody></table></div>
      <p className={styles.help}>Provider requests require an allowance of {researchProviderRequestAllowance(context.policy.content)} sends before document retrieval, including a possible PubMed metadata request. Minimum intervals include the adapter’s service limits.</p>
      {reservation && <><h4>Reservation ceiling before admission</h4><p>{reservation.calls.toLocaleString()} calls · {reservation.bytes_read.toLocaleString()} bytes · {reservation.tool_ms.toLocaleString()} tool ms, including configured intervals and document processing · {reservation.money_microunits.toLocaleString()} money microunits · 1 concurrency slot.</p><p className={styles.help}>This is the ceiling implied by the reviewed policy. Admission checks the task’s available budget and retains the exact owned reservation.</p></>}
      <p className={styles.help}>Monetary tariffs are declared ceilings. A conservative estimate does not establish a measured provider invoice. Queries and retained sources are shared with authenticated members of this workspace.</p></> : <p>No external research policy is available. A native operator must configure supported providers, credentials, fetch hosts and limits before an action can be queued.</p>}
    {context.need && <ResearchJSON title="Exact information need and resolution criteria" value={context.need} />}<ResearchJSON title="Exact task, policy and provider descriptors" value={context} />
  </section>;
}

export function ResearchExecutionView({ execution }: { execution: ResearchExecution }) {
  const { run, action, authorization, receipt } = execution;
  return <><h3>{action?.query.search_text ?? "Retained research run"}</h3><div className={styles.badges}><span className={styles.badge}>{run.state}</span><span className={styles.badge}>revision {run.revision}</span><span className={styles.badge}>workspace shared</span><span className={styles.badge}>{run.recordIntegrityValid ? "record integrity valid" : "record integrity unavailable"}</span></div>
    <dl className={styles.facts}><dt>Research run</dt><dd><code>{run.id}</code></dd><dt>Task episode</dt><dd><code>{run.episodeId}</code></dd><dt>Information need</dt><dd><code>{run.needId ?? "Unavailable"}</code></dd><dt>Need revision</dt><dd><code>{run.needRevisionId ?? "Unavailable"}</code></dd><dt>Admitting operator</dt><dd><code>{authorization?.actorPrincipalId ?? "Unavailable"}</code></dd><dt>Current reader</dt><dd><code>{execution.scope.principalId}</code></dd><dt>Disclosure decision</dt><dd><code>{run.disclosureDecisionId}</code></dd><dt>Reservation</dt><dd><code>{run.reservationId}</code></dd><dt>Requests retained</dt><dd>{run.requestCount}</dd><dt>Usage origin</dt><dd>{run.usageOrigin ?? "Not settled"}</dd><dt>Created</dt><dd>{run.createdAt}</dd><dt>Completed</dt><dd>{run.finishedAt ?? "Not finished"}</dd><dt>Result artifact</dt><dd><code>{run.resultArtifactId ?? "Not available"}</code></dd><dt>Receipt digest</dt><dd><code>{run.receiptSha256 ?? "Not available"}</code></dd></dl>
    {run.errorCode && <ResearchReasons codes={[run.errorCode]} />}{run.cancelRequested && <p className={styles.warning}>Cancellation has been requested. Retained transport evidence may still describe a request already sent.</p>}
    {!run.recordIntegrityValid && <p className={styles.warning}>Positive evidence is unavailable. Read-only history and cancellation remain available; this record cannot establish verified source results.</p>}
    {authorization && <ResearchJSON title="Original disclosure, policy and resource reservation" value={authorization} />}{receipt && <ResearchJSON title="Exact completion, usage and typed transformation receipt" value={receipt} />}<ResearchJSON title="Full retained run and original action" value={execution} />
  </>;
}

export function ResearchResultView({ result, inspect, disabled }: { result: ResearchResult; inspect: (source: ResearchSource) => void; disabled: boolean }) {
  const { batch } = result;
  return <section className={styles.section}><h3>Source evidence: {result.verification}</h3><p className={styles.help}>Verified means the retained transport, source and transformation evidence matches its owned artifacts. Search snippets are discovery observations; only fetched, complete extracted documents can support citations.</p><ResearchReasons codes={result.reasonCodes} />
    {batch && <><p>{batch.complete ? "The admitted bounded collection completed." : "The collection is incomplete; inspect the explicit reasons."} {batch.documents.length} document records and {batch.mergedResults.length} deduplicated search results are retained.</p><ResearchReasons codes={batch.reasonCodes} /><ResearchJSON title="Settled usage, origin and transformation declaration" value={{ usage: batch.usage, typedTransformation: batch.typedTransformation }} />
      <h4>Fetched documents and exclusions</h4>{batch.documents.length === 0 && <p className={styles.help}>No fetched document records were produced by this attempt.</p>}<ul className={styles.history}>{batch.documents.map(source => <li key={source.id}><ResearchLink url={source.requestedUrl} /><p><span className={styles.badge}>{source.status}</span> · retrieved {source.retrievedAt ?? "not fetched"}</p><ResearchReasons codes={source.reasonCodes} /><code>{source.id}</code><div className={styles.actions}><button disabled={disabled || source.extractionArtifactId === null} onClick={() => inspect(source)}>Inspect retained extracted source</button></div></li>)}</ul>
      <ResearchSpecialistResults results={batch.mergedResults} /><ResearchJSON title="Search observations with provider/rank lineage (not citable snippets)" value={batch.mergedResults} /><ResearchJSON title="Retained request and response receipts" value={batch.requests} /><ResearchJSON title="Exact verified batch" value={batch} />
    </>}
  </section>;
}

export function ResearchCitationView({ citation }: { citation: ResearchCitation }) {
  return <section className={styles.notice}><h4>Verified exact quote</h4><pre>{citation.quote}</pre><ResearchLink url={citation.resolvedUrl} /><dl className={styles.facts}><dt>UTF-8 range</dt><dd>{citation.startByte} ≤ byte &lt; {citation.endByte}; lines {citation.lineStart}–{citation.lineEnd}</dd><dt>Retrieved</dt><dd>{citation.retrievedAt}</dd><dt>Published</dt><dd>{citation.publishedAt ?? "Unknown"}{citation.publicationProvenance ? " · source reported" : ""}</dd><dt>License</dt><dd>{citation.license}</dd><dt>Quote digest</dt><dd><code>{citation.quoteSha256}</code></dd><dt>Citation digest</dt><dd><code>{citation.sha256}</code></dd></dl><ResearchJSON title="Exact citation, scope and source locations" value={citation} /></section>;
}

export function ResearchComparisonView({ comparison }: { comparison: ResearchComparisonEnvelope }) {
  const { report } = comparison;
  return <section className={styles.notice}><h3>Cited-source comparison: {report.status}</h3><p>This historical report compares the exact selected quotes. “Supported” means literal cited-source agreement for the declared claim key; it does not establish truth, label entailment or completion of the information need.</p><dl className={styles.facts}><dt>Evidence event</dt><dd><code>{comparison.evidenceId}</code></dd><dt>Evaluation time</dt><dd>{report.evaluatedAt}</dd><dt>Need revision</dt><dd><code>{report.needRevisionId}</code></dd><dt>Report digest</dt><dd><code>{report.sha256}</code></dd><dt>Need resolved</dt><dd>No</dd></dl>
    {report.claims.map(claim => <section className={styles.section} key={claim.key}><h4>{claim.label} <span className={styles.badge}>{claim.status}</span></h4><code>{claim.key}</code><p>{claim.citations.length} verified citations; {claim.unavailableCitationCount} unavailable.</p><ResearchReasons codes={claim.reasonCodes} />{claim.values.map((value, index) => <pre key={index}>{value}</pre>)}<ResearchJSON title="Verified citations for this comparison" value={claim.citations} /></section>)}<ResearchJSON title="Exact immutable comparison report" value={report} />
  </section>;
}
