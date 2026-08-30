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

## `mcpify init` — setup wizard

```bash
mcpify init
```

Asks for the spec (bare origins are auto-discovered), base URL, auth
style and env variable, read-only mode, cache/retry, and writes
`.mcpify.toml`. Prefill any answer with `--spec` / `--base-url`.

## Operational flags

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

## Health & status

```bash
mcpify status openapi.json          # reachability, tool count, auth env
mcpify status https://api.example.com --json
```

Exit code 0 when the API answered, 2 when unreachable. Inside a session,
the `mcpify_health` tool (listed in every mode) returns the same report:
`api_reachable`, `api_status`, latency, tool count, cache/retry settings,
and whether the auth env variable is actually set.
