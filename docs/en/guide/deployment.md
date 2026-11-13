# Deploying Assay

The public base URL is **https://assay.qfinverse.com**. REST and MCP run in the
same API process, use the same authentication and share persistent research state.

| Interface | Public endpoint |
|---|---|
| Web workbench | `https://assay.qfinverse.com/` |
| Streamable HTTP MCP | `https://assay.qfinverse.com/mcp/` |
| Research REST | `https://assay.qfinverse.com/v1/research` |
| OpenAPI / interactive API | `https://assay.qfinverse.com/openapi.json` / `/docs` |
| Connection discovery | `https://assay.qfinverse.com/.well-known/assay.json` |
| Documentation | `https://assay.qfinverse.com/doc/` |
| Liveness / authenticated readiness | `/health` / `/ready` |

The discovery document is Assay-specific metadata. It advertises the interface
version, actual tool count/schema digest and authentication methods. It is not an
OAuth discovery document. Remote clients need support for a Bearer token or
`X-API-Key`; clients that require OAuth-only servers need an additional integration.

## Install and configure

Python 3.12 and the versions in `deploy/constraints.txt` are used by the deployment
acceptance tests. From the release checkout:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -c deploy/constraints.txt '.[api,mcp,perf]'
python scripts/build_doc_hub.py
```

Run the document build with the development interpreter that has `markdown-it-py`,
or ship the already generated static documentation. It is not a runtime dependency.

Use [`deploy/assay.env.example`](../../../deploy/assay.env.example) for the environment,
[`assay.service`](../../../deploy/assay.service) for systemd, and
[`Caddyfile`](../../../deploy/Caddyfile) for TLS and streaming proxying. The templates
assume a dedicated `assay` user, the checkout/venv at `/opt/assay`, configuration at
`/etc/assay/assay.env`, writable local storage at `/var/lib/assay` and prepared,
read-only US data at `/srv/assay/market/us`. Create the directories and assign the
runtime directory to that user before starting the unit. Adjust paths to the host.

Key settings:

```dotenv
ASSAY_DEPLOYMENT=production
ASSAY_PUBLIC_URL=https://assay.qfinverse.com
ASSAY_MCP_HTTP=1
ASSAY_ACCOUNTS=1
ASSAY_CORS_ORIGINS=https://assay.qfinverse.com
ASSAY_DATA_DIR=/var/lib/assay
ASSAY_STATE_DIR=/var/lib/assay/accounts
ASSAY_CONFIG_FILE=/var/lib/assay/config.json
ASSAY_DATA_DIR_US=/srv/assay/market/us
```

Production mode rejects a missing HTTPS public origin, missing authentication or
disabled/unavailable MCP. Configure `ASSAY_PUBLIC_URL` as an origin with no path,
query or embedded credential. The public URL is used in discovery and OpenAPI;
proxy-provided Host values cannot replace it.

When using accounts for a new installation, visit `/login`, then use the first-setup
token from `ASSAY_STATE_DIR/setup_token` to create the administrator. Existing
installations retain their auth database and secret. Researchers join by invitation
and create a personal API key on the Account page. The server needs no LLM key;
the external agent supplies its own model connection.

Start with one API worker. Research SQLite state is persistent, while temporary
panel sessions and cache counters are process-local. The in-process data scheduler
also runs per process. Stateless MCP HTTP does not make these components distributed.
For FactorDB/ADES, additionally configure its writable registry/queue directory,
the read-only QFV panel and worker-only keys; the base template does not enable it.

For existing multi-market deployments, retain `ASSAY_DATA_DIR_CN` and
`ASSAY_DATA_DIR_HK` with their `SNAPSHOT.json`, and verify per-market versions at
`/v1/fdb/markets`. Updated workers advertise all available panels and
`info.lease_token_supported=true`. During a rolling upgrade, legacy workers can
continue ordinary compute jobs; token-fenced research tasks are leased only to
workers supporting that protocol. Upgrade the controller first, followed by at
least one worker supporting the target market.

Corrected `next_close` and newly supported `close` jobs also require upgraded
workers; legacy workers continue ordinary `next_open` jobs. The `next_close`
forward-return cache uses a separate v2 filename so pre-upgrade labels cannot be reused.

## Reverse proxy and transport

Preserve the external `Host`, `Authorization`, `X-API-Key`, `Accept` and MCP protocol
headers. Disable response buffering for `/mcp/` and evaluation streams. Caddy's
template flushes immediately; for nginx use `proxy_buffering off`, HTTP/1.1 and a
read timeout long enough for a complete experiment. Trust forwarded headers only
from the actual proxy, as the systemd command does for loopback.

Use `/mcp/` as the canonical client URL. `/mcp` returns a relative 308 redirect.
The MCP transport validates Host and Origin against the configured endpoint and
allowed browser origins, including equivalent explicit default ports. CORS exposes
the MCP session/protocol headers. Legacy SSE is restricted to local use; the public
endpoint uses Streamable HTTP and the installed SDK's negotiated protocol version.

## Verify the release

Set `ASSAY_MCP_API_KEY` through your normal secret manager or shell, then run:

```bash
PYTHONPATH=src .venv/bin/python scripts/check_deployment.py \
  --base-url https://assay.qfinverse.com \
  --output artifacts/deployment-release/check.json
```

The checker reads health, discovery and OpenAPI; verifies unauthenticated rejection;
checks authenticated readiness and dataset access; initializes a real MCP session;
and calls tool discovery, lint and dataset listing. It creates no dataset, session
or experiment and makes no model calls. No key is written to its report. A missing
key yields an incomplete result and exit code 2, rather than a successful verdict.

`/health` is cheap process liveness. `/ready` verifies that the research database can
enter a write transaction and the MCP transport is available; it requires the same
credentials as REST. It does not certify market coverage. Before admitting a mining
job, separately check backend availability, its requested data/calendar window and
a small authorized factor experiment against the prepared data. Lint alone does
not exercise factor execution or portfolio accounting.

Before switching traffic, run the regression suite and verify a new authenticated
research session through MCP and REST. Preserve its request ID and confirm the same
attempt after restart. The tests in `tests/api/test_deployment.py` exercise real TCP
MCP connections; existing research transport tests cover restart, budgets and migration.

## Persistent state and release boundaries

Back up `/var/lib/assay`, including research and account databases, `auth_secret`,
configuration and the factor library. Cache files may be rebuilt. Use SQLite's
backup API or a stopped service for database copies; copying a live database file
alone is not a consistent backup. Keep a protected pre-upgrade snapshot and restore
it with the previous release when rolling back across schema changes.

When account authentication is enabled, personal accounts use private research,
library, combination and FactorDB registries. Service keys continue to use the
original shared workspace. Back up the account database and account directories
together; existing browser sessions must sign in again after the workspace-identity
migration. ADES workers are trusted with the expressions they execute. Temporary
panel sessions remain process-local, and account-level CPU, memory and storage hard
quotas are not yet enforced; use separate instances when those boundaries are needed.

The read-only market-data/atomic-publication contract for compute reuse is described
in [performance](performance.md). Full experiment results can be large; use compact
research context and the paginated experiment/history endpoints for routine agent reads.

Client examples: [MCP](mcp-server.md), [reference agent](reference-agent.md),
[REST](rest-api.md).
