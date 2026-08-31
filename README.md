<!-- mcp-name: io.github.furkan708/mcpify -->

# mcpify

[![Tests](https://img.shields.io/badge/tests-493%20passed-brightgreen)](https://github.com/furkan708/mcpify/actions/workflows/ci.yml)
[![CI](https://github.com/furkan708/mcpify/actions/workflows/ci.yml/badge.svg)](https://github.com/furkan708/mcpify/actions/workflows/ci.yml)
[![CodeQL](https://github.com/furkan708/mcpify/actions/workflows/codeql.yml/badge.svg)](https://github.com/furkan708/mcpify/actions/workflows/codeql.yml)
[![PyPI](https://img.shields.io/pypi/v/mcpify-openapi)](https://pypi.org/project/mcpify-openapi/)
[![PyPI Downloads](https://img.shields.io/pypi/dm/mcpify-openapi)](https://pypi.org/project/mcpify-openapi/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
![License](https://img.shields.io/badge/license-MIT-green)
[![MCP Registry](https://img.shields.io/badge/MCP_Registry-listed-4A90D9)](server.json)
![Dependencies](https://img.shields.io/badge/dependencies-zero-success)

English | [Türkçe](README.tr.md)

<p align="center">
  <img src="docs/demo.gif" alt="mcpify in action — listing and serving OpenAPI endpoints as MCP tools" width="720">
</p>

**Turn any OpenAPI REST API into an [MCP](https://modelcontextprotocol.io) server** — so Claude Code, Cursor, and every other MCP client can call your API directly. One command, zero runtime dependencies:

```bash
mcpify serve https://your-company.com/openapi.json
```

```bash
# try it right now, nothing installed (uvx pulls from PyPI on demand)
uvx --from mcpify-openapi mcpify list examples/petstore.json --cost
```

Focused, production-ready, CLI-first: one job (OpenAPI → MCP), with the
governance, token economics and operations around it that real deployments
need. 493 tests across twenty-seven suites, two transports (stdio + HTTP
with SSE responses), dual MCP-spec compatibility, split read/write
credentials (static and OAuth2), a policy layer, ETag-aware caching, safe
retries, health probes, an audit trail, per-token tool RBAC and plugin
hooks back that one job.

## Why you'll like it

**From spec to server**

- **60 seconds to working** — point it at any OpenAPI 3.x spec (file or URL)
- **Every operation becomes a first-class MCP tool** — input schemas are generated from `parameters` + `requestBody`, internal `$ref`s are resolved
- **Spec versions diffed from the tool view** — `mcpify diff old.yaml new.yaml` reports added/removed/changed operations with per-change **breaking** verdicts and a migration guide; `--fail-on-breaking` is a CI gate
- **`mcpify doctor`** — tells you if your spec is agent-friendly before you ship: missing operationIds, missing summaries, instruction-like tool text, overlong descriptions; `--probe` dials the API once — with your real credential (`--auth-env`) when you want auth proven end-to-end, and `--fail-on-http-error` for a strict CI gate
- **`mcpify try` / `mcpify mock` / `mcpify output-server`** — call the tools without an agent client, serve a schema-shaped fake API for CI, or bake a serve command into a shareable script

**Credentials & policy**

- **Credentials never touch the spec or the model** — pulled from your environment at call time; the spec's own security declarations pick the flags (bearer, basic, header, query)
- **OAuth2 client-credentials built in** — tokens are fetched, cached, refreshed and re-fetched on a mid-flight 401 (RFC 6749, stdlib only); `--write-oauth2-*` gives non-GET calls a **second client identity** so reads and writes authenticate as different clients
- **Least-privilege by default** — `--write-auth-env` splits the static credential (reads on your read key, writes on a dedicated key), `--read-only` filters the surface, `--deny/--allow` hides mutating GETs, per-token RBAC gives each bearer token its own allow/deny scopes
- **`--redact password,token`** — values whose key names a secret are masked with `***` at every level of every response (error bodies included, case-insensitive); the model never sees them
- **Audit trail without content exposure** — one JSON line per call: tool, API, status, latency, an argument *fingerprint* (never raw arguments); `--plugin` loads your Python module for auth/request/result hooks

**Token economics**

- **See the bill before serving** — `mcpify list --cost` prices the surface (~4 chars/token): what every agent pays in EVERY `tools/list`; multi-API configs get per-API and total prices in one run
- **`--fields id,event`** — response projection that selects at every level: selected keys keep their value, non-selected containers stay transparent, emptied containers drop. Live weather.gov: 350 alerts in full inside the budget that previously truncated at ~189
- **Valid truncation** — oversized responses are cut along JSON structure with an explicit `"truncated": true` marker, never mid-document
- **`--lazy` search-then-call** — cut api.weather.gov's listing by 95.5%; search results now show what pulling each full schema would cost, so the agent pulls only what it needs
- **`[tool-text]` overrides** — doctor flags model-facing instruction-like descriptions; you replace them per tool in config

**Operations**

- **Two transports, one tool surface** — stdio for local agents; `serve --http 8080` speaks MCP Streamable HTTP (SSE responses for clients that ask, JSON otherwise) so a whole team shares one server, with optional bearer tokens
- **Several APIs, one MCP server** — `[apis.NAME]` sections in `.mcpify.toml`: per-API auth, caching, retries, filters and rate limits; collision-safe renames; aggregated health; `mcpify status` probes every API in parallel
- **Upstream courtesy built in** — ETag-aware caching, idempotent-only retries (502/503/504), `--wait-on-429` honors Retry-After, `--rate-limit RPS` caps requests/second (per upstream in multi-API, retries included)
- **Observability, opt-in only** — Prometheus `--metrics` (call counters, latencies, cache, health — plus projection/redaction counters when those run), `--otel` spans, `--reload` hot swap, `mcpify ui` local dashboard
- **Host it yourself for free** — docker-compose with automatic-HTTPS Caddy plus a hardened systemd unit: [Self-hosting guide](docs/SELF-HOSTING.md)
- **Zero runtime dependencies** — the entire tree is auditable stdlib Python; YAML specs need an optional `pip install 'mcpify[yaml]'`

## Quick start

```bash
# install (installs the `mcpify` command)
pipx install mcpify-openapi

# run without installing (uvx — pulls from PyPI on demand)
uvx --from mcpify-openapi mcpify list ./openapi.json --read-only

# first time? the wizard writes a config for you
uvx --from mcpify-openapi mcpify init

# ...as a container (GHCR, published on every release)
docker run -i ghcr.io/furkan708/mcpify:latest serve ./openapi.json --read-only

# ...or from source
git clone https://github.com/furkan708/mcpify.git
cd mcpify && pip install .

# 1. preview the tools that will be generated (add --cost for the context price)
mcpify list examples/petstore.json

# 2. validate the spec is agent-friendly (+ --probe for a live pre-flight)
mcpify doctor examples/petstore.json

# 3. serve it over MCP
mcpify serve examples/petstore.json --base-url https://petstore.example.com/v1

# 4. no agent client at hand? try the tools in your terminal
mcpify try examples/petstore.json --base-url https://petstore.example.com/v1

# 5. or share it over HTTP with the whole team
mcpify serve examples/petstore.json --http 8080 --http-token $SHARED_TOKEN
```

### With authentication

```bash
# Bearer token read from the environment (never hardcoded)
export PETSTORE_KEY="sk-..."
mcpify serve petstore.json \
  --base-url https://petstore.example.com/v1 \
  --auth-env PETSTORE_KEY \
  --auth-style bearer \            # optional: auto-detected from the spec
  --read-only
```

No explicit style needed in the common case — the spec's security
declarations pick bearer/basic/header/query (with the right name) for
you. For HTTP Basic, the env variable holds `username:password`:
`--auth-style basic --auth-env CREDS`.

| Flag | Meaning |
| ---- | ------- |
| `--auth-env VAR` | environment variable holding the credential |
| `--auth-style bearer\|basic\|header\|query` | how it is sent (**default: auto-detected from the spec**) |
| `--auth-name NAME` | header / query name for non-bearer styles (e.g. `X-API-Key`) |

### With OAuth2 (client credentials)

For APIs behind an OAuth2 identity provider (RFC 6749 §4.4). Credentials
live in the environment; the access token is fetched, cached until its
`expires_in`, refreshed transparently, and re-fetched automatically once
if the API answers 401 mid-flight:

```bash
export OAUTH2_CLIENT_ID="..."
export OAUTH2_CLIENT_SECRET="..."
mcpify serve api.json \
  --oauth2-token-url https://idp.example.com/oauth2/token \
  --oauth2-client-id-env OAUTH2_CLIENT_ID \
  --oauth2-client-secret-env OAUTH2_CLIENT_SECRET \
  --oauth2-scope "read write"        # optional; --oauth2-client-auth body for token endpoints that reject Basic
```

**Split write identities too:** `--write-oauth2-token-url` (+ client/scope
flags) runs a second client-credentials flow for non-GET calls — reads
authenticate as the read client, writes as the write client, each with
its own token cache and the same 401 self-heal. Mutually exclusive with
`--write-auth-env` (pick one credential kind for writes).

### Multiple APIs in one server

Put several OpenAPI documents in one config and serve them as a single
tool surface — no gateway, no per-API process:

```toml
# .mcpify.toml
[apis.catalog]
spec = "https://shop.example.com/openapi.json"
auth-env = "CATALOG_TOKEN"          # per-API credential
cache-ttl = 60
rate-limit = 5                      # per-API courtesy throttle (req/s)
redact = "password,client_secret"   # per-API response masking

[apis.crm]
spec = "./crm.yaml"
read-only = true                    # per-API policy
base-url = "https://crm.internal/v2"
fields = "id,name"                  # per-API response projection

[apis.weather]
spec = "https://api.weather.gov/openapi.json"
timeout = 10
```

Surface switches (`--lazy`, `--enable-preview`, `--http`, `--format`) are
server-wide flags; credentials, policies, caching and retries are per-API.

```bash
mcpify list --cost      # preview every API and price each surface
mcpify serve            # stdio, all three APIs, prefixed on collisions
mcpify serve --http 8080
mcpify try              # REPL across every API
mcpify status           # probes each API concurrently
```

`mcpify status` reports per API — `[catalog] reachable (status 200, 0.03s)
— https://shop.example.com — 31 tools` — and exits non-zero if any API is
unreachable. When two APIs expose the same tool name (`list_pets`), both
get renamed with their label (`catalog_list_pets`, `crm_list_pets`) so
nothing silently wins; non-conflicting names stay untouched. The
`mcpify_health` tool returns one report covering every API. Precedence
per key: CLI flags > `[apis.NAME]` > `[serve]`. Pass a positional spec
*or* `[apis.*]` sections — never both.

## Plug it into your agent

**Claude Code:**

```bash
claude mcp add my-api -- mcpify serve openapi.json --read-only
```

**Claude Desktop / Cursor / any MCP client** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "petstore": {
      "command": "mcpify",
      "args": ["serve", "~/specs/petstore.json", "--auth-env", "PETSTORE_KEY"]
    }
  }
}
```

**HTTP transport (team-shared server)** — run `mcpify serve api.json --http 0.0.0.0:8080 --http-token $TOKEN` once, then point HTTP-capable clients at it:

```json
{
  "mcpServers": {
    "petstore": {
      "type": "http",
      "url": "http://your-host:8080",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Now ask your agent: *"list the pets, then create one named Milo"* — it discovers `list_pets` and `create_pet`, fills the arguments, and performs real HTTP calls.

## How operations become tools

| OpenAPI | mcpify |
| ------- | ------ |
| `operationId` | tool name (sanitized; falls back to `method_path`) |
| `summary` / `description` | tool description the agent reads |
| `deprecated: true` | shown by `mcpify list` before you expose old endpoints |
| `parameters` (path/query/header) | individual typed arguments with enums |
| `requestBody` (JSON) | a `body` object argument |
| `$ref` pointers | resolved inline (components → real schemas) |
| `servers[0].url` | default base URL (override: `--base-url`) |

The agent only ever sees the tool list and your API's JSON responses —
mcpify adds no middleware, caches nothing you did not ask for, and sends
credentials nowhere except your API.

## Doctor

```bash
$ mcpify doctor my-api.json
openapi: 3.0.3
title:   Acme API
paths:   23
tools:   41 operations
servers: https://api.acme.com
warning: 12/41 operations have no operationId (names fall back to method_path)
warning: 30/41 operations have no summary (agents see no description)
```

Add `--probe` for a live pre-flight — after the static report, mcpify
dials one argument-free GET (or the base URL) and reports reachability;
a connection failure exits non-zero so CI and shell scripts stop before
serving:

```bash
$ mcpify doctor https://api.weather.gov/openapi.json --probe
...
probe:    GET /alerts → 200 reachable (2.80s)
```

## CLI reference

```
mcpify list <spec> [--tag T] [--include P] [--exclude P] [--read-only] [--json]
mcpify list --cost                        # price the surface (~4 chars/token)
mcpify list --cost --lazy                 # ...and the 3-meta-tool lazy surface
mcpify list --config .mcpify.toml --cost  # multi-API: every surface priced
mcpify serve <spec> [--base-url URL] [--server INDEX|NAME] [--name N] [--auth-env VAR]
                    [--auth-style bearer|header|query] [--auth-name NAME]
                    [--oauth2-token-url URL --oauth2-client-id-env VAR
                     --oauth2-client-secret-env VAR] [--timeout S]
                    [--read-only] [--tag T] [--include P] [--exclude P]
                    [--http [HOST:]PORT] [--http-token TOKEN] [--wait-on-429 SEC]
mcpify try <spec> [same serve flags]        # interactive REPL, no agent needed
mcpify output-server <spec> -o FILE [-- <any serve flags>]
mcpify ui <spec> [same serve flags]         # local dashboard (tool explorer, health, config)
mcpify mock <spec> [--http 8000] [--delay-ms N]
mcpify diff OLD NEW [--json] [--fail-on-breaking]        # upgrade report + CI gate
mcpify diff OLD NEW --probe [--auth-env V]               # ...+ live check of the NEW API + cost delta
mcpify config-schema                        # JSON Schema for .mcpify.toml (editor wiring)
mcpify doctor <spec> [--probe --auth-env V --fail-on-http-error]   # static audit + live pre-flight / CI gate

# token economics: --fields id,name (projection), --redact password,token (masking),
#   --rate-limit RPS (upstream courtesy, retries included)
# credential split: --write-auth-env WRITE_KEY_ENV or --write-oauth2-token-url (reads keep --auth-env)
# tool-text overrides: [tool-text.TOOL] description = "..." in .mcpify.toml

# multi-API: define [apis.NAME] sections in .mcpify.toml, then run
#   mcpify list|serve|try|status|ui (no positional spec) — one process, every API
# ops add-ons for serve/ui: --metrics [HOST:]PORT   --reload   --cache-warm
#   --audit-log FILE   --http-token-file FILE   --plugin FILE (repeatable)   --otel [ENDPOINT]
```

### Notes & limitations

- JSON specs work out of the box; YAML specs need `pip install 'mcpify[yaml]'`
- External `$ref` targets (files or URLs) are bundled automatically at load; circular
  cross-file refs are left in place rather than unwound (surface skips what it cannot resolve)
- Oversized JSON responses truncate to valid JSON (first items + a truncation marker);
  non-JSON bodies cut at a character boundary
- `--fields` selects at every level by documented rule (selected keys verbatim,
  non-selected containers transparent); it is a projection, not a security boundary —
  use `--redact` when a field must never reach the model
- Request bodies are exposed as a single `body` object argument — predictable over clever
- HTTP transport serves one JSON-RPC response per request (batching was removed from
  the MCP spec); clients that send `Accept: text/event-stream` get it framed as a
  single SSE `message` event. Server-initiated streams (a GET stream with sessions)
  stay deliberately out of scope for a stateless server
- Spec versions: OpenAPI 3.x and Swagger 2.x roots are accepted; 3.x is the happy path

## Hardened against the real world

mcpify is audited on every release against a 10-category checklist of MCP
best practices and published production failure modes — not just our own
examples:

- **Hostile-spec corpus (12/12):** circular `$ref`s, multipart uploads,
  `allOf` schemas, server URL variables, relative base URLs, oversized
  responses — every scenario derived from a documented real-world failure,
  fixed, and locked in by a regression test. Sources include the arXiv
  study of REST→MCP generation across 18 real APIs.
- **Live integration:** the real api.weather.gov spec loads in CI — and
  the live checks found the last two real bugs (nested-truncation
  envelope splits, top-level-only projection) before any user did.
- **MCP lifecycle enforced:** tools are unreachable until the client
  completes the `initialize` handshake.
- **Blast-radius controls:** read-only mode, deny/allow policy layer,
  per-token RBAC, response projection + secret masking, rate limiting,
  40k-char valid truncation, `--timeout`, credentials never logged.

Full checklist with per-item status: **[docs/AUDIT-CHECKLIST.md](docs/AUDIT-CHECKLIST.md)**

## Tests

**493 passing**, plus one live-integration test that loads the real
api.weather.gov document (auto-skipped when offline) and an OTel positive
test that runs wherever the optional tracing extra is installed. Every
suite runs on Python 3.10–3.12 across Linux and Windows; `ruff`, strict
`mypy` and CodeQL gate every push.

| Suite | Tests | What it pins down |
|---|---:|---|
| Spec parsing & resolution | 13 | OpenAPI 3.x + YAML loading, `$ref` chains, `allOf` merge, server variables, malformed input |
| Tool translation | 19 | operationId naming with collision suffixing, input schemas, enums, body handling, annotation & output-schema derivation |
| Agent surface | 32 | HTTP-derived annotations, structured output contract, remediation errors, `--lazy` search, dry-run previews |
| CLI | 15 | `list` / `doctor` / `serve` flags, `--json` output, deprecated badges |
| Hostile corpus | 11 | circular `$ref`s, multipart bodies, relative base URLs, 300 KB truncation, 500-op performance — each traced to a documented real-world failure |
| Lifecycle & hygiene | 8 | initialize handshake (`-32002`), byte-pure stdio, credentials never logged |
| Protocol end-to-end | 9 | real JSON-RPC over stdio against a live local HTTP API, wire-level assertions |
| Policy layer | 7 | `--read-only`, `--allow` / `--deny` precedence, mutating-GET protection |
| `$ref` parameters | 4 | parameter schemas resolved against the full spec — the weather.gov bug class (one test hits the live document) |
| Ops & configuration | 47 | config files + env precedence, init wizard, cache TTL & bounds, retry safety, XML conversion, discovery, batching, status/health |
| Protocol version compat | 5 | 2026-07-28 stateless `_meta` requests and the legacy 2025-06-18 handshake, on the same wire |
| HTTP transport | 19 | Streamable HTTP: lifecycle over POST, 405/411/413/415 error ladder, parse/batch rejections, bearer enforcement, bind-string parser |
| OAuth2 client-credentials | 18 | token fetch/cache/refresh with a fake clock, Basic vs body client auth, public clients, every failure mode, 401 self-heal end-to-end |
| `try` REPL | 26 | piped-stdin sessions: selection by number/name, typed prompts, re-prompt on bad input, `:raw`/`:info`, clean EOF/Ctrl+C exits, read-only surface |
| `output-server` | 11 | embedded spec integrity, guard rails (existing file, bad spec, unknown flags), secret warnings, and a real subprocess E2E handshake |
| Server selection | 17 | `--server INDEX|NAME` rules: index, description/URL name matching, error listings, `--base-url` precedence, server-variable defaults, CLI/status/config/doctor wiring |
| Auth auto-detection & Basic | 22 | securitySchemes → style/name resolution (OpenAPI + Swagger 2.0), requirement-order precedence, operation-level security, exact hint text, HTTP Basic header encoding, CLI/try/doctor wiring, explicit-style override |
| Rate-limit courtesy (`--wait-on-429`) | 9 | Retry-After honored once within cap, cap exceeded returns 429 untouched, missing header falls back to retry delay, HTTP-date form never waits, POST never auto-waited, CLI wiring |
| Multi-API aggregation | 26 | `[apis.*]` merge with two-sided collision prefixes and `_2` suffixes, per-API routing/auth/cache isolation, concurrent aggregated health (dead-API named in hint), lazy search across APIs incl. label match, preview routing, status exit codes, `--env` inheritance, both-rejected flag combos |
| Ops: dashboard, metrics, mock, reload | 25 | `/metrics` text format (counters/histograms/cache hit-miss/health gauges), token'd UI routes, masked preview API, config-form writer (+unknown-key 400), schema-shaped mock responses with template routing, hot-reload rebuild incl. broken-spec survival |
| CLI connectivity glue | 10 | `--http` wiring, `MCPIFY_HTTP_TOKEN` fallback, OAuth2 flag rules, config-file keys, wizard option 5, `try` smoke test |
| Spec diff (`mcpify diff`) | 14 | added/removed/changed ops, breaking verdicts (required param added/became, body became required, op removal), deprecation & operationId warnings, migration guide, document-level diff, CLI exit contract 0/1/2, `--json` |
| v1.11 serving: audit, cache, RBAC, plugins | 17 | JSONL audit trail with argument fingerprints + fail-safe on unwritable files, ETag 304 revalidation on stale entries, `mcpify_cache_invalidate` (scoped + full), `--cache-warm` pre-calls argument-free GETs only, token-file scoping end-to-end (401 / filtered lists / refused calls, deny wins, duplicate-token rejection), plugin hooks on real requests, `mcpify ui` dispatch (dead-command regression), `config-schema` matches the config module, OTel guard |
| External `$ref` bundling | 6 | file + URL-base targets inlined, component-only target files, nested refs resolved relative to their own file, missing targets skipped, circular refs survive, same-document refs untouched |
| Governance: split keys, tool text, valid truncation | 21 | read-key/write-key per method over a live upstream (shared-identity default unchanged), style/name inheritance + explicit override, config `write-auth-*` keys in serve/envs/apis, `[tool-text]` override through `list --json`, unknown-tool warnings, validator errors, schema/keys parity, doctor instruction-like + overlong-description counts, oversized array → valid JSON with marker, object key-keeping, non-JSON fallback, error-prefix survival |
| v1.13: cost, projection, SSE, OAuth2 write | 20 | surface pricing in JSON + human output, recursive projection with transparent envelopes (both rules pinned: the top-level-only first rule failed live), selected keys keep their arrays, SSE framing vs JSON clients, write-flow resolution + mutual exclusion with `--write-auth-env` |
| v1.16: status policy, REPL session controls, diff probe + cost delta | 12 | policy (fields/redact/rate-limit) in multi-API JSON+human status and single-spec `policy:` line, `:redact`/`:fields` session set/show/clear over a live upstream (masking verified on the wire), diff surface-cost delta in JSON+human, `--probe` reachable/unreachable exit contract (2 on probe failure), form retry-delay float |
| v1.15: auth-probe, strict gate, metrics, lazy pricing | 16 | probe with a real credential (401-without vs 200-with over a live local upstream), strict-mode verdicts, doctor CLI exit contract, projection/redaction Prometheus counters (values counted, fresh-session enable), count_redact_targets, lazy-surface pricing lines, `init --probe` reachable/unreachable, dashboard-form token keys (float coercion, unknown-key rejection) |
| v1.14: redact, rate-limit, probe, multi list | 29 | masking at every level incl. error bodies and selected-key overlap, arrays masked in place, limiter slots with a fake clock, retry throttling, probe target selection + reachability exit contract, config `redact`/`rate-limit` in serve/apis/envs, per-upstream limiters, multi-API `list` + pricing |

Policy on failures: every bug found in the wild becomes a pinned
regression test before the fix ships — the suite only grows.

Run it locally:

```bash
pip install pytest pyyaml
pytest -v
```

## Roadmap

The v1.6–1.16 roadmap is fully shipped. Possible future work (not
promised): server-initiated SSE (a GET stream with sessions —
deliberately out for a stateless transport).
- [x] ~~`status` policy visibility, REPL `:redact`/`:fields`, `diff --probe` + cost delta, form examples~~ — shipped in v1.16.0
- [x] ~~Authenticated `doctor --probe` + `--fail-on-http-error` CI gate, `init --probe`, projection/redaction metrics, lazy-surface pricing~~ — shipped in v1.15.0
- [x] ~~`--redact`, `--rate-limit`, `doctor --probe`, lazy-search costs, multi-API `list`~~ — shipped in v1.14.0
- [x] ~~OAuth2 write flow (`--write-oauth2-*`), `list --cost`, `--fields` projection, SSE POST responses~~ — shipped in v1.13.0
- [x] ~~Read/write credential split, tool-text overrides, doctor prompt-hygiene audit, structure-aware truncation~~ — shipped in v1.12.0
- [x] ~~Spec diff + audit log + per-token RBAC + plugin hooks + config JSON Schema + external `$ref` bundling + OTel extra~~ — shipped in v1.11.0
- [x] ~~Web dashboard, Prometheus metrics, mock server, hot reload~~ — shipped in v1.10.0
- [x] ~~Multi-API aggregation: one `serve` process fronting several OpenAPI documents~~ — shipped in v1.9.0
- [x] ~~HTTP transport~~, ~~OAuth2 client-credentials~~, ~~`mcpify try` REPL~~, ~~`--output-server`~~ — shipped in v1.6.0

## License

MIT — see the [LICENSE](LICENSE) file for details.
