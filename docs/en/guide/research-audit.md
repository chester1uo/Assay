# Research audit for judges

An audit turns a research session into a compact, evidence-linked review packet.
It checks the saved records without calling a model, executing a backend, changing
candidate decisions or consuming experiment budget. The same implementation serves
MCP, REST, the Python workspace, the workbench and offline manifests.

## Run an audit

In the workbench, select a session and click **Audit session**. The panel shows
check results, errors, review items and candidate evidence. **Download audit JSON**
preserves the server response and its content hash. Download the session manifest
alongside it to give a judge the underlying evidence.

MCP clients call the read-only, idempotent `assay_research_audit` tool:

```json
{"session_id":"research_...","offset":0,"limit":20}
```

The deployed HTTP API exposes the same packet:

```bash
curl -H "Authorization: Bearer $ASSAY_MCP_API_KEY" \
  'https://assay.qfinverse.com/v1/research/sessions/research_.../audit?limit=20'
```

Python callers use their existing workspace:

```python
report = service.research.audit_session(session_id)
print(report["record_integrity"], report["finding_counts"])
```

A judge can also audit an exported manifest without a server or market data:

```bash
python -m assay.audit --manifest session.json --output session.audit.json
# Equivalent console entry point after installing the updated package:
assay-audit --manifest session.json --output session.audit.json
```

The CLI returns 1 for failed record checks, 0 for consistent records (including
review warnings), and 2 for invalid command/input. It refuses to overwrite an
existing output file. Offline audits do not import the manifest or open a workspace.

## What is checked

| Check | Evidence |
|---|---|
| Manifest and specification | Envelope/version/hash, dataset hash and record structure |
| Attempts and budget | Unique IDs/request IDs, all terminal and running attempts, spent/remaining budget |
| Provenance | Session/spec links, canonical expression, parent/replay relationships and time order |
| Lifecycle | Candidate decisions point to matching evidence; acceptance needs a completed attempt |
| Diagnostics | Saved lookahead flags, scoring completeness and registered expression policy |
| IC metrics | Finite values, correlation bounds, aligned dates, score-window bounds and series means |
| Portfolio | Saved NAV arithmetic, config/spec consistency, numeric metrics and cost assumptions |
| Local registry | Factor-attempt links and experiment/decision identity columns, in the same SQLite snapshot |

The live database connection is query-only. Cross-session replay sources are checked
when present in that snapshot. An offline audit reports an external source as a
review item, since that source is absent from the exported session. Registry jobs,
score discovery views and usage accounting are outside these registry checks.

Failed and cancelled experiments remain in the counts. A recorded experimental
failure is not itself an audit failure. Running attempts produce a review item;
auditing them neither cancels them nor makes them importable as terminal records.

## Read the result

| Field | Meaning |
|---|---|
| `record_integrity` | `passed` or `failed` saved-record consistency |
| `status` | `failed`, `needs_review` or `passed`; warnings do not change the integrity verdict |
| `checks` / `finding_counts` | Whole-session results, independent of pagination |
| `findings` | Stable codes, severity, experiment ID and JSON Pointer evidence locations |
| `summary` | Goal, dataset, budget, all status counts, decisions and candidate metrics |
| `assessment` | Explicit scope and which judgments have not been performed |
| `manifest_hash` / `audit_hash` | Selected input revision and report content digest |

Unfrozen data specifications and portfolio execution/cost warnings are review items.
They do not imply malformed records. Candidate metrics are descriptive; the audit
does not impose a positive-IC threshold or make acceptance decisions for the judge.
Backend option values are omitted from the compact summary; the original manifest
remains the reference for the complete dataset configuration.

Every record is checked even when only one page is returned. `page.finding_total`
and `page.candidate_total` report separate totals. `offset`/`limit` page both lists;
`has_more` and `candidates_have_more` identify which list continues. Fetch details
with the existing experiment and history tools.

Pass the first response's `manifest_hash` as `expected_manifest_hash` on subsequent
calls, or supply `--expected-manifest-hash` offline. A different snapshot produces
`EXPECTED_MANIFEST_MISMATCH`, preventing a judge from silently combining revisions.
The hashes detect content changes; they do not authenticate an author. Replay,
market-data authenticity/coverage, out-of-sample validity and goal achievement need
their own evidence. The report leaves those judgments explicit in `assessment`.

Related: [agent research](agent-research.md), [MCP](mcp-server.md),
[deployment](deployment.md).
