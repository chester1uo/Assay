# Anonymous reviewer demo

`assay.api.reviewer_demo:app` runs a guest workbench in a **separate process**.
It must use an independent `ASSAY_DATA_DIR`, `ASSAY_STATE_DIR`,
`ASSAY_CONFIG_FILE` and FactorDB directory, with no production account DB,
secret key, API keys or provider credentials inherited. A read-only copy of the
market panel may be mounted at `ASSAY_QFV_PANEL_DIR`.

Set `ASSAY_ACCOUNTS=1`, `ASSAY_DEPLOYMENT=production`, `ASSAY_PUBLIC_URL` to the
public HTTPS origin, and `ASSAY_DEMO_MANIFEST` to a validated exported research
session manifest. Each new visitor gets a distinct workspace initialized from
that manifest. Example launch:

```sh
uvicorn assay.api.reviewer_demo:app --host 127.0.0.1 --port 19421 \
  --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1,::1
```

The reverse proxy must send `/demo` and `/demo/*` to the demo process and route
subsequent requests bearing `assay_review_route=1` there. All other requests
remain on the regular service. For Caddy, add these handlers before the normal
fallback handler:

```caddyfile
@demo path /demo /demo/*
@review header_regexp Cookie (^|;[[:space:]]*)assay_review_route=1(;|$)
handle @demo {
    reverse_proxy 127.0.0.1:19421
}
handle @review {
    reverse_proxy 127.0.0.1:19421
}
handle {
    reverse_proxy 127.0.0.1:19420
}
```

A guest session expires 15 days after creation. Expired guest accounts and
workspaces are reclaimed on a subsequent `/demo` visit after an additional
day's grace. The page links to `/demo/exit`, which removes the routing cookie and
returns visitors to the main application. Guests can also use MCP with the
cookies returned by `/demo`; no API key is necessary for that session.

The demo limits individual request size, batches, worker count, concurrent API
calls and per-IP request rates. Run exactly one worker per demo instance and
also enforce CPU, memory and task limits in the service manager. The account
capacity defaults to 1,000 and can be set with `ASSAY_DEMO_MAX_USERS`. These
limits are designed for a small review demonstration, not an open multi-tenant
production deployment. The manifest should contain only information intended
for public access.
