# mcpify — Deep Usage Guide

Everything beyond the README quick start: authentication patterns, scoping,
deployment, troubleshooting, and frequently asked questions.

## Table of contents

1. [Choosing a spec source](#1-choosing-a-spec-source)
2. [Authentication patterns](#2-authentication-patterns)
3. [Scoping which operations are exposed](#3-scoping-which-operations-are-exposed)
4. [Connecting different MCP clients](#4-connecting-different-mcp-clients)
5. [Running behind Docker / production](#5-running-behind-docker--production)
6. [How tool naming works](#6-how-tool-naming-works)
7. [Troubleshooting](#7-troubleshooting)
8. [FAQ](#8-faq)
9. [HTTP transport: serve a team](#9-http-transport-serve-a-team)
10. [Trying tools without an agent (`mcpify try`)](#10-trying-tools-without-an-agent-mcpify-try)
11. [Sharing a preconfigured server (`mcpify output-server`)](#11-sharing-a-preconfigured-server-mcpify-output-server)
12. [Multi-API aggregation: one server, several APIs](#12-multi-api-aggregation-one-server-several-apis)
13. [Dashboard, metrics, mock & hot reload](#13-dashboard-metrics-mock--hot-reload)
14. [Governance & upgrades: diff, audit, RBAC, plugins, tracing](#14-governance--upgrades-diff-audit-rbac-plugins-tracing)

---

## 1. Choosing a spec source

`mcpify list` and `mcpify serve` accept both a local file and an https URL:

```bash
mcpify list ./openapi.json
mcpify list https://api.example.com/openapi.json
```

- **JSON specs** work out of the box.
- **YAML specs** need the optional extra: `pip install 'mcpify[yaml]'`.
- **Swagger 2.x** documents are accepted (the `swagger` root field is
  recognized), but OpenAPI 3.x is the well-tested happy path.
- Specs with **external `$ref` documents** are not resolved over the
  network. Bundle the spec first (most generators have a "bundle" option).

### Multiple declared servers: `--server`

Specs often declare several `servers[]` entries (prod, staging, dev).
Pick one by 1-based index or by name (matched against each entry's
description, or its URL as a substring):

```bash
mcpify serve spec.json --server 2          # the second declared server
mcpify serve spec.json --server staging    # description or URL match
mcpify status spec.json --server prod      # works for status too
```

Precedence: `--base-url` (explicit override) beats `--server`, which
beats the `servers[0]` default. A wrong choice fails with the full
numbered listing of what the spec declares. The selected entry's server
variables still need defaults (or `--base-url`). `mcpify doctor` hints
at this whenever a spec declares more than one server, and the `server`
key is accepted in config files.

## 2. Authentication patterns

Credentials are **never** read from the spec, a CLI flag, or the model's
context — only from an environment variable at call time.

### Bearer token (most common)

```bash
export ACME_TOKEN="ey..."
mcpify serve acme.json --base-url https://api.acme.com \
  --auth-env ACME_TOKEN --auth-style bearer
```

Every outgoing request carries `Authorization: Bearer ey...`.

### Custom header (API keys)

```bash
mcpify serve acme.json --auth-env ACME_KEY --auth-style header --auth-name X-API-Key
# -> X-API-Key: <value>
```

### Query parameter

```bash
mcpify serve acme.json --auth-env ACME_KEY --auth-style query --auth-name api_key
# -> https://api.acme.com/v1/pets?api_key=<value>
```

### Auto-detected from the spec (the common case)

The spec's `security` declarations already say how the API authenticates.
Give mcpify a credential and it wires the rest itself:

```bash
mcpify serve spec.json --auth-env API_TOKEN     # bearer? basic? header? query? — read from the spec
```

The detection covers `http: bearer`, `http: basic`, `apiKey` in header
or query (with the declared name), Swagger 2.0 `securityDefinitions`,
and operation-level security when the top level is silent. What you'll
see:

- the chosen style is echoed to stderr
  (`auth: style auto-detected from the spec -> header (X-API-Key)`)
- serving a secured spec **without** a credential prints the exact
  flags to run — `mcpify doctor` shows them too
- an explicit `--auth-style` always wins over detection

### HTTP Basic

The env variable holds `username:password`; mcpify produces the
`Authorization: Basic base64(...)` header:

```bash
export CREDS="svc-account:secret"
mcpify serve spec.json --auth-env CREDS --auth-style basic
```

### No auth / public APIs

Omit all auth flags. Nothing is injected.

### What happens when the variable is missing

The tool call fails **before** any network request with:
`environment variable 'ACME_TOKEN' is not set (required for API
authentication)`. The agent sees this as a tool error and can tell you —
it never sends an unauthenticated request blindly.

> **Rotation tip:** because the variable is read on every call, rotating
> the secret requires no server restart — export the new value in the
> environment where the server process runs.

### OAuth2 client-credentials (RFC 6749 §4.4)

For APIs fronted by an OAuth2 identity provider. mcpify talks the
**client-credentials grant**: it fetches an access token from your IdP's
token endpoint, caches it in memory until shortly before `expires_in`,
refreshes it transparently, and — if the API answers 401 once — drops the
token and fetches a fresh one before retrying that single call.

```bash
export OAUTH2_CLIENT_ID="my-service"
export OAUTH2_CLIENT_SECRET="s3cret"
mcpify serve acme.json \
  --oauth2-token-url https://idp.acme.com/oauth2/token \
  --oauth2-client-id-env OAUTH2_CLIENT_ID \
  --oauth2-client-secret-env OAUTH2_CLIENT_SECRET \
  --oauth2-scope "read write"          # optional
```

Details that matter in production:

- **Secrets stay out of flags and config files.** Only the *names* of the
  environment variables travel on the command line; the values are read
  at call time, like every other credential.
- **Client auth style:** HTTP Basic is the default. Some token endpoints
  reject Basic — use `--oauth2-client-auth body` to put the credentials
  in the form body. Public clients (no secret) simply omit
  `--oauth2-client-secret-env`.
- **`--auth-env` and `--oauth2-*` are mutually exclusive** — one credential
  mode per server; mcpify refuses the mix with a clear error.
- `mcpify_health` reports the full OAuth2 configuration (token URL, which
  env variables are set) so misconfigurations are visible at a glance.
- When `expires_in` is absent from the token response, the RFC-recommended
  3600 s is assumed; a wrong assumption self-heals on the first 401.

## 3. Scoping which operations are exposed

Giving an agent *everything* is rarely what you want. Six filters combine:

| Flag | Effect |
| ---- | ------ |
| `--read-only` | Only `GET` operations (a heuristic — see below) |
| `--tag payments` | Only operations with that OpenAPI tag |
| `--include /v1/orders` | Only this path prefix (repeatable) |
| `--exclude /admin` | Everything except this prefix (repeatable) |
| `--allow REGEX` | Re-include operations that `--read-only` dropped |
| `--deny REGEX` | Never expose matching paths — wins over everything |

`--include` and `--exclude` combine; excludes win. Deny beats allow.

### GET-only is not side-effect-free

Method filtering is a heuristic. Real APIs break it in both directions:

- **GETs that mutate** — `/admin/reset-cache` is a GET but flushes state.
- **Reads that use POST** — `/search` with a request body touches nothing.

The policy layer handles both honestly:

```bash
mcpify serve api.json \
  --read-only \
  --deny '/admin' \
  --allow '/search'
```

Treat the output of `mcpify list` as the permission boundary: whatever it
shows is what the agent can call. If a GET looks suspicious even when
read-only, deny it explicitly.

```bash
# A safe "reporting" server: only reads, no admin paths
mcpify serve acme.json --read-only --exclude /admin --exclude /internal
```

Preview the result first with the same filters:

```bash
mcpify list acme.json --read-only --exclude /admin
```

The `Authorization` header parameter is always stripped from advertised
schemas — clients cannot inject their own credentials through tool arguments.

## 4. Connecting different MCP clients

### Claude Code (CLI)

```bash
claude mcp add acme -- mcpify serve acme.json --auth-env ACME_TOKEN --read-only
```

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "acme": {
      "command": "mcpify",
      "args": ["serve", "/absolute/path/acme.json", "--auth-env", "ACME_TOKEN"],
      "env": { "ACME_TOKEN": "..." }
    }
  }
}
```

### Cursor / Windsurf / any stdio client

Same shape — `command` + `args` pointing at `mcpify serve ...`. The server
speaks the standard MCP stdio transport (newline-delimited JSON-RPC 2.0).

### One API, many scopes

Register the same spec twice with different flags:

```json
{
  "mcpServers": {
    "acme-readonly": { "command": "mcpify", "args": ["serve", "acme.json", "--read-only"] },
    "acme-payments": { "command": "mcpify", "args": ["serve", "acme.json", "--tag", "payments"] }
  }
}
```

## 5. Running behind Docker / production

```dockerfile
FROM python:3.12-slim
RUN pip install git+https://github.com/furkan708/mcpify.git
ENTRYPOINT ["mcpify"]
```

```bash
docker run -i --rm \
  -e ACME_TOKEN="$ACME_TOKEN" \
  -v "$PWD/specs:/specs:ro" \
  my-mcpify serve /specs/acme.json --auth-env ACME_TOKEN
```

Notes for production:

- stdio MCP servers are meant to be **spawned by the client**, not exposed
  over the network. Do not wrap them in a TCP listener without adding auth.
- `--timeout` (seconds, default 30) bounds each API call; tune for slow APIs.
- The server keeps zero state between calls — restarts are free.

## 6. How tool naming works

- `operationId` is preferred, slugified (`getPetById` → `get_pet_by_id`).
- Without an `operationId`, the name falls back to `method_path`
  (`GET /pets/{petId}` → `get_pets_petid`).
- Duplicates get numeric suffixes (`same`, `same_2`).
- Run `mcpify doctor` to see how many operations lack ids/summaries —
  fixing those makes agent interaction dramatically better.

### Deliberate omissions

- **Automatic retries are opt-in and conservative.** `--retry N` re-attempts
  idempotent requests (GET/PUT/DELETE) only, and only on 502/503/504 or
  connection failures, capped at 5. POST/PATCH are **never** retried —
  a non-idempotent retry can duplicate side effects. Rate limiting and
  circuit breaking remain gateway territory (they need shared state and
  observability a single stdio process should not fake).
## 7. Troubleshooting

| Symptom | Cause / fix |
| ------- | ----------- |
| `not an OpenAPI document` | The URL returned HTML (a login page?). Check the URL. |
| `no base URL` | Spec has no `servers` — pass `--base-url https://...`. |
| `unresolvable reference` | External `$ref` — bundle the spec first. |
| `YAML specs need PyYAML` | `pip install 'mcpify[yaml]'`. |
| Agent calls never arrive | Check the client config command resolves (`which mcpify`). |
| `HTTP 401` from calls | Env var set but wrong/rotated; or the API expects a different auth style. |
| `HTTP 404` on every call | Base URL path duplication — spec paths usually already include a version segment. |
| `timeout: no response within Ns` | The upstream accepted the connection but was slower than the client-side `--timeout` — re-run, or raise `--timeout`. This is a tool error, not a dead server: the session stays alive. |
| `connection failed` mid-session | Upstream reset the connection (deploy? restart?). Retry; if it persists, check the base URL and network. |

## 8. FAQ

**What about non-JSON bodies, server variables, and huge responses?**
- `multipart/form-data` or other non-JSON request bodies are exposed as a
  raw `body` string argument, sent as-is with the spec's content type.
- Server URL variables (`https://{host}/v2`) are substituted from their
  declared defaults; variables without defaults or relative server URLs
  fail at startup with a clear "pass --base-url" message instead of
  producing broken requests.
- Responses larger than ~40k characters are truncated with a note, so one
  tool call cannot flood the model's context.

**Does mcpify cache or store anything?**
No. It is a stateless translation layer between MCP and your API.

**Can the agent see my API key?**
No. Keys live in the server process environment and are injected into
outgoing requests only. They never appear in tool descriptions or results.

**POST bodies?**
Exposed as a single `body` object argument whose shape comes from the
spec's `requestBody` schema (with `$ref`s resolved).

**Is a dynamically generated server safe for production?**
The tool surface is built **once at startup** from your spec and never changes
while the process runs — so a running server is as stable as a handwritten one.
For production, pin the spec: commit the exact file you reviewed and serve that
path (not a live URL), add `mcpify doctor` to CI, and set the exposure policy
explicitly (`--read-only` with `--allow`/`--deny`). The OpenAPI spec is already
a versioned contract; mcpify projects it, it doesn't invent a moving surface.

**Does it support SSE/streaming MCP transports?**
Currently stdio only — which is what Claude Code, Cursor, and Claude
Desktop spawn. If you need HTTP transport, put a transport shim in front.

**Multiple servers, one spec?**
Run one `mcpify serve` per scope (see §4). Processes are cheap and isolated.


## Agent-grade surface (v1.2.0)

These features exist for one reason: raise the rate at which an agent's
tool calls succeed — and let clients spend less context doing it.

### Tool annotations from HTTP semantics

Every tool carries MCP annotations derived from its HTTP method. Clients
use them for approval routing (read-only tools can be auto-approved,
destructive ones prompt):

| Method | readOnlyHint | destructiveHint | idempotentHint |
|--------|--------------|-----------------|----------------|
| GET    | true         | false           | true           |
| PUT    | false        | false           | true (replacement) |
| DELETE | false        | true            | true (RFC 9110 idempotent) |
| POST/PATCH | false    | false           | false          |

`openWorldHint` is always true (the tool reaches an external API); `title`
comes from the operation summary. Hints describe the HTTP method — never
your deployment's intent.

### Structured output

If an operation documents an `application/json` body for a 2xx response,
its tool declares an `outputSchema` and successful calls return
`structuredContent` (plus the same JSON as text for back-compat). No JSON
schema documented → no declaration: mcpify never promises what the spec
doesn't. Error results stay text-only (spec-exempt), and a non-JSON body
against a declared schema is reported as a tool error — a broken promise
is a protocol violation, so it's surfaced instead.

### Remediation-grade errors

HTTP errors carry corrective guidance so the *next* call succeeds:
FastAPI-style `detail` arrays become named validation lines, 401/403 point
at `--auth-env`, 429 reports `Retry-After` (mcpify never retries
automatically), 5xx blames the upstream, and 404s suggest the closest
known paths.

### Lazy mode for large APIs (`--lazy`)

```bash
mcpify serve huge-spec.json --lazy
```

The listing shrinks to three meta tools: `mcpify_search_tools` (keyword +
tag search over compact entries), `mcpify_get_tool_schema` (full definition
of one tool), and `mcpify_call_tool` (executes it). Measured against the
live api.weather.gov document (69 tools): **38,882 → 1,741 characters in
tools/list — a 95.5% reduction** — with search ranking `gridpoint forecast`
to exactly `gridpoint_forecast` first. Meta names are reserved
(`mcpify_*`); a spec operation using one of them gets the usual `_2` suffix
instead of shadowing anything.

### Dry-run previews (`--enable-preview`)

Adds `mcpify_preview_request`: given a tool name and arguments, it prints
the exact request that would be sent — method, URL, headers, body — with
credentials masked and **nothing sent**. Use it to audit what an agent is
about to do, or to let it plan before acting.

## Which MCP protocol versions does mcpify speak?

Both generations, on the same stdio wire:

- **Classic (2025-06-18):** the client sends `initialize` +
  `notifications/initialized`; requests before the handshake get the
  `-32002` lifecycle error.
- **Stateless (2026-07-28):** the handshake was removed from the spec —
  every request carries its version in
  `_meta["io.modelcontextprotocol/protocolVersion"]`. mcpify accepts
  those requests directly, no handshake needed.

Nothing to configure; the server detects which generation the client
speaks per request.


## Configuration file

Serve settings can live in a config file: `.mcpify.toml` (preferred),
`.mcpify.yaml` (needs the `mcpify[yaml]` extra) or `.mcpify.json`.
mcpify auto-discovers them in the current directory, or take an explicit
one with `--config`:

```toml
# .mcpify.toml
[serve]
spec = "openapi.json"
base-url = "https://staging.example.com"
auth-env = "API_TOKEN"
auth-style = "bearer"
read-only = true
cache-ttl = 30
retry = 2

[envs.prod]
base-url = "https://api.example.com"
read-only = true

[envs.dev]
base-url = "http://localhost:8080"
read-only = false
```

```bash
mcpify serve --env prod     # flags > [envs.prod] > [serve] > defaults
```

Unknown keys are reported as warnings (typos never vanish silently).
Every setting maps 1:1 to a CLI flag; flags always win.

Multiple APIs in one process? See §12 — `[apis.NAME]` sections give
each document its own credential, policy and filters.

## `mcpify init` — setup wizard

```bash
mcpify init
```

Asks for the spec (bare origins are auto-discovered), base URL, auth
style and env variable, read-only mode, cache/retry, and writes
`.mcpify.toml`. Prefill any answer with `--spec` / `--base-url`.

## Operational flags

- `--server INDEX|NAME` — pick among the spec's declared `servers[]`
  entries (see §1); `--base-url` overrides it
- `--wait-on-429 SEC` — opt-in rate-limit courtesy: on 429 for an
  idempotent call, wait `min(Retry-After, SEC)` seconds ONCE and retry;
  a wait beyond the cap returns the 429 so the agent can decide.
  POST/PATCH are never auto-waited. Off by default — mcpify still never
  retries anything automatically unless you ask

```bash
mcpify serve spec.json \
  --verbose \          # per-call log lines on stderr
  --log-file ops.log \  # same lines appended to a file
  --cache-ttl 30 \      # GET+200 responses cached in memory for 30s
  --retry 3 \           # idempotent requests retried on 502/503/504
  --retry-delay 1 \
  --strict \            # every advertised argument becomes required
  --format auto          # XML responses (per Content-Type) become JSON
```

- **Logging** never touches the JSON-RPC stream; URLs are logged without
  query strings and `Authorization` values are masked. Response bodies
  are excerpted (first 1000 chars) — point `--log-file` somewhere private.
- **Cache** is in-memory, bounded (256 entries), GET+200 only. Distinct
  arguments are distinct entries; expiry is per-TTL.
- **Retry** is idempotent-only by design (see Deliberate omissions).
- **`--format auto`** trusts the response Content-Type; `xml` forces
  conversion even without the header. Namespaces are simplified —
  this is an agent convenience, not a lossless XML tool.

## Auto-discovery

```bash
mcpify serve https://api.example.com
```

A bare origin is probed for `/.well-known/openapi.json`, `/openapi.json`,
`/swagger.json`, `/openapi.yaml` and `/api-docs`. Nothing found → a clear
error listing every path tried.

## 9. HTTP transport: serve a team

`--http` switches the *same* server (same tools, same policy, same auth)
from stdio to MCP Streamable HTTP — one process your whole team or
gateway points at:

```bash
# local try-out (binds 127.0.0.1)
mcpify serve acme.json --http 8080

# shared deployment: bind all interfaces AND require a bearer token
mcpify serve acme.json --http 0.0.0.0:8080 --http-token "$SHARED_TOKEN"

# the token can also come from the environment
export MCPIFY_HTTP_TOKEN="$SHARED_TOKEN"
mcpify serve acme.json --http 0.0.0.0:8080
```

Behavior, precisely:

- clients **POST** JSON-RPC to any path (`http://host:8080/` is fine);
  responses are `application/json`
- **stateless** per the current MCP spec: no session ids, no server-side
  sessions — horizontal scaling needs no sticky routing
- notifications (messages without an `id`) get `202 Accepted`
- `GET`/`DELETE` → 405 (there is no server-initiated stream to accept);
  `OPTIONS` → 204 with the allowed methods
- without a token, requests are unauthenticated; binding a non-loopback
  address without `--http-token` prints a prominent warning — put the
  token in front of anything reachable by others
- body cap: 10 MB per request (`413` beyond); missing `Content-Length`
  → 411; wrong `Content-Type` → 415
- one JSON-RPC message per request: batching was removed from the spec
  and arrays are rejected with `-32600` (the stdio transport still
  tolerates legacy batched lines for gateways)
- transport problems are HTTP status codes; JSON-RPC problems are error
  bodies agents can read

Docker users: the container listens on stdio by default; pass
`--http 0.0.0.0:8080` and publish the port to run it as a shared service.


## 10. Trying tools without an agent (`mcpify try`)

`try` takes the same flags as `serve` and opens an interactive REPL in
your terminal — no MCP client required:

```bash
$ mcpify try acme.json --read-only
mcpify try — 8 tools. Type :h for help, :q to quit.
   1. list_pets [read-only]     GET      /pets
   2. create_pet                POST     /pets
   3. get_pet [read-only]       GET      /pets/{petId}
mcpify try> 3
→ get_pet
  petId* (integer): 7
  ✓ (0.12s)
  {
    "id": 7,
    "name": "Pet7"
  }
mcpify try> :q
```

Commands: `<number>`/`<name>` selects a tool, `:raw NAME {"json": "args"}`
calls with inline JSON, `:info [NAME]` shows the full schema, `:ls`
re-lists, `:q` quits (Ctrl+C/D work too). Arguments are prompted field by
field with the schema's types, enums and defaults — a wrong type is
re-asked, not silently dropped.

**What you see is what an agent gets:** `try` runs the identical
execution path as MCP `tools/call` — same auth, retry, cache, truncation,
remediation. It is also the fastest way to sanity-check credentials and
scopes before wiring the server into a client.


## 11. Sharing a preconfigured server (`mcpify output-server`)

Bake a serve command into a small, readable Python script:

```bash
mcpify output-server acme.json -o server.py -- \
  --base-url https://api.acme.com \
  --auth-env ACME_TOKEN --read-only --retry 2
```

Teammates run `python3 server.py` and get the identical MCP server — no
flags to remember, no docs to re-read. The script:

- **embeds a local spec** (base64 of the raw file — quoting can never
  break) or **keeps a remote URL** fetched at startup
- calls the public mcpify CLI, so its behavior always matches the
  installed version (header states: requires `mcpify-openapi >= 1.6`)
- never embeds credential **values** — only environment-variable names
  travel in the script; `--http-token` is the one flag that would embed a
  secret, so generation warns loudly if you bake it
- refuses to overwrite an existing file without `--force`, and validates
  the flags you pass after `--` against the real serve parser — typos
  fail at generation time, not at your teammate's desk


## Batch requests (legacy tolerance)

The current MCP spec removed JSON-RPC batching, but some gateways still
send arrays. mcpify accepts a JSON array on one stdio line, processes
notifications in order, and runs same-batch `tools/call` entries
concurrently (thread pool, locked cache).

## 12. Multi-API aggregation: one server, several APIs

`[apis.NAME]` sections turn one `mcpify serve` process into a front for
multiple OpenAPI documents. Every API keeps its own credential, policy,
caching and retry settings; the model sees one merged tool surface.

```toml
# .mcpify.toml
[apis.catalog]
spec = "https://shop.example.com/openapi.json"
auth-env = "CATALOG_TOKEN"
cache-ttl = 60

[apis.crm]
spec = "./crm.yaml"
read-only = true
base-url = "https://crm.internal/v2"

[apis.weather]
spec = "https://api.weather.gov/openapi.json"
timeout = 10
```

```bash
mcpify serve              # stdio: one tool surface, every API
mcpify serve --http 8080  # or one HTTP endpoint for the whole team
mcpify try                # REPL across every API
mcpify status             # probes each API concurrently
```

Rules that make this safe:

- **Naming:** tools keep their names unless two APIs collide — then
  *both* sides get the label prefix (`catalog_list_pets`,
  `crm_list_pets`). Non-conflicting names are never touched, and a name
  that matches a built-in `mcpify_*` tool is reserved.
- **Routing:** each call executes against its owning API with that
  API's auth, cache, retry and filter settings. A `read-only` section
  doesn't leak into other APIs.
- **Env selection:** `--env prod` (or `default-env`) still applies —
  `[envs.*]` sits on the `[serve]` layer, so every API inherits it
  unless its own section overrides a key.
- **Precedence per key:** CLI flags > `[apis.NAME]` > `[serve]` >
  defaults. Surface switches (`--lazy`, `--enable-preview`, `--http`,
  `--format`, `--verbose`) are server-wide and only read from CLI /
  `[serve]`.
- **Status & health:** `mcpify status` prints one line per API and
  exits 2 if any is unreachable; the `mcpify_health` tool returns a
  combined report (`apis: [...]`, `all_reachable`, dead-API hint).
- **Either/or:** pass a positional spec *or* `[apis.*]` sections —
  mcpify rejects the combination with a clear error instead of
  guessing which wins.

## 13. Dashboard, metrics, mock & hot reload

### `mcpify ui` — the local dashboard

```bash
mcpify ui openapi.json                    # http://127.0.0.1:8787
mcpify ui --config .mcpify.toml           # multi-API surface
mcpify ui openapi.json --http-token S3cret  # non-loopback or token'd
```

One stdlib page, zero external resources: a tool explorer with full
JSON schemas, **masked dry-run previews** (the dashboard never executes
calls — `mcpify try` is for real ones), on-demand health probes with
latency history, a masked log tail, and a config form that writes a
validated `.mcpify.toml` (unknown keys → HTTP 400, never a silent dead
config). Binds 127.0.0.1; a warning is printed if you bind wider
without a token.

### Prometheus metrics

```bash
mcpify serve openapi.json --metrics 9090              # stdio + /metrics sidecar
mcpify serve --http 8080 --metrics 127.0.0.1:9090     # HTTP + metrics together
```

Scrape `http://127.0.0.1:9090/metrics`:

```
mcpify_tool_calls_total{api="mcpify",outcome="ok",tool="list_pets"} 3
mcpify_tool_latency_seconds_bucket{api="mcpify",tool="list_pets",le="0.05"} 2
mcpify_cache_requests_total{result="hit"} 11
mcpify_api_health{api="catalog"} 1
```

Recording is opt-in (a boolean check when off). Example per-API alert
rule — error rate above 10% for 5 minutes:

```yaml
- alert: McpifyApiErrorRate
  expr: |
    sum by (api) (rate(mcpify_tool_calls_total{outcome="error"}[5m]))
      / sum by (api) (rate(mcpify_tool_calls_total[5m])) > 0.10
  for: 5m
```

OpenTelemetry export stays out of core on purpose (SDK = heavy
dependency); it may arrive as an optional extra.

### `mcpify mock` — a fake API from the spec

```bash
mcpify mock ./openapi.json --http 8000 --delay-ms 120
```

Every documented operation answers with a schema-shaped JSON example
(examples > example > default > const/enum > format > type; `$ref`
resolved; objects honor `required`). Path templates match
`/users/{id}` style segments; unknown paths get a 404 listing the
known routes. Perfect with the dashboard: point `mcpify ui`'d serve at
the mock for a full offline playground.

### `--reload` — hot-swapping the tool surface

```bash
mcpify serve openapi.json --reload        # stdio
mcpify serve --config .mcpify.toml --reload --http 8080
mcpify ui openapi.json --reload
```

A daemon thread watches the local spec files (URLs are skipped with a
note) and rebuilds the tool list in place on change — the stdio loop
and HTTP handler closures keep working. If a save leaves the spec
broken, the previous surface stays live and stderr says so. Config
file changes still need a restart.

## Health & status

```bash
mcpify status openapi.json          # reachability, tool count, auth env
mcpify status https://api.example.com --json
```

Exit code 0 when the API answered, 2 when unreachable. Inside a session,
the `mcpify_health` tool (listed in every mode) returns the same report:
`api_reachable`, `api_status`, latency, tool count, cache/retry settings,
and whether the auth env variable is actually set.

## 14. Governance & upgrades: diff, audit, RBAC, plugins, tracing

### `mcpify diff` — upgrade a spec without breaking your agents

```bash
mcpify diff v1.yaml v2.yaml                # human report
mcpify diff v1.yaml v2.yaml --json         # machine report (CI)
mcpify diff v1.yaml v2.yaml --fail-on-breaking   # CI gate: exit 1 if breaking
```

Breaking means "a deployed agent can fail": an operation disappears, a
required parameter appears, an optional one becomes required, or a request
body becomes required. Deprecations and `operationId` renames are reported
as warnings. The report ends with a migration guide written for the agent
consumer ("pass `limit` when calling `GET /pets`").

### `--audit-log` — who called what, when

```bash
mcpify serve spec.yaml --audit-log /var/log/mcpify/audit.jsonl
```

One JSON line per real API call: `ts`, `tool`, `api`, `status`,
`outcome`, `latency_ms`, `arguments_fingerprint`. Arguments are hashed
(sha256, first 12 hex chars), never written raw — the log correlates
repeat calls without storing end-user content. An unwritable file warns
once; serving never stops because auditing failed.

### `--http-token-file` — per-token tool scopes

```toml
# tokens.toml
[tokens.readonly]
token = "tok-read-xyz"
allow = ["^list_", "^get_"]

[tokens.ops]
token = "tok-ops-xyz"
allow = [".*"]
deny  = ["^delete_"]
```

```bash
mcpify serve spec.yaml --http 8080 --http-token-file tokens.toml
```

Each caller authenticates with their own bearer; their `tools/list` shows
only what their scopes allow and scoped-out calls are refused with a clear
error. Deny wins; at least one `allow` is required (a scope-less token is
what plain `--http-token` is for). This is name-based access control, not
identity/SSO — `docs/SELF-HOSTING.md` is explicit about the difference.

### `--plugin` — your Python, mcpify's serving loop

```python
# plug.py
AUTH = MyRotatingAuthProvider()      # replaces the spec-derived credential logic

def on_request(request):             # see every upstream request
    request["headers"]["X-Org"] = "acme"
    return request

def on_result(result):               # see every raw result (pre-formatting)
    return result
```

```bash
mcpify serve spec.yaml --plugin plug.py --plugin other.py
```

Hooks run best-effort: a raising hook is suppressed, the request still
goes through. `AUTH`, when present, overrides `--auth-env`-style
configuration (a note is printed when it does).

### `--write-auth-env` — split the read and write identities

```bash
mcpify serve spec.yaml \
  --auth-env READ_KEY \
  --write-auth-env WRITE_KEY
```

GET calls carry the read credential; POST/PUT/PATCH/DELETE carry the write
credential. A manipulated or over-eager read call can now only do what the
read key allows — `--read-only` filters the surface, the split makes the
*credential* match the policy. Style and header/query name are inherited
from `--auth-style`/`--auth-name`; override with `--write-auth-style` /
`--write-auth-name`. Per API: `write-auth-env` inside `[apis.NAME]`. Static
credentials only — with OAuth2 the flag is refused (a second token flow is
deliberate future work).

### `[tool-text]` — the operator's last word on tool text

```toml
[tool-text.showPetById]
description = "Fetch one pet by id. Returns 404 for unknown ids."
```

Spec authors wrote descriptions for humans; the model reads them as
prompts. When the operator vouches for a surface, the operator should be
able to fix the text without forking the spec. Keys are final tool names
(aggregation prefixes included); unknown names warn on stderr; `mcpify
doctor` flags instruction-like or docs-grade-verbose descriptions so you
know where to start.

### `--fields` — response projection

```bash
mcpify serve spec.yaml --fields id,name,status
```

The rule is documented and predictable at EVERY level: a selected key
keeps its value verbatim; non-selected containers are transparent — they
survive only when they still hold selected data after projection; empty
containers and unmatched items are dropped. API envelopes become
transparent: `--fields id,event` on an alert envelope keeps `features`
with each alert's `id` and `properties.event`, and drops geometry,
context and prose. Per API: `fields = "id,name"` in `[apis.NAME]`.
Combine with `--cache-ttl` for cheap repeated reads.

### `mcpify list --cost` — know the bill before serving

```bash
mcpify list openapi.yaml --cost
mcpify list openapi.yaml --json --cost     # cost_tokens per tool
```

Prints an estimated context cost for the surface (~4 chars/token over name
+ description + input schema): the price every agent pays in EVERY
`tools/list`. Estimates, not measurements — but exact enough to decide
what to cut (`--tag`, `--include`, `--lazy`, or `[tool-text]` to shorten
descriptions).

### OAuth2 write split (`--write-oauth2-*`)

```bash
mcpify serve spec.yaml \
  --oauth2-token-url https://auth.example.com/token --oauth2-client-id-env READ_CLIENT \
  --write-oauth2-token-url https://auth.example.com/token --write-oauth2-client-id-env WRITE_CLIENT
```

A second client-credentials token flow for non-GET calls: reads
authenticate as the read client, writes as the write client. Each flow
caches its own token; the 401 self-heal applies to both. Mutually
exclusive with `--write-auth-env` (pick one credential kind for writes).

### SSE responses (Streamable HTTP)

Clients that send `Accept: text/event-stream` on POST get the response as
a single `message` event (`text/event-stream`); JSON-only clients keep
getting `application/json`. The transport stays stateless — one event,
then the stream closes. Server-initiated messages (a GET SSE stream with
sessions) remain deliberately out of scope.

### `--redact` — secrets never reach the model

```bash
mcpify serve spec.yaml --redact password,token,client_secret
```

Values whose key names one of the listed fields are masked with `***` at
every level of every response — success and error bodies alike,
case-insensitive (`Password`, `Client_Secret`). Arrays are masked in
place, so indices stay stable for the agent. Projection runs first,
redaction last: a field you asked for by name is still masked when it is
a secret. This is the security boundary — `--fields` is only a
projection. Per API: `redact = "password,token"` in `[apis.NAME]`.

### `--rate-limit` — be kind to the upstream

```bash
mcpify serve spec.yaml --rate-limit 5      # max 5 requests/second
```

A thread-safe client-side throttle: every call waits for its slot before
the request leaves — retries included, so a retrying call cannot burst
the API either. In multi-API configs each upstream gets its own limiter;
one API's budget never waits for another's slots. Combine with
`--wait-on-429` for APIs that publish explicit Retry-After delays. Per
API: `rate-limit = 2.5`.

### `doctor --probe` — dial the API before you serve

```bash
mcpify doctor spec.yaml --probe --base-url https://api.example.com

# prove the credential works end-to-end, not just that the host answers
mcpify doctor spec.yaml --probe --auth-env MY_KEY

# CI gate: 4xx/5xx count as failure (default counts only connection failures)
mcpify doctor spec.yaml --probe --auth-env MY_KEY --fail-on-http-error
```

After the static report, mcpify performs one argument-free GET (or the
base URL when every GET needs arguments) and reports reachability. Any
HTTP status proves the API is up — a 401 with no credentials configured
is a working API; only a connection failure exits non-zero, so CI and
shell scripts stop before serving something that cannot answer. With
`--auth-env` the probe carries your real credential (style auto-detected
from the spec, or `--auth-style/--auth-name`); the report is tagged
`authenticated`. `--fail-on-http-error` treats 4xx/5xx as a failed
pre-flight for pipelines. `--json` includes the probe payload
(`probe.status`, `probe.authenticated`, `probe.latency_seconds`).

### `init --probe` — the pre-flight is part of setup

```bash
mcpify init --probe
```

After the wizard writes `.mcpify.toml`, mcpify runs one live probe
against the configured API with the configured credential. Unreachable
exits 1 — you learn the credential or URL is wrong in the same minute
you write the config, not on the first agent call.

### `mcpify try` session controls — `:fields`, `:redact`

```text
:redact password,token     # mask these fields in every subsequent response
:fields id,name            # project subsequent responses to these fields
:redact                    # show the current setting
:fields -                  # clear the session projection
```

Both apply immediately to subsequent tool calls in the session — try a
suspected-secret field once, see `***`, move on without restarting the
server. The same two settings are visible in `mcpify status` (`policy:`
line, or per-API in multi-API reports) and in Prometheus counters
(`mcpify_redactions_total`, `mcpify_projection_responses_total`).

### `mcpify diff --probe` — prove the NEW spec before adopting it

```bash
mcpify diff old.yaml new.yaml --probe --auth-env MY_KEY
```

The upgrade report now ends with two live facts: the **surface-cost
delta** (`surface cost: ~18 → ~38 tokens (+111.1%)` — what the change
does to every agent's tools/list bill) and a **probe of the NEW spec's
API** — one argument-free GET, with your credential when `--auth-env` is
given. Probe failure exits 2 (infra problem, distinct from the
breaking-change exit 1); `--fail-on-http-error` counts 4xx/5xx as
failure too. Adopting a spec that does not answer is now impossible to
do silently.

### `mcpify list --cost --lazy` — price the lazy lever too

```bash
mcpify list big-api.json --cost --lazy
```

Beside the full-surface price, prints what the lazy mode costs: three
meta tools (search / get_tool_schema / call_tool) replace the whole
listing. Live api.weather.gov: ~291 tokens vs ~6,900 — a 96% cut,
quantified before you choose.

### `mcpify list` over a config — every surface, one run

```bash
mcpify list --config .mcpify.toml --cost
```

With `[apis.*]` sections, `list` previews every API: per-API tool tables,
and with `--cost` a per-API plus total context price (live example:
weather.gov 69 tools ~6,900 tokens + petstore 19 tools ~1,679 tokens =
~8,579 tokens). `--json` emits one row per tool with its `api` label.

### `--otel` — one span per upstream call

```bash
pip install 'mcpify[otel]'
mcpify serve spec.yaml --otel http://localhost:4318/v1/traces
```

Each upstream API call produces a span carrying `mcpify.tool`,
`mcpify.api`, `mcpify.ok`, `mcpify.status_detail` and
`mcpify.latency_ms`. Without the extra installed, `--otel` fails with the
exact install command; numeric metrics stay in Prometheus `--metrics`.
