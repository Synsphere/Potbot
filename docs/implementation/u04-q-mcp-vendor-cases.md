# U04-Q MCP case design and vendor boundaries

Primary sources inspected on **18 November 2026**. This document identifies
useful qualification cases and the configuration they still require. A source
inspection or authored fixture is not a completed run against an installed
vendor server. Qualification binds the exact installed profile, catalogue
reference, task, arguments, oracle and gateway evidence through private files,
immutable artifacts and hashes. Public reports omit private arguments and
expected result values.

## Independent result checks

`potbot-mcp-result-oracle/v1` accepts an exact `schema` and an `assertions`
array. Each assertion contains exactly `op`, `pointer` and `value`.

| Operation | Check |
| --- | --- |
| `equals` | Canonical JSON equality, preserving distinctions such as `true` versus `1` and `1` versus `1.0`. |
| `contains` | A nonempty literal substring of a selected string. This does not interpret regular expressions, code or instructions. |
| `length_equals` | The exact number of array elements or Unicode characters in a selected string. Objects and scalar numbers do not satisfy it. |

Pointers use RFC 6901 over `{content, structuredContent, isError}` reconstructed
from the existing gateway's retained output. For example,
`/structuredContent/sum` selects a structured arithmetic result and
`/content/0/text` selects the first content block's text. Missing paths are
distinct from JSON `null`. Array indices must be existing canonical decimal
indices; `-`, negative indices, leading zeros and wildcard selectors are not
supported. The empty pointer selects the whole normalized result object.

Limits are 1–16 assertions, 64 KiB for an oracle, 16 KiB for one expected value,
1,024 UTF-8 bytes and 16 segments per pointer. Retained output is bounded to
2 MiB, depth 32 and 8,192 JSON nodes. Duplicate keys, nonfinite values, malformed
UTF-8 and invalid envelopes are rejected. These are intentionally finite data
checks with no imports, expressions, subprocesses, remote schemas or URL reads.

Before evaluating assertions, the verifier requires a successful
`ToolObservation` with the expected invocation, existing SYN/Synny result Type
and exact canonical MCP result hash. Failed, indeterminate or modified output
cannot pass by including a field such as `success: true`. The native owner must
independently verify the gateway receipt, stopped process and retained artifact;
the pure oracle does not authenticate those owners.

The resulting `potbot-mcp-result-check/v1` record contains oracle/output hashes,
an overall Boolean and indexed checks with fixed reason codes. It omits the
selected values, expected values, arguments and raw output. A passing check
proves the declared assertion about this retained result. It does not establish
general task usefulness, provider trust, isolation or permission for another call.

## Candidate case matrix

| Integration | Suitable bounded case | Independent expectation | Remaining prerequisite |
| --- | --- | --- | --- |
| Authored protocol fixture | Call the existing integer addition fixture with fixed inputs. | An independently calculated structured sum, including an intentionally wrong expected sum that must fail. | This is protocol/controller acceptance, with explicit fixture classification. |
| DanceFlow | Retrieve the packaged `visual-language` workflow using `danceflow_get_workflow`. | Exact title, `execution: instructions_only` and the full body from the independently pinned source file. | An admitted installation of the source/release containing that interface and a current exact tool reference. |
| DanceFlow | Search the component catalogue with `danceflow_search`, `kind=component`, `limit=100`. | The pinned source's component inventory and its expected 15 results. | Exact version pin; changes to the catalogue require a reviewed replacement case. A count check alone does not prove task usefulness. |
| WordPress MCP Adapter | Inspect a specifically selected public ability using `mcp-adapter-get-ability-info`. | The selected ability identifier and independently reviewed input/output schemas. | A real site, explicit site/user scope, an installed public ability and a supported admitted authentication route. |
| A selected IBM MCP server | Query a fixed read-only dataset or inspect an exact documented interface for that product. | A known result from independently provisioned test data. | Select and pin the actual server/product, transport, credentials, data and tool schema first. The IBM catalogue alone supplies none of these. |

All inputs and expectations must be reviewed before dispatch. A server's own
`source.sha256`, success flag, tool description or `readOnlyHint` cannot provide
its independent oracle or confer an effect permission. Resource links and
artifact URIs are not fetched by this verifier; inspecting metadata about an
artifact does not verify its external bytes or visual/creative quality.

## DanceFlow source boundary

The inspected [MCP ecosystem guide](https://github.com/kieransimkin/DanceFlow/blob/a424100b4293d83bf9aa4044d64f0ea753770a59/docs/mcp-ecosystem.md)
is pinned to `a424100b4293d83bf9aa4044d64f0ea753770a59`. It distinguishes that
source from the previously published `0.1.16` package. Its discovery tools return
catalogue data and workflow instructions; retrieving a workflow does not run
it or install its renderer, model or optional native engine.

The [source tests](https://github.com/kieransimkin/DanceFlow/blob/a424100b4293d83bf9aa4044d64f0ea753770a59/tests/test_discovery.py)
check the 15-component catalogue and the `visual-language` result. Its expected
body is available independently in
[the pinned workflow document](https://github.com/kieransimkin/DanceFlow/blob/a424100b4293d83bf9aa4044d64f0ea753770a59/docs/workflows/visual-language.md).
Compare that body as reviewed expected data rather than accepting a hash claimed
by the responding server. Newer source, an umbrella tool listing or an installed
hub cannot qualify every underlying DanceFlow engine or native workflow.

## WordPress naming, effects and authentication

At inspected commit `dbddfed96406f6dbffd2404f1fb07a33d5c2aee7`, the
[default-server contract](https://github.com/WordPress/mcp-adapter/blob/dbddfed96406f6dbffd2404f1fb07a33d5c2aee7/docs/guides/default-server.md)
uses these MCP tool names:

- `mcp-adapter-discover-abilities`
- `mcp-adapter-get-ability-info`
- `mcp-adapter-execute-ability`

WordPress ability identifiers use slashes, such as
`mcp-adapter/get-ability-info`; they are distinct from the MCP names above.
Only exposed abilities are visible through the default server. An empty public
ability list is a possible result, not proof that a useful content task succeeded.

The execute meta-tool can dispatch different underlying abilities, including
mutations. Do not classify that entire tool as read-only because one selected
ability happens to read data. Each exact installed tool and approved arguments
must retain their existing effect and capability authority. Discovery/get-info
provide narrower initial inspection cases.

The pinned [site-owner guide](https://github.com/WordPress/mcp-adapter/blob/dbddfed96406f6dbffd2404f1fb07a33d5c2aee7/docs/getting-started/site-owners.md)
describes username/Application Password authentication and explains that the
adapter itself does not add an OAuth server. Potbot's current registered
OAuth/JWT HTTP profile therefore does not automatically support a default
WordPress deployment. Keep that route unavailable until its actual supported
authentication and host path are admitted; do not put Basic credentials in a
task manifest, weaken the existing broker or invent an OAuth issuer. WP-CLI
stdio is also a separate installed-host configuration, not an implicit fallback.

## IBM product selection

The inspected [IBM MCP catalogue](https://github.com/IBM/mcp/blob/a6b8c9c82a5504dd6240d2e9658622ea0f2ea757/README.md)
at `a6b8c9c82a5504dd6240d2e9658622ea0f2ea757` lists multiple servers, clients
and developer tools. ContextForge is one entry, not a universal identity or
tool schema for all IBM services. Qualification must name the exact selected
product and immutable implementation, and retain its dataset, authority and
observed interfaces. No IBM service, hosted account or live provider is qualified
by this catalogue inspection.

## Evidence labels

Keep authored fixture results, pinned-source inspection, actual installed-native
execution, vendor interoperability and useful task outcome evidence separate.
A configured test which cannot obtain its required host, authentication or
dataset is unavailable or blocked. It must not turn into a passing substitute
fixture, a skipped mandatory gate or a global compatibility claim.
